"""Metrics: live sampling, ring buffers, history persistence and Prometheus.

Two tiers of data:

* **live** — a shared in-process ring buffer per server, sampled on demand and by
  the background worker.  Feeds the streaming charts over the WebSocket.
* **history** — downsampled rows in ``server_metrics`` used for the 1h/24h/30d
  charts and for reports.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from datetime import timedelta
from typing import Any, Iterable

from ..backends import BackendError, get_backend
from ..db import db, iso, utcnow
from ..models import Role, Server, ServerMetric, ServerStatus

log = logging.getLogger("skvm.metrics")

LIVE_POINTS = 180          # 180 × 2s ≈ 6 minutes of live chart
RATE_WINDOW_S = 30
_LOCK = threading.Lock()
_LIVE: dict[str, deque] = {}
_LAST: dict[str, dict[str, Any]] = {}
_LAST_SAMPLE_AT: dict[str, float] = {}


def _series(uid: str) -> deque:
    with _LOCK:
        if uid not in _LIVE:
            _LIVE[uid] = deque(maxlen=LIVE_POINTS)
        return _LIVE[uid]


def _rate(uid: str, key: str, value: float | None, what: str) -> float:
    """Turn a cumulative counter into a per-second rate."""
    now = time.time()
    previous = _LAST.get(uid) or {}
    previous_value = previous.get(key)
    previous_at = previous.get("_at", now)
    _LAST[uid] = {**previous, key: value, "_at": now}
    if value is None or previous_value is None:
        return 0.0
    delta = float(value) - float(previous_value)
    elapsed = max(now - previous_at, 0.001)
    if delta < 0:  # counter reset (container restart)
        return 0.0
    return round(delta / elapsed / 1024, 2)  # KB/s


def _unreachable_row(server: Server, error: str = "") -> dict[str, Any]:
    """A stats row for a server the driver would not talk about.

    Every key the dashboards read has to be present: a missing ``disk_pct`` used to
    turn one unreachable container into a 500 on the server page.
    """
    uid = server.uid
    previous = _LAST.get(uid) or {}
    stored = server.last_metrics or {}
    row: dict[str, Any] = {
        "cpu_pct": 0.0,
        "ram_used_mb": 0.0,
        "ram_total_mb": float(server.ram_mb),
        "ram_pct": 0.0,
        "disk_used_gb": float(stored.get("disk_used_gb") or previous.get("disk_used_gb") or 0),
        "disk_total_gb": float(server.disk_gb),
        "disk_pct": None,
        "net_rx_kbps": 0.0,
        "net_tx_kbps": 0.0,
        "load1": 0.0,
        "procs": 0,
        "status": server.status,
        "sampled_at": iso(utcnow()),
        "offline": True,
    }
    if error:
        row["error"] = error
    return row


def collect(server: Server, cache_seconds: float = 2.0) -> dict[str, Any]:
    """Return live stats for one server (cached for a couple of seconds)."""
    uid = server.uid
    now = time.time()
    with _LOCK:
        cached_at = _LAST_SAMPLE_AT.get(uid, 0.0)
        cached = _LAST.get(uid, {})
    if cached and (now - cached_at) < cache_seconds and cached.get("status"):
        return cached

    if server.status != ServerStatus.RUNNING.value:
        stats = _unreachable_row(server)
        _remember(uid, stats)
        return stats

    backend = get_backend(server)
    try:
        raw = backend.stats(server.container_id)
    except BackendError as exc:
        log.debug("stats failed for %s: %s", uid, exc)
        # Same complete shape as the offline row: templates read these keys
        # unguarded, so a driver that cannot answer must not blank them out.
        stats = _unreachable_row(server, error=str(exc)[:160])
        _remember(uid, stats)
        return stats
    except Exception as exc:  # pragma: no cover - driver specific
        log.warning("unexpected stats error for %s: %s", uid, exc)
        return _unreachable_row(server, error=str(exc)[:160])

    rx_kbps = raw.get("net_rx_kbps")
    tx_kbps = raw.get("net_tx_kbps")
    if rx_kbps is None:
        rx_kbps = _rate(uid, "rx", raw.get("net_rx_bytes"), "rx")
    if tx_kbps is None:
        tx_kbps = _rate(uid, "tx", raw.get("net_tx_bytes"), "tx")

    ram_total = float(raw.get("ram_total_mb") or server.ram_mb or 1) or 1.0
    ram_used = float(raw.get("ram_used_mb") or 0)
    disk_total = float(raw.get("disk_total_gb") or server.disk_gb or 0)
    disk_used = float(raw.get("disk_used_gb") or 0)

    stats = {
        "cpu_pct": round(float(raw.get("cpu_pct") or 0), 2),
        "cpu_cores": float(raw.get("cpu_cores") or server.cpu_cores),
        "ram_used_mb": round(ram_used, 2),
        "ram_total_mb": round(ram_total, 2),
        "ram_pct": round(ram_used / ram_total * 100, 2),
        "disk_used_gb": round(disk_used, 3),
        "disk_total_gb": round(disk_total, 3),
        "disk_pct": round(disk_used / disk_total * 100, 2) if disk_total else None,
        "net_rx_kbps": round(float(rx_kbps or 0), 2),
        "net_tx_kbps": round(float(tx_kbps or 0), 2),
        "load1": round(float(raw.get("load1") or 0), 2),
        "procs": int(raw.get("procs") or 0),
        "status": raw.get("status") or server.status,
        "sampled_at": iso(utcnow()),
        "offline": False,
    }
    _remember(uid, stats)
    return stats


def _remember(uid: str, stats: dict[str, Any]) -> None:
    point = {
        "ts": stats.get("sampled_at") or iso(utcnow()),
        "t": time.time(),
        "cpu": stats.get("cpu_pct") or 0,
        "ram": stats.get("ram_pct") or 0,
        "disk": stats.get("disk_pct") or 0,
        "rx": stats.get("net_rx_kbps") or 0,
        "tx": stats.get("net_tx_kbps") or 0,
        "load": stats.get("load1") or 0,
    }
    with _LOCK:
        series = _LIVE.setdefault(uid, deque(maxlen=LIVE_POINTS))
        series.append(point)
        _LAST_SAMPLE_AT[uid] = time.time()
        _LAST[uid] = {**( _LAST.get(uid) or {}), **stats, "_at": time.time()}


def live_series(server: Server, points: int = LIVE_POINTS) -> list[dict[str, Any]]:
    series = list(_series(server.uid))
    if not series:
        collect(server)
        series = list(_series(server.uid))
    return series[-points:]


def record_history(server: Server, stats: dict[str, Any]) -> None:
    """Persist a downsampled sample (called by the worker)."""
    db.session.add(
        ServerMetric(
            server_id=server.id,
            cpu_pct=float(stats.get("cpu_pct") or 0),
            ram_used_mb=float(stats.get("ram_used_mb") or 0),
            ram_total_mb=float(stats.get("ram_total_mb") or 0),
            disk_used_gb=float(stats.get("disk_used_gb") or 0),
            disk_total_gb=float(stats.get("disk_total_gb") or 0),
            net_rx_kbps=float(stats.get("net_rx_kbps") or 0),
            net_tx_kbps=float(stats.get("net_tx_kbps") or 0),
            load1=float(stats.get("load1") or 0),
            procs=int(stats.get("procs") or 0),
        )
    )
    server.last_metrics = {
        "cpu_pct": stats.get("cpu_pct"),
        "ram_pct": stats.get("ram_pct"),
        "ram_used_mb": stats.get("ram_used_mb"),
        "disk_pct": stats.get("disk_pct"),
        "disk_used_gb": stats.get("disk_used_gb"),
        "net_rx_kbps": stats.get("net_rx_kbps"),
        "net_tx_kbps": stats.get("net_tx_kbps"),
        "load1": stats.get("load1"),
        "procs": stats.get("procs"),
        "ts": stats.get("sampled_at"),
    }
    interval = int(stats.get("_interval") or 0)
    if stats.get("status") == "running":
        server.uptime_seconds = int(server.uptime_seconds or 0) + interval
    else:
        server.uptime_seconds = 0


def history(server: Server, hours: int = 24, buckets: int = 96) -> list[dict[str, Any]]:
    """Return downsampled history rows for charts."""
    since = utcnow() - timedelta(hours=hours)
    rows = (
        db.session.query(ServerMetric)
        .filter(ServerMetric.server_id == server.id, ServerMetric.ts >= since)
        .order_by(ServerMetric.ts.asc())
        .all()
    )
    if not rows:
        return []
    step = max(1, len(rows) // buckets)
    out: list[dict[str, Any]] = []
    for index in range(0, len(rows), step):
        chunk = rows[index : index + step]
        if not chunk:
            continue
        count = len(chunk)
        out.append(
            {
                "ts": iso(chunk[-1].ts),
                "cpu": round(sum(r.cpu_pct for r in chunk) / count, 2),
                "ram": round(
                    sum((r.ram_used_mb / r.ram_total_mb * 100) if r.ram_total_mb else 0 for r in chunk) / count, 2
                ),
                "disk": round(
                    sum((r.disk_used_gb / r.disk_total_gb * 100) if r.disk_total_gb else 0 for r in chunk) / count, 2
                ),
                "rx": round(sum(r.net_rx_kbps for r in chunk) / count, 2),
                "tx": round(sum(r.net_tx_kbps for r in chunk) / count, 2),
                "load": round(sum(r.load1 for r in chunk) / count, 2),
            }
        )
    return out


def summary(servers: Iterable[Server], include_live: bool = True) -> dict[str, Any]:
    """Fleet-wide totals used by dashboards and the API."""
    servers = list(servers)
    totals = {
        "servers": len(servers),
        "running": sum(1 for s in servers if s.status == ServerStatus.RUNNING.value),
        "stopped": sum(1 for s in servers if s.status == ServerStatus.STOPPED.value),
        "suspended": sum(1 for s in servers if s.status == ServerStatus.SUSPENDED.value),
        "error": sum(1 for s in servers if s.status == ServerStatus.ERROR.value),
        "provisioning": sum(1 for s in servers if s.status == ServerStatus.PROVISIONING.value),
        "cpu_cores": round(sum(float(s.cpu_cores or 0) for s in servers), 1),
        "ram_mb": sum(int(s.ram_mb or 0) for s in servers),
        "disk_gb": sum(int(s.disk_gb or 0) for s in servers),
        "monthly_cost": round(sum(float(s.monthly_price or 0) for s in servers if s.auto_renew), 2),
        "bandwidth_used_gb": round(sum(float(s.bandwidth_used_mb or 0) for s in servers) / 1024, 2),
    }
    live_cpu = live_ram = 0.0
    sampled = 0
    if include_live:
        for server in servers:
            if server.status != ServerStatus.RUNNING.value:
                continue
            stats = collect(server, cache_seconds=8)
            if stats.get("offline"):
                continue
            live_cpu += float(stats.get("cpu_pct") or 0)
            live_ram += float(stats.get("ram_used_mb") or 0)
            sampled += 1
    totals["live_cpu_pct"] = round(live_cpu, 2)
    totals["live_ram_used_mb"] = round(live_ram, 1)
    totals["sampled"] = sampled
    totals["avg_cpu_pct"] = round(live_cpu / sampled, 2) if sampled else 0.0
    totals["avg_ram_pct"] = (
        round(
            sum(float(collect(s, cache_seconds=8).get("ram_pct") or 0) for s in servers if s.status == "running")
            / sampled,
            2,
        )
        if sampled
        else 0.0
    )
    open_events = db.session.query(ServerMetric).filter(ServerMetric.ts > utcnow() - timedelta(hours=1)).count()
    totals["samples_last_hour"] = int(open_events)
    return totals


def sparkline(server: Server, metric: str = "cpu", points: int = 40) -> list[float]:
    series = live_series(server, points=points)
    return [round(float(point.get(metric) or 0), 2) for point in series]


def prometheus_lines(servers: list[Server], node_stats: dict[str, Any] | None = None) -> str:
    """Render the /metrics endpoint (text exposition format 0.0.4)."""
    lines: list[str] = [
        "# HELP skvm_server_info Static information about a managed server",
        "# TYPE skvm_server_info gauge",
    ]
    for server in servers:
        owner = server.owner.username if server.owner else "?"
        labels = (
            f'server="{server.uid}",name="{server.name}",owner="{owner}",'
            f'image="{server.os_image}",status="{server.status}"'
        )
        lines.append(f"skvm_server_info{{{labels}}} 1")
    lines += [
        "# HELP skvm_server_cpu_percent Current CPU utilisation (%)",
        "# TYPE skvm_server_cpu_percent gauge",
        "# HELP skvm_server_memory_percent Current memory utilisation (%)",
        "# TYPE skvm_server_memory_percent gauge",
        "# HELP skvm_server_disk_percent Current disk utilisation (%)",
        "# TYPE skvm_server_disk_percent gauge",
        "# HELP skvm_server_network_rx_kbps Inbound throughput (KB/s)",
        "# TYPE skvm_server_network_rx_kbps gauge",
        "# HELP skvm_server_network_tx_kbps Outbound throughput (KB/s)",
        "# TYPE skvm_server_network_tx_kbps gauge",
        "# HELP skvm_server_up Whether the server is running (1) or not (0)",
        "# TYPE skvm_server_up gauge",
    ]
    for server in servers:
        if server.status != ServerStatus.RUNNING.value:
            label = f'server="{server.uid}",name="{server.name}"'
            lines.append(f"skvm_server_up{{{label}}} 0")
            continue
        stats = collect(server, cache_seconds=10)
        label = f'server="{server.uid}",name="{server.name}"'
        lines.append(f'skvm_server_cpu_percent{{{label}}} {float(stats.get("cpu_pct") or 0)}')
        lines.append(f'skvm_server_memory_percent{{{label}}} {float(stats.get("ram_pct") or 0)}')
        lines.append(f'skvm_server_disk_percent{{{label}}} {float(stats.get("disk_pct") or 0)}')
        lines.append(f'skvm_server_network_rx_kbps{{{label}}} {float(stats.get("net_rx_kbps") or 0)}')
        lines.append(f'skvm_server_network_tx_kbps{{{label}}} {float(stats.get("net_tx_kbps") or 0)}')
        lines.append(f"skvm_server_up{{{label}}} 1")
    if node_stats:
        lines += [
            "# HELP skvm_node_cpu_percent Host CPU utilisation (%)",
            "# TYPE skvm_node_cpu_percent gauge",
            f"skvm_node_cpu_percent {float(node_stats.get('cpu_pct') or 0)}",
            "# HELP skvm_node_memory_total_mb Host memory total (MB)",
            "# TYPE skvm_node_memory_total_mb gauge",
            f"skvm_node_memory_total_mb {float(node_stats.get('memory_total_mb') or 0)}",
            "# HELP skvm_node_memory_used_percent Host memory used (%)",
            "# TYPE skvm_node_memory_used_percent gauge",
            f"skvm_node_memory_used_percent {float(node_stats.get('memory_used_pct') or 0)}",
        ]
    return "\n".join(lines) + "\n"


def clear_live() -> None:
    with _LOCK:
        _LIVE.clear()
        _LAST.clear()
        _LAST_SAMPLE_AT.clear()


def can_view(user, server: Server) -> bool:
    return server.owner_id == user.id or user.can(Role.OPERATOR)
