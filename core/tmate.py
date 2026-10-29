"""tmate SSH sharing for SKVM guests.

tmate is installed inside the guest during provisioning. Starting a
session runs `tmate new-session -d` in the guest and reads the real
`tmate display -p '#{tmate_ssh} / #{tmate_web}'` values back — the panel
only ever displays strings returned by the guest itself. Sessions can be
revoked with stop_session() (kills the tmate server in the guest).
"""
from __future__ import annotations
import shlex
import time

from . import backend

INSTALL_CHECK = "command -v tmate >/dev/null 2>&1 && echo yes || echo no"


def is_installed(vps) -> bool:
    try:
        _, out = backend.exec_shell(vps, INSTALL_CHECK, timeout=20)
        return "yes" in out
    except Exception:
        return False


def ensure_installed(vps):
    """Install tmate in a Debian/Ubuntu guest if missing (idempotent)."""
    if is_installed(vps):
        return True
    script = ("export DEBIAN_FRONTEND=noninteractive; "
              "(apt-get update -y && apt-get install -y tmate) "
              "|| (apt-get install -y --no-install-recommends tmate)")
    backend.exec_shell(vps, script, timeout=420)
    return is_installed(vps)


def start_session(vps, wait_seconds: float = 12.0):
    """Start (or reuse) a detached tmate session; return (ssh, web)."""
    if not ensure_installed(vps):
        raise RuntimeError("tmate could not be installed in this guest")
    sock = f"/tmp/skvm-tmate-{vps.id}.sock"
    script = (
        f"S={shlex.quote(sock)}; "
        f"tmate -S \"$S\" kill-server 2>/dev/null || true; "
        f"rm -f \"$S\"; "
        f"tmate -S \"$S\" -f /dev/null new-session -d -s skvm; "
        f"tmate -S \"$S\" wait tmate-ready; "
        f"tmate -S \"$S\" display -p '#{{tmate_ssh}}' ; "
        f"tmate -S \"$S\" display -p '#{{tmate_web}}'"
    )
    _, out = backend.exec_shell(vps, script, timeout=60)
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    ssh = next((ln for ln in lines if ln.startswith("ssh ")), "")
    web = next((ln for ln in lines if ln.startswith("http")), "")
    deadline = time.time() + wait_seconds
    while not ssh and time.time() < deadline:  # guest may need a moment to register
        time.sleep(1.5)
        _, out = backend.exec_shell(
            vps, f"tmate -S {shlex.quote(sock)} display -p '#{{tmate_ssh}}'; "
                 f"tmate -S {shlex.quote(sock)} display -p '#{{tmate_web}}'", timeout=20)
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        ssh = next((ln for ln in lines if ln.startswith("ssh ")), "")
        web = next((ln for ln in lines if ln.startswith("http")), "")
    if not ssh:
        raise RuntimeError("tmate session started but returned no SSH string yet; try again in a few seconds")
    return ssh, web


def stop_session(vps):
    sock = f"/tmp/skvm-tmate-{vps.id}.sock"
    try:
        backend.exec_shell(vps, f"tmate -S {shlex.quote(sock)} kill-server 2>/dev/null || true; "
                                f"rm -f {shlex.quote(sock)}", timeout=20)
    except Exception:
        pass
