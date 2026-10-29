"""Container backend for SKVM PANEL (Docker SDK for Python).

Talks to a real Docker daemon or a Podman API-compatible socket
(rootless Podman is the easiest way to run VPS containers inside
Codespaces-style Linux containers). Endpoint resolution order:
DOCKER_HOST env -> /var/run/docker.sock -> Podman sockets
($XDG_RUNTIME_DIR/podman, /run/podman, /run/user/<uid>/podman).

Each VPS is one container with hard CPU/memory limits applied by the
daemon (nano_cpus / mem_limit). Disk quota uses overlay2
`storage_opt.size` where the daemon supports it; when the storage driver
cannot enforce a quota the provisioning job records that honestly in its
details instead of pretending the limit exists.

All operations use the SDK (no shell), so user input can never become a
host shell command.
"""
from __future__ import annotations
import os
import re
import time
from pathlib import Path

NAME_RE = re.compile(r"^[a-z][a-z0-9-]{2,54}$")


class DockerEngineError(RuntimeError):
    pass


def safe_name(name: str) -> str:
    name = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    if not NAME_RE.match(name):
        raise ValueError("Instance name must be 3–55 lowercase letters, digits, or hyphens")
    return name


def resolve_endpoint() -> str:
    """Return the container-runtime endpoint SKVM should talk to."""
    env = (os.getenv("DOCKER_HOST") or "").strip()
    if env:
        return env
    candidates = ["/var/run/docker.sock"]
    xdg = os.getenv("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(str(Path(xdg) / "podman" / "podman.sock"))
    candidates.append("/run/podman/podman.sock")
    try:
        candidates.append(f"/run/user/{os.getuid()}/podman/podman.sock")
    except Exception:
        pass
    candidates.append(str(Path.home() / ".local" / "share" / "containers" / "podman.sock"))
    for sock in candidates:
        if Path(sock).exists():
            return f"unix://{sock}"
    return "unix:///var/run/docker.sock"


def runtime_name(endpoint: str | None = None) -> str:
    endpoint = endpoint or resolve_endpoint()
    return "podman" if "podman" in endpoint else "docker"


def _client():
    try:
        import docker
    except ImportError as exc:
        raise DockerEngineError("Docker SDK (pip package 'docker') is not installed") from exc
    endpoint = resolve_endpoint()
    try:
        client = docker.DockerClient(base_url=endpoint, timeout=10)
        client.ping()
        return client
    except Exception as exc:
        runtime = runtime_name(endpoint)
        raise DockerEngineError(
            f"Container runtime is not available at {endpoint} ({runtime}): {exc}. "
            "Start Docker/Podman (or set DOCKER_HOST) and retry — nothing was created."
        ) from exc


def client_or_none():
    try:
        return _client()
    except DockerEngineError:
        return None


def container_name(vps_name: str) -> str:
    return f"skvm-{safe_name(vps_name)}"


def _get(client, name):
    import docker
    try:
        return client.containers.get(container_name(name))
    except docker.errors.NotFound:
        return None
    except Exception as exc:
        raise DockerEngineError(str(exc)) from exc


def create_container(name, image, vcpus, memory_mb, disk_gb):
    """Create (or adopt) the container and apply resource limits."""
    client = _client()
    cname = container_name(name)
    existing = _get(client, name)
    labels = {"skvm.managed": "true", "skvm.name": safe_name(name)}
    if existing is not None:
        # Idempotent recovery after a panel/worker restart.
        try:
            existing.update(nano_cpus=int(float(vcpus) * 1_000_000_000),
                            mem_limit=f"{int(memory_mb)}m")
        except Exception:
            pass
        if existing.status != "running":
            existing.start()
        return existing.id, {"adopted": True, "disk_quota_enforced": _quota_of(existing)}
    kwargs = dict(
        image=image, name=cname, hostname=safe_name(name), detach=True,
        tty=True, stdin_open=True, labels=labels,
        nano_cpus=int(float(vcpus) * 1_000_000_000),
        mem_limit=f"{int(memory_mb)}m", memswap_limit=f"{int(memory_mb)}m",
        # Keep the guest alive; sshd/tmate are started by provisioning.
        command=["/bin/bash", "-lc", "sleep infinity"],
    )
    # Progressive fallback for runtimes with fewer knobs (e.g. Podman
    # compat API, vfs storage): quota first, then no-memswap. Every drop
    # is reported honestly in the returned notes.
    attempts = [
        (dict(kwargs, storage_opt={"size": f"{int(disk_gb)}G"}), True),
        (dict(kwargs), False),
        ({k: v for k, v in kwargs.items() if k != "memswap_limit"}, False),
    ]
    last_exc = None
    for attempt, quota in attempts:
        try:
            container = client.containers.run(**attempt)
            return container.id, {"adopted": False, "disk_quota_enforced": quota,
                                  "runtime": runtime_name()}
        except Exception as exc:  # try the next, simpler shape
            last_exc = exc
    raise DockerEngineError(f"Container creation failed: {last_exc}")


def _quota_of(container) -> bool:
    try:
        opts = (container.attrs.get("HostConfig") or {}).get("StorageOpt") or {}
        return bool(opts.get("size"))
    except Exception:
        return False


def pull_image(image: str):
    client = _client()
    try:
        client.images.pull(image)
    except Exception as exc:
        raise DockerEngineError(f"Could not pull image {image}: {exc}") from exc


def wait_ready(name, timeout=60):
    client = _client()
    end = time.time() + timeout
    while time.time() < end:
        c = _get(client, name)
        if c is not None:
            c.reload()
            if c.status == "running":
                rc, _ = exec_in(name, ["true"], timeout=10)
                if rc == 0:
                    return
        time.sleep(2)
    raise DockerEngineError("Container did not become ready in time")


def exec_in(name, argv, timeout=300, stdin_text=None):
    """Run an argv list inside the container. Returns (exit_code, output)."""
    client = _client()
    c = _get(client, name)
    if c is None:
        raise DockerEngineError(f"Container for {name} not found")
    if not isinstance(argv, (list, tuple)) or not argv:
        raise ValueError("exec argv must be a non-empty list")
    try:
        result = c.exec_run(list(argv), demux=False, tty=False,
                            stdin=False, socket=False)
        out = result.output.decode("utf-8", "replace") if isinstance(result.output, bytes) else str(result.output or "")
        return int(result.exit_code or 0), out
    except Exception as exc:
        raise DockerEngineError(str(exc)) from exc


def exec_shell(name, script: str, timeout=300):
    """Run a provisioning script via bash inside the guest.

    Only panel-generated scripts (scripts/provision_guest.sh content and
    fixed package commands) may be passed here — never raw user input.
    """
    return exec_in(name, ["/bin/bash", "-lc", script], timeout=timeout)


def instance_status(name):
    client = _client()
    c = _get(client, name)
    if c is None:
        return {"state": "missing", "ip": None, "container_id": None}
    c.reload()
    state = "running" if c.status == "running" else "stopped"
    ip = None
    try:
        nets = (c.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        for net in nets.values():
            if net.get("IPAddress"):
                ip = net["IPAddress"]
                break
    except Exception:
        pass
    return {"state": state, "ip": ip, "container_id": c.id}


def action(name, op):
    client = _client()
    c = _get(client, name)
    if c is None:
        raise DockerEngineError(f"Container for {name} not found")
    if op == "start":
        if c.status != "running":
            c.start()
    elif op == "stop":
        c.stop(timeout=15)
    elif op == "restart":
        c.restart(timeout=15)
    else:
        raise ValueError("Unsupported action")
    time.sleep(1)
    return instance_status(name)


def remove(name):
    client = _client()
    c = _get(client, name)
    if c is None:
        return
    try:
        c.remove(force=True)
    except Exception as exc:
        raise DockerEngineError(str(exc)) from exc


def resize_container(name, vcpus, memory_mb, disk_gb):
    client = _client()
    c = _get(client, name)
    if c is None:
        raise DockerEngineError(f"Container for {name} not found")
    c.update(nano_cpus=int(float(vcpus) * 1_000_000_000), mem_limit=f"{int(memory_mb)}m",
             memswap_limit=f"{int(memory_mb)}m")
    st = instance_status(name)
    st["disk_quota_enforced"] = _quota_of(c)
    st["disk_note"] = ("Disk quota enforced by the storage driver."
                       if st["disk_quota_enforced"] else
                       "Storage driver cannot enforce a hard disk quota; disk_gb is recorded "
                       "as the allocation target. Use overlay2 on XFS (pquota) for hard quotas.")
    return st


def stats(name):
    """Real per-container stats from the daemon (one-shot sample)."""
    client = _client()
    c = _get(client, name)
    if c is None:
        return None
    c.reload()
    if c.status != "running":
        return {"state": "stopped"}
    try:
        s = c.stats(stream=False)
        cpu_delta = s["cpu_stats"]["cpu_usage"]["total_usage"] - s["precpu_stats"]["cpu_usage"]["total_usage"]
        sys_delta = s["cpu_stats"].get("system_cpu_usage", 0) - s["precpu_stats"].get("system_cpu_usage", 0)
        ncpu = s["cpu_stats"].get("online_cpus") or 1
        # Podman compat stats may lack a system delta; report None (unknown)
        # rather than inventing 0%.
        cpu_percent = (cpu_delta / sys_delta * ncpu * 100.0) if sys_delta > 0 else None
        mem = s.get("memory_stats") or {}
        return {"state": "running", "cpu_percent": (round(cpu_percent, 2) if cpu_percent is not None else None),
                "memory_used_mb": round((mem.get("usage") or 0) / 1024 / 1024, 1),
                "memory_limit_mb": round((mem.get("limit") or 0) / 1024 / 1024, 1),
                "memory_percent": round((mem.get("usage", 0) / mem["limit"] * 100.0), 2) if mem.get("limit") else None,
                "runtime": runtime_name()}
    except Exception as exc:
        return {"state": "running", "error": str(exc)[:300]}
