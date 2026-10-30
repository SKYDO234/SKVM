"""SKVM allocation fetch profile for VPS guests.

New guests get a small, dependency-free ``skvm-fetch`` command. The
familiar ``neofetch`` and ``fastfetch`` commands in the web console are
wrappers around it, so they report the CPU/RAM/storage allocation saved
for that VPS by the panel/admin instead of leaking host-node figures
from the container runtime. The profile also combines those allocations
with live guest facts (OS, kernel, uptime and current guest/cgroup usage
where the guest exposes it).
"""
from __future__ import annotations

import re
import shlex

from . import backend
from .engine import EngineError

READY_MARKER = "SKVM_FETCH_READY"

FETCH_SCRIPT = r'''#!/bin/sh
# SKVM PANEL allocation-aware fetch. Installed by the panel; safe to run
# as any user. It reads /etc/skvm/allocations.conf (or $SKVM_ALLOCATION_FILE).
CONF="${SKVM_ALLOCATION_FILE:-/etc/skvm/allocations.conf}"
if [ -r "$CONF" ]; then
  # Values are written by SKVM with shell quoting.
  # shellcheck disable=SC1090
  . "$CONF"
fi

is_num() { case "$1" in ''|*[!0-9]*) return 1 ;; *) return 0 ;; esac }
fmt_mib() {
  awk -v mib="$1" 'BEGIN { if (mib >= 1024) printf "%.1f GiB", mib/1024; else printf "%d MiB", mib }'
}
pct_of() {
  awk -v used="$1" -v total="$2" 'BEGIN { if (total <= 0) print 0; else { p = used / total * 100; if (p > 100) p = 100; if (p < 0) p = 0; printf "%d", p + 0.5 } }'
}
bar_for() {
  pct="$1"; filled=$((pct * 18 / 100)); i=0; out=""
  while [ "$i" -lt 18 ]; do
    if [ "$i" -lt "$filled" ]; then out="${out}█"; else out="${out}░"; fi
    i=$((i + 1))
  done
  printf '%s' "$out"
}
json_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/	/\\t/g' | tr '\n' ' '
}

USER_NAME="$(id -un 2>/dev/null || printf 'root')"
HOST_NAME="$(hostname 2>/dev/null || printf '%s' "${SKVM_HOSTNAME:-skvm}")"
OS_NAME="$(awk -F '"' '/^PRETTY_NAME=/{print $2; exit}' /etc/os-release 2>/dev/null)"
[ -n "$OS_NAME" ] || OS_NAME="$(uname -s 2>/dev/null || printf 'Linux')"
KERNEL="$(uname -r 2>/dev/null || printf 'unknown')"
ARCH="$(uname -m 2>/dev/null || printf 'unknown')"
UPTIME_TEXT="$(uptime -p 2>/dev/null | sed 's/^up //')"
if [ -z "$UPTIME_TEXT" ] && [ -r /proc/uptime ]; then
  UPTIME_TEXT="$(awk '{ d=int($1/86400); h=int(($1%86400)/3600); m=int(($1%3600)/60); if (d>0) printf "%d days, %d hours, %d minutes", d,h,m; else if (h>0) printf "%d hours, %d minutes", h,m; else printf "%d minutes", m }' /proc/uptime)"
fi
[ -n "$UPTIME_TEXT" ] || UPTIME_TEXT="unknown"
SHELL_NAME="$(printf '%s' "${SHELL:-/bin/bash}" | sed 's#.*/##')"
LOADAVG="$(awk '{print $1, $2, $3}' /proc/loadavg 2>/dev/null || printf 'unknown')"
LOAD1="$(printf '%s' "$LOADAVG" | awk '{print $1}')"
IP_ADDR="$(hostname -I 2>/dev/null | awk '{print $1}')"
[ -n "$IP_ADDR" ] || IP_ADDR="${SKVM_IP:-pending}"

VCPUS="${SKVM_VCPUS:-}"
is_num "$VCPUS" || VCPUS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || printf '1')"
is_num "$VCPUS" || VCPUS=1
MEMORY_MIB="${SKVM_MEMORY_MB:-}"
if ! is_num "$MEMORY_MIB"; then
  MEMORY_MIB="$(awk '/^MemTotal:/{printf "%d", $2/1024; exit}' /proc/meminfo 2>/dev/null || printf '0')"
fi
is_num "$MEMORY_MIB" || MEMORY_MIB=0
DISK_GIB="${SKVM_DISK_GB:-}"
if ! is_num "$DISK_GIB"; then
  DISK_GIB="$(df -Pk / 2>/dev/null | awk 'NR==2{printf "%d", ($2/1024/1024)+0.5}')"
fi
is_num "$DISK_GIB" || DISK_GIB=0

MEM_USED_MIB=""
if [ -r /sys/fs/cgroup/memory.current ]; then
  raw="$(cat /sys/fs/cgroup/memory.current 2>/dev/null || printf '')"
  is_num "$raw" && MEM_USED_MIB=$((raw / 1024 / 1024))
elif [ -r /sys/fs/cgroup/memory/memory.usage_in_bytes ]; then
  raw="$(cat /sys/fs/cgroup/memory/memory.usage_in_bytes 2>/dev/null || printf '')"
  is_num "$raw" && MEM_USED_MIB=$((raw / 1024 / 1024))
elif [ -r /proc/meminfo ]; then
  MEM_USED_MIB="$(awk '/^MemTotal:/{t=$2} /^MemAvailable:/{a=$2} END{if(t>0) printf "%d", (t-a)/1024}' /proc/meminfo 2>/dev/null || printf '')"
fi
is_num "$MEM_USED_MIB" || MEM_USED_MIB=""
DISK_USED_KIB="$(df -Pk / 2>/dev/null | awk 'NR==2{print $3}')"
is_num "$DISK_USED_KIB" || DISK_USED_KIB=0
DISK_USED_MIB=$((DISK_USED_KIB / 1024))
DISK_TOTAL_KIB=$((DISK_GIB * 1024 * 1024))
CPU_PCT="$(awk -v load="$LOAD1" -v cpu="$VCPUS" 'BEGIN { if (cpu<=0 || load !~ /^[0-9.]+$/) print 0; else { p=load/cpu*100; if(p>100)p=100; printf "%d", p+0.5 } }')"
MEM_PCT=0; [ -n "$MEM_USED_MIB" ] && MEM_PCT="$(pct_of "$MEM_USED_MIB" "$MEMORY_MIB")"
DISK_PCT="$(pct_of "$DISK_USED_KIB" "$DISK_TOTAL_KIB")"
MEMORY_HUMAN="$(fmt_mib "$MEMORY_MIB")"
MEM_USED_HUMAN=""; [ -n "$MEM_USED_MIB" ] && MEM_USED_HUMAN="$(fmt_mib "$MEM_USED_MIB")"
DISK_USED_HUMAN="$(fmt_mib "$DISK_USED_MIB")"

case "${1:-}" in
  --json)
    mem_used_json="null"; [ -n "$MEM_USED_MIB" ] && mem_used_json="$MEM_USED_MIB"
    printf '{"hostname":"%s","os":"%s","kernel":"%s","arch":"%s","backend":"%s","engine":"%s","plan":"%s","vcpus":%s,"memory_mb":%s,"memory_used_mb":%s,"disk_gb":%s,"disk_used_mb":%s,"load":"%s","ip":"%s","allocation_source":"SKVM PANEL"}\n' \
      "$(json_escape "$HOST_NAME")" "$(json_escape "$OS_NAME")" "$(json_escape "$KERNEL")" "$(json_escape "$ARCH")" \
      "$(json_escape "${SKVM_BACKEND:-unknown}")" "$(json_escape "${SKVM_ENGINE:-container}")" "$(json_escape "${SKVM_PLAN:-}")" \
      "$VCPUS" "$MEMORY_MIB" "$mem_used_json" "$DISK_GIB" "$DISK_USED_MIB" "$(json_escape "$LOADAVG")" "$(json_escape "$IP_ADDR")"
    exit 0 ;;
  -h|--help)
    printf '%s\n' 'Usage: neofetch | fastfetch | skvm-fetch [--json]' 'Shows this VPS with the allocation assigned in SKVM PANEL.'
    exit 0 ;;
esac

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C0='\033[0m'; CB='\033[1m'; CC='\033[38;5;81m'; CV='\033[38;5;141m'; CG='\033[38;5;82m'; CY='\033[38;5;220m'; CD='\033[38;5;245m'
else
  C0=''; CB=''; CC=''; CV=''; CG=''; CY=''; CD=''
fi

printf "${CV}${CB}%s${C0}\n" '███████╗██╗  ██╗██╗   ██╗███╗   ███╗'
printf "${CV}${CB}%s${C0}\n" '██╔════╝██║ ██╔╝██║   ██║████╗ ████║'
printf "${CC}${CB}%s${C0}\n" '███████╗█████╔╝ ██║   ██║██╔████╔██║'
printf "${CC}${CB}%s${C0}\n" '╚════██║██╔═██╗ ╚██╗ ██╔╝██║╚██╔╝██║'
printf "${CG}${CB}%s${C0}\n" '███████║██║  ██╗ ╚████╔╝ ██║ ╚═╝ ██║'
printf "${CG}${CB}%s${C0}\n\n" '╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚═╝     ╚═╝'
printf "${CB}%s@%s${C0}\n" "$USER_NAME" "$HOST_NAME"
printf "${CD}%s${C0}\n" '----------------------------------------'
row() { printf "${CC}%-17s${C0} %s\n" "$1" "$2"; }
row 'OS' "$OS_NAME $ARCH"
row 'Host' "SKVM ${SKVM_BACKEND:-runtime} ${SKVM_ENGINE:-container}${SKVM_PLAN:+ · Plan: $SKVM_PLAN}"
row 'Kernel' "$KERNEL"
row 'Uptime' "$UPTIME_TEXT"
row 'Shell' "$SHELL_NAME"
row 'CPU allocation' "$VCPUS vCPU  ${CG}$(bar_for "$CPU_PCT")${C0} ${CPU_PCT}% load"
row 'RAM allocation' "$MEMORY_HUMAN${MEM_USED_HUMAN:+  ·  current $MEM_USED_HUMAN  ${CG}$(bar_for "$MEM_PCT")${C0} ${MEM_PCT}%}"
row 'Disk allocation' "$DISK_GIB GiB  ·  root used $DISK_USED_HUMAN  ${CY}$(bar_for "$DISK_PCT")${C0} ${DISK_PCT}%"
row 'Load average' "$LOADAVG"
row 'Network' "$IP_ADDR"
printf "${CD}%s${C0}\n" 'Allocation source: SKVM PANEL (admin assigned)'
'''


