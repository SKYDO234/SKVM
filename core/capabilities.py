"""Host / node capability snapshot.

These numbers describe the NODE (host) and are always labelled as such in
the UI. Per-VPS usage comes from core.backend.guest_stats(), which reads
the guest/container itself — host figures are never presented as VPS
figures.
"""
from __future__ import annotations
import os
import platform
import shutil
import subprocess
from pathlib import Path
import psutil


def cmd_version(cmd):
    path = shutil.which(cmd)
    if not path:
        return None
    try:
        p = subprocess.run([cmd, "--version"], text=True, stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, timeout=5)
        return (p.stdout or "").strip().splitlines()[0][:180]
    except Exception:
        return None


def has_cmd(cmd):
    return shutil.which(cmd) is not None


def lxcfs_status() -> dict:
    """LXCFS virtualizes /proc so guest tools (free, neofetch, fastfetch)
    report the container's own limits instead of the host node's.

    LXCFS must run on the same host as the container runtime. The panel
    reports it honestly: active with mount root, or unavailable with the
    reason it cannot provide virtualized stats here.
    """
    disabled = (os.getenv("SKVM_LXCFS", "true") or "").strip().lower() in {"0", "false", "no", "off"}
    root = Path((os.getenv("SKVM_LXCFS_PATH", "/var/lib/lxcfs") or "/var/lib/lxcfs").strip())
    if disabled:
        return {"ok": False, "path": str(root), "message": "LXCFS disabled via SKVM_LXCFS"}
    if (root / "proc" / "meminfo").exists():
        return {"ok": True, "path": str(root),
                "message": "LXCFS active — guest /proc is virtualized per-container"}
    binary = shutil.which("lxcfs")
    fuse = Path("/dev/fuse").exists()
    if binary and not fuse:
        return {"ok": False, "path": str(root), "binary": binary,
                "message": "lxcfs is installed but /dev/fuse is not available in this environment"}
    return {"ok": False, "path": str(root), "binary": binary,
            "message": "LXCFS not running on this node (install lxcfs or run on the Docker host)"}


def kvm_status():
    dev = os.path.exists("/dev/kvm")
    if has_cmd("kvm-ok"):
        try:
            p = subprocess.run(["kvm-ok"], text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=8)
            return {"device": dev, "ok": p.returncode == 0,
                    "message": (p.stdout or "").strip()[:500]}
        except Exception as e:
            return {"device": dev, "ok": dev, "message": str(e)}
    return {"device": dev, "ok": dev, "message": "/dev/kvm present" if dev else "kvm-ok not installed"}


def docker_status():
    """Real container-runtime reachability via the Docker SDK (never faked).

    Works against a Docker daemon or a Podman API socket (Codespaces /
    rootless-container friendly). Reports runtime + endpoint so the UI can
    show exactly what provisioning would talk to.
    """
    from core.docker_engine import resolve_endpoint, runtime_name
    endpoint = resolve_endpoint()
    info = {"installed": has_cmd("docker") or has_cmd("podman"), "available": False,
            "version": None, "containers": None, "message": "",
            "runtime": runtime_name(endpoint), "endpoint": endpoint,
            "podman": has_cmd("podman")}
    try:
        import docker  # Docker SDK for Python
        client = docker.DockerClient(base_url=endpoint, timeout=4)
        data = client.info()
        info["available"] = True
        info["version"] = (data.get("ServerVersion") or None)
        info["containers"] = data.get("Containers")
        try:
            client.close()
        except Exception:
            pass
    except Exception as exc:  # daemon down, SDK missing, permission, ...
        info["message"] = str(exc)[:300]
    return info


def incus_status():
    available = False
    if has_cmd("incus"):
        try:
            p = subprocess.run(["incus", "info"], text=True, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, timeout=6)
            available = p.returncode == 0
        except Exception:
            available = False
    return {"installed": has_cmd("incus"), "available": available,
            "version": cmd_version("incus")}


def snapshot():
    vm = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    boot = psutil.boot_time()
    import time
    return {
        "hostname": platform.node(),
        "os": f"{platform.system()} {platform.release()}",
        "cpu_count": psutil.cpu_count(logical=True),
        "cpu_physical": psutil.cpu_count(logical=False),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_total_mb": round(vm.total / 1024 / 1024),
        "memory_used_mb": round(vm.used / 1024 / 1024),
        "memory_available_mb": round(vm.available / 1024 / 1024),
        "memory_percent": vm.percent,
        "disk_total_gb": round(disk.total / 1024 / 1024 / 1024, 1),
        "disk_used_gb": round(disk.used / 1024 / 1024 / 1024, 1),
        "disk_free_gb": round(disk.free / 1024 / 1024 / 1024, 1),
        "disk_percent": disk.percent,
        "uptime_seconds": int(time.time() - boot),
        "load": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
        "incus": incus_status(),
        "docker": docker_status(),
        "kvm": kvm_status(),
        "lxcfs": lxcfs_status(),
        "qemu": cmd_version("qemu-system-x86_64"),
    }


def available_backends():
    """Backends that can actually provision right now on this node."""
    out = []
    if docker_status()["available"]:
        out.append("docker")
    if incus_status()["available"]:
        out.append("incus")
    return out
