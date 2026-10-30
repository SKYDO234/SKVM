"""Unified VPS backend dispatcher (Incus + Docker).

Every VPS row records which backend owns it, so start/stop/console/stats
always talk to the right daemon. New VPS placement picks the backend from
the node setting / SKVM_BACKEND, preferring a backend that is actually
reachable — the panel never reports a provisioning success for a daemon
that is down.
"""
from __future__ import annotations
import shlex

from . import docker_engine
from .config import BACKEND
from .capabilities import docker_status, incus_status
from .engine import (EngineError, action as incus_action, create_instance,
                     guest_exec, instance_status as incus_status_of,
                     remove as incus_remove, resize_instance, safe_name)
from .os_profiles import get_profile

__all__ = ["EngineError", "safe_name", "resolve_backend", "create", "status",
           "do_action", "remove_instance", "resize", "exec_argv", "exec_shell",
           "guest_stats", "pull_or_launch_note"]


def resolve_backend(requested: str | None = None) -> str:
    """Choose a backend that is actually available right now."""
    want = (requested or BACKEND or "auto").lower()
    if want in ("incus", "docker"):
        ok = incus_status()["available"] if want == "incus" else docker_status()["available"]
        if not ok:
            raise EngineError(f"Requested backend '{want}' is not available on this node")
        return want
    # auto: prefer Incus (full VPS semantics), fall back to Docker.
    if incus_status()["available"]:
        return "incus"
    if docker_status()["available"]:
        return "docker"
    raise EngineError("No provisioning backend is available: neither the Incus daemon "
                      "nor the Docker daemon responded. Install/start one (see install.sh) "
                      "and retry — the VPS was not created.")


def _images_for(vps):
    profile = get_profile(vps.os_slug)
    incus_image, docker_image = profile.image, profile.docker_image
    try:
        from . import db as dbmod
        row = dbmod.get_os_image(vps.os_slug)
        if row is not None:
            incus_image, docker_image = row.incus_image, row.docker_image
    except Exception:
        pass
    return incus_image, docker_image


def create(vps):
    """Provision the instance. Returns dict with container_id + notes."""
    backend = vps.backend
    incus_image, docker_image = _images_for(vps)
    if backend == "docker":
        docker_engine.pull_image(docker_image)
        cid, notes = docker_engine.create_container(
            vps.name, docker_image, vps.vcpus, vps.memory_mb, vps.disk_gb)
        docker_engine.wait_ready(vps.name)
        return {"container_id": cid, **notes}
    # Incus backend (system containers and QEMU virtual machines).
    create_instance(vps.name, vps.os_slug, vps.engine, vps.vcpus, vps.memory_mb, vps.disk_gb)
    st = incus_status_of(vps.name)
    # Incus mounts the host's LXCFS into system containers automatically
    # when it is running there. VMs see their real guest kernel instead.
    lxcfs_on = False
    if vps.engine == "container":
        try:
            from .capabilities import lxcfs_status
            lxcfs_on = bool(lxcfs_status().get("ok"))
        except Exception:
            lxcfs_on = False
    return {"container_id": None, "ip": st.get("ip"), "disk_quota_enforced": True,
            "lxcfs": lxcfs_on}


def status(vps):
    if vps.backend == "docker":
        return docker_engine.instance_status(vps.name)
    st = incus_status_of(vps.name)
    return {"state": st.get("state", "missing"), "ip": st.get("ip"), "container_id": None}


def do_action(vps, op):
    if op not in ("start", "stop", "restart"):
        raise ValueError("Unsupported action")
    if vps.backend == "docker":
        return docker_engine.action(vps.name, op)
    st = incus_action(vps.name, op)
    return {"state": st.get("state"), "ip": st.get("ip"), "container_id": None}


def remove_instance(vps):
    if vps.backend == "docker":
        docker_engine.remove(vps.name)
    else:
        incus_remove(vps.name)


def resize(vps, vcpus, memory_mb, disk_gb):
    if vps.backend == "docker":
        return docker_engine.resize_container(vps.name, vcpus, memory_mb, disk_gb)
    st = resize_instance(vps.name, vcpus, memory_mb, disk_gb)
    return {"state": st.get("state"), "disk_quota_enforced": True}


def exec_argv(vps, argv, timeout=300):
    """Run an argv list in the guest. No shell is involved for Docker;
    for Incus the argv is passed as separate exec arguments."""
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("argv must be a non-empty list")
    if vps.backend == "docker":
        return docker_engine.exec_in(vps.name, list(argv), timeout=timeout)
    cmd = " ".join(shlex.quote(str(a)) for a in argv)
    return guest_exec(vps.name, cmd, timeout=timeout)


def exec_shell(vps, script: str, timeout=300):
    """Run a panel-generated provisioning script inside the guest.

    Callers must only pass fixed panel scripts / fixed commands with
    validated, quoted values — never raw end-user input.
    """
    if vps.backend == "docker":
        return docker_engine.exec_shell(vps.name, script, timeout=timeout)
    return guest_exec(vps.name, script, timeout=timeout)


def guest_stats(vps):
    """Usage read from the guest/container itself (never host numbers)."""
    base = {"backend": vps.backend}
    if vps.backend == "docker":
        s = docker_engine.stats(vps.name)
        if s is None:
            return {**base, "state": "missing"}
        out = {**base, **s}
        if s.get("state") == "running":
            rc, txt = exec_argv(vps, ["sh", "-lc",
                                      "hostname; cat /etc/os-release 2>/dev/null | head -2; "
                                      "uptime; df -h / | tail -1; free -m | head -2"], timeout=20)
            out["guest"] = txt[-2000:] if rc == 0 else ""
        return out
    st = incus_status_of(vps.name)
    out = {**base, "state": st.get("state"), "ip": st.get("ip")}
    if st.get("state") == "running":
        try:
            _, txt = guest_exec(vps.name,
                                "hostname; (cat /etc/os-release 2>/dev/null | head -2); uptime; "
                                "df -h / | tail -1; free -m | head -2; "
                                "cat /proc/loadavg", timeout=20)
            out["guest"] = txt[-2000:]
        except Exception as exc:
            out["guest_error"] = str(exc)[:300]
    return out


def pull_or_launch_note(vps):
    incus_image, docker_image = _images_for(vps)
    return docker_image if vps.backend == "docker" else incus_image
