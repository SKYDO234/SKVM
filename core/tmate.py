"""tmate SSH sharing for SKVM guests.

tmate is installed inside the guest during provisioning. Starting a
session runs `tmate new-session -d` in the guest and reads the real
`tmate display -p '#{tmate_ssh} / #{tmate_web}'` values back — the panel
only ever displays strings returned by the guest itself. Sessions can be
revoked with stop_session() (kills the tmate server in the guest).

Key creation (the part that used to fail): tmate authenticates to the
tmate.io server with an SSH identity from ~/.ssh. tmate itself refuses
to connect when no key exists ("SSH keys not found. Run 'ssh-keygen' to
create keys."), so SKVM creates the guest's SSH keys explicitly:
`ssh-keygen -A` for the OpenSSH host keys (needed by sshd) and a
passphrase-less root identity (id_ed25519, RSA fallback) for tmate.
"""
from __future__ import annotations
import shlex
import time

from . import backend

INSTALL_CHECK = "command -v tmate >/dev/null 2>&1 && echo yes || echo no"
KEY_READY_MARKER = "SKVM_SSH_KEY_READY"

# Idempotent guest-side key creation. Safe to run on every tmate start:
# existing keys are never overwritten.
KEY_SETUP_SCRIPT = (
    "mkdir -p /root/.ssh /var/run/sshd; chmod 700 /root/.ssh; "
    "ssh-keygen -A >/dev/null 2>&1 || true; "
    "if ! ls /root/.ssh/id_ed25519 /root/.ssh/id_rsa /root/.ssh/id_ecdsa >/dev/null 2>&1; then "
    "ssh-keygen -q -t ed25519 -N '' -f /root/.ssh/id_ed25519 "
    "|| ssh-keygen -q -t rsa -b 2048 -N '' -f /root/.ssh/id_rsa || true; "
    "fi; "
    "ls /root/.ssh/id_*.pub >/dev/null 2>&1 && echo " + KEY_READY_MARKER + " || true"
)


def is_installed(vps) -> bool:
    try:
        _, out = backend.exec_shell(vps, INSTALL_CHECK, timeout=20)
        return "yes" in out
    except Exception:
        return False


def ensure_installed(vps):
    """Install tmate in a Debian/Ubuntu guest if missing (idempotent).

    Uses the same serialized, lock-waiting, repair-retrying apt helper
    shape as provisioning so a busy or bruised package manager in the
    guest does not turn into a spurious "tmate could not be installed".
    """
    if is_installed(vps):
        return True
    script = (
        "export DEBIAN_FRONTEND=noninteractive; "
        "APT=\"apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=30 "
        "-o Dpkg::Lock::Timeout=240\"; "
        "exec 9>/run/skvm-apt.lock 2>/dev/null || exec 9>/tmp/skvm-apt.lock; flock 9 || true; "
        "for _try in 1 2 3; do "
        "  dpkg --configure -a >/dev/null 2>&1 || true; "
        "  apt-get clean >/dev/null 2>&1 || true; "
        "  if $APT update -y >/dev/null 2>&1 && $APT install -y --fix-missing tmate openssh-client; then break; fi; "
        "  sleep 5; "
        "done; true"
    )
    try:
        backend.exec_shell(vps, script, timeout=600)
    except Exception:
        pass
    return is_installed(vps)


def ensure_identity(vps) -> bool:
    """Create the guest SSH keys tmate/sshd need, if missing.

    Returns True when a usable identity key exists afterwards. Never
    overwrites existing keys.
    """
    try:
        _, out = backend.exec_shell(vps, KEY_SETUP_SCRIPT, timeout=60)
        return KEY_READY_MARKER in (out or "")
    except Exception:
        return False


def _display(vps, sock):
    _, out = backend.exec_shell(
        vps, f"tmate -S {shlex.quote(sock)} display -p '#{{tmate_ssh}}' 2>/dev/null; "
             f"tmate -S {shlex.quote(sock)} display -p '#{{tmate_web}}' 2>/dev/null; true",
        timeout=20)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    ssh = next((ln for ln in lines if ln.startswith("ssh ")), "")
    web = next((ln for ln in lines if ln.startswith("http")), "")
    return ssh, web


def _diagnose(vps, sock) -> str:
    """Best-effort guest-side diagnostics for a failed tmate start."""
    try:
        _, out = backend.exec_shell(
            vps,
            "echo \"tmate: $(command -v tmate || echo missing)\"; "
            "ls /root/.ssh/id_*.pub >/dev/null 2>&1 && echo 'ssh identity: present' "
            "|| echo 'ssh identity: MISSING'; "
            f"tmate -S {shlex.quote(sock)} list-sessions 2>&1 | head -3; "
            "ls -la /root/.tmate.sock 2>/dev/null || true",
            timeout=20)
        lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
        return " | ".join(lines[-4:])[:400]
    except Exception:
        return ""


def start_session(vps, wait_seconds: float = 30.0):
    """Start (or reuse) a detached tmate session; return (ssh, web)."""
    if not ensure_installed(vps):
        raise RuntimeError("tmate could not be installed in this guest")
    if not ensure_identity(vps):
        raise RuntimeError(
            "Could not create an SSH key in this guest (ssh-keygen missing?). "
            "tmate needs a key in /root/.ssh to connect — install openssh-client and retry.")
    sock = f"/tmp/skvm-tmate-{vps.id}.sock"
    script = (
        f"S={shlex.quote(sock)}; "
        f"tmate -S \"$S\" kill-server 2>/dev/null || true; "
        f"rm -f \"$S\"; "
        f"tmate -S \"$S\" -f /dev/null new-session -d -s skvm 2>&1; "
        # Bounded wait: if the guest cannot reach tmate.io (no outbound
        # SSH) this must not hang the request forever.
        f"timeout 25 tmate -S \"$S\" wait tmate-ready 2>/dev/null || true; "
        f"tmate -S \"$S\" display -p '#{{tmate_ssh}}' 2>/dev/null; "
        f"tmate -S \"$S\" display -p '#{{tmate_web}}' 2>/dev/null; true"
    )
    _, out = backend.exec_shell(vps, script, timeout=90)
    lines = [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    ssh = next((ln for ln in lines if ln.startswith("ssh ")), "")
    web = next((ln for ln in lines if ln.startswith("http")), "")
    deadline = time.time() + wait_seconds
    while not ssh and time.time() < deadline:  # guest may need a moment to register
        time.sleep(1.5)
        try:
            ssh, web = _display(vps, sock)
        except Exception:
            break
    if not ssh:
        detail = _diagnose(vps, sock)
        raise RuntimeError(
            "tmate session started but returned no SSH string. The guest may "
            "be unable to reach tmate.io (outbound SSH/DNS), or the tmate "
            "server died." + (f" Guest diagnostics: {detail}" if detail else ""))
    return ssh, web


def stop_session(vps):
    sock = f"/tmp/skvm-tmate-{vps.id}.sock"
    try:
        backend.exec_shell(vps, f"tmate -S {shlex.quote(sock)} kill-server 2>/dev/null || true; "
                                f"rm -f {shlex.quote(sock)}", timeout=20)
    except Exception:
        pass
