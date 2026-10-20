"""Backend registry.

Routes request a driver through :func:`get_backend`.  Selection rules:

1. ``SKVM_BACKEND=demo`` (or ``testing``) → :class:`DemoBackend`
2. a server pinned to a node → :class:`NodeBackend` for that node
3. ``SKVM_BACKEND=node`` → least-loaded online node
4. otherwise Docker if the daemon answers, else the demo driver (with a warning)
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from flask import current_app, has_app_context

from .base import Backend, BackendError, ConsoleSession, ExecResult, InstanceInfo, NotSupported, ProvisionRequest
from .demo_backend import DemoBackend
from .docker_backend import DockerBackend
from .node_backend import NodeBackend

log = logging.getLogger("skvm.backend")

__all__ = [
    "Backend", "BackendError", "ConsoleSession", "ExecResult", "InstanceInfo", "NotSupported",
    "ProvisionRequest", "DemoBackend", "DockerBackend", "NodeBackend",
    "get_backend", "backend_status", "available_backends", "reset_cache",
]

_PROBE_LOCK = threading.Lock()
_PROBE_CACHE: dict[str, Any] = {"ts": 0.0, "docker": (False, "not probed"), "demo": (True, "ready")}
PROBE_TTL = 20.0


def _docker_available(force: bool = False) -> tuple[bool, str]:
    now = time.time()
    with _PROBE_LOCK:
        if not force and (now - _PROBE_CACHE["ts"]) < PROBE_TTL:
            return _PROBE_CACHE["docker"]
    result = DockerBackend.available()
    with _PROBE_LOCK:
        _PROBE_CACHE["docker"] = result
        _PROBE_CACHE["ts"] = time.time()
    return result


def reset_cache() -> None:
    with _PROBE_LOCK:
        _PROBE_CACHE["ts"] = 0.0


def _config_mode() -> str:
    if not has_app_context():
        return "auto"
    return str(current_app.config.get("BACKEND", "auto")).lower()


def get_backend(server=None, node=None) -> Backend:
    """Resolve the driver for a server / node / platform default."""
    mode = _config_mode()
    if mode == "demo":
        return DemoBackend()

    if server is not None and getattr(server, "driver", "") == "demo":
        return DemoBackend()

    if node is None and server is not None:
        node = getattr(server, "node", None)
        if node is not None:
            return NodeBackend(node)

    if mode == "node":
        candidate = node or pick_node()
        if candidate is None:
            raise BackendError("No online hypervisor nodes are registered", code="no_nodes")
        return NodeBackend(candidate)

    if mode == "docker":
        return DockerBackend()

    # auto
    ok, detail = _docker_available()
    if ok:
        return DockerBackend()
    log.debug("docker unavailable (%s) — falling back to demo driver", detail)
    return DemoBackend()


def available_backends() -> list[dict[str, Any]]:
    docker_ok, docker_detail = _docker_available(force=True)
    return [
        {
            "name": "docker",
            "label": "Docker engine",
            "available": docker_ok,
            "detail": docker_detail,
            "features": sorted(DockerBackend.supports),
        },
        {
            "name": "node",
            "label": "Remote hypervisor agents",
            "available": _online_nodes() > 0,
            "detail": f"{_online_nodes()} online node(s)",
            "features": sorted(NodeBackend.supports),
        },
        {
            "name": "demo",
            "label": "Demo (simulated)",
            "available": True,
            "detail": "always available — no hypervisor required",
            "features": sorted(DemoBackend.supports),
        },
    ]


def _online_nodes() -> int:
    try:
        from ..db import db
        from ..models import Node

        return db.session.query(Node).filter(Node.status == "online").count()
    except Exception:
        return 0


def pick_node(exclude: set[int] | None = None, cpu: float = 0, ram_mb: int = 0):
    """Choose the least-loaded online node that fits the request."""
    from ..db import db
    from ..models import Node

    exclude = exclude or set()
    nodes = (
        db.session.query(Node)
        .filter(Node.status == "online")
        .order_by(Node.priority.asc(), Node.overload.asc())
        .all()
    )
    for candidate in nodes:
        if candidate.id in exclude:
            continue
        if cpu and candidate.capacity_cpu and candidate.overload + cpu > candidate.capacity_cpu:
            continue
        return candidate
    return nodes[0] if nodes else None


def backend_status() -> dict[str, Any]:
    """Health summary shown on dashboards and the status page."""
    mode = _config_mode()
    docker_ok, docker_detail = _docker_available(force=True)
    active = "demo"
    if mode in ("docker", "node"):
        active = mode
    elif docker_ok:
        active = "docker"
    return {
        "configured": mode,
        "active": active,
        "docker": docker_ok,
        "docker_detail": docker_detail,
        "nodes_online": _online_nodes(),
        "host": _host_snapshot() if active != "node" else {},
    }


def _host_snapshot() -> dict[str, Any]:
    try:
        import psutil

        memory = psutil.virtual_memory()
        return {
            "cpu_pct": psutil.cpu_percent(interval=None),
            "cpu_count": psutil.cpu_count(logical=True),
            "memory_total_mb": round(memory.total / 1024 / 1024, 1),
            "memory_used_pct": round(memory.percent, 1),
            "disk": _disk_snapshot(),
            "load": list(psutil.getloadavg()) if hasattr(psutil, "getloadavg") else [],
            "boot_time": psutil.boot_time(),
        }
    except Exception as exc:  # pragma: no cover
        return {"error": str(exc)[:160]}


def _disk_snapshot() -> dict[str, Any]:
    try:
        import psutil

        usage = psutil.disk_usage("/")
        return {
            "total_gb": round(usage.total / 1024**3, 1),
            "used_gb": round(usage.used / 1024**3, 1),
            "percent": usage.percent,
        }
    except Exception:  # pragma: no cover
        return {}