def _clean(value, fallback=""):
    text = "" if value is None else str(value)
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text).strip()
    return text or fallback


def _int(value, fallback=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def allocation_values(vps) -> dict[str, str]:
    """Panel-owned allocation values that are written into the guest."""
    plan_name = ""
    try:
        plan = getattr(vps, "plan", None)
        if plan is not None:
            plan_name = _clean(getattr(plan, "name", ""))
    except Exception:
        plan_name = ""
    return {
        "SKVM_VPS_NAME": _clean(getattr(vps, "name", ""), "skvm-vps"),
        "SKVM_HOSTNAME": _clean(getattr(vps, "hostname", ""), _clean(getattr(vps, "name", ""), "skvm-vps")),
        "SKVM_OS_SLUG": _clean(getattr(vps, "os_slug", ""), "linux"),
        "SKVM_BACKEND": _clean(getattr(vps, "backend", ""), "unknown"),
        "SKVM_ENGINE": _clean(getattr(vps, "engine", ""), "container"),
        "SKVM_PLAN": plan_name,
        "SKVM_VCPUS": str(max(1, _int(getattr(vps, "vcpus", 0), 1))),
        "SKVM_MEMORY_MB": str(max(1, _int(getattr(vps, "memory_mb", 0), 512))),
        "SKVM_DISK_GB": str(max(1, _int(getattr(vps, "disk_gb", 0), 5))),
        "SKVM_IP": _clean(getattr(vps, "ip_address", ""), "pending"),
    }


def allocations_conf(vps) -> str:
    values = allocation_values(vps)
    lines = ["# Managed by SKVM PANEL. Edit allocations in the panel, not here."]
    for key in sorted(values):
        lines.append(f"{key}={shlex.quote(values[key])}")
    return "\n".join(lines) + "\n"


def fetch_script_text() -> str:
    return FETCH_SCRIPT


def guest_install_script(vps) -> str:
    """Return the idempotent guest shell script that installs the profile."""
    conf = allocations_conf(vps)
    wrapper = "#!/bin/sh\nexec /usr/local/bin/skvm-fetch \"$@\"\n"
    return (
        "set -e; "
        "mkdir -p /etc/skvm /usr/local/bin; "
        "cat > /etc/skvm/allocations.conf <<'SKVM_ALLOC_EOF'\n"
        f"{conf}"
        "SKVM_ALLOC_EOF\n"
        "cat > /usr/local/bin/skvm-fetch <<'SKVM_FETCH_EOF'\n"
        f"{FETCH_SCRIPT}"
        "SKVM_FETCH_EOF\n"
        "cat > /usr/local/bin/neofetch <<'SKVM_WRAP_EOF'\n"
        f"{wrapper}"
        "SKVM_WRAP_EOF\n"
        "cat > /usr/local/bin/fastfetch <<'SKVM_WRAP_EOF'\n"
        f"{wrapper}"
        "SKVM_WRAP_EOF\n"
        "chmod 644 /etc/skvm/allocations.conf; "
        "chmod 755 /usr/local/bin/skvm-fetch /usr/local/bin/neofetch /usr/local/bin/fastfetch; "
        f"printf '%s\\n' {READY_MARKER}"
    )


def sync_fetch_profile(vps, timeout=30) -> str:
    """Install/refresh neofetch/fastfetch allocation output inside a guest."""
    rc, out = backend.exec_shell(vps, guest_install_script(vps), timeout=timeout)
    if rc != 0 or READY_MARKER not in (out or ""):
        raise EngineError(f"Could not install SKVM fetch profile: {(out or '')[-500:]}")
    return out
