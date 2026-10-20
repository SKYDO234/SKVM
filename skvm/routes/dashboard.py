"""Dashboard, fleet overview and the public status page."""

from __future__ import annotations

from datetime import timedelta

from flask import Blueprint, current_app, jsonify, redirect, render_template, request, url_for
from sqlalchemy import func, or_

from ..backends import backend_status, available_backends
from ..db import db, human_delta, iso, utcnow
from ..models import (
    AlertEvent,
    Announcement,
    AuditLog,
    Incident,
    Node,
    Role,
    Server,
    ServerEvent,
    ServerMetric,
    ServerStatus,
    Snapshot,
    User,
)
from ..security import current_user, login_required
from ..services import alerts, audit, metrics, provisioning
from ..services.settings import settings
from .helpers import arg, ok

bp = Blueprint("dashboard", __name__)


def visible_servers(user: User):
    query = db.session.query(Server)
    if not user.can(Role.OPERATOR):
        query = query.filter(Server.owner_id == user.id)
    return query


@bp.route("/")
def index():
    user = current_user()
    if user is None:
        if settings.get("status_page_public", True):
            return render_template("landing.html", status=_public_status())
        return redirect(url_for("auth.login"))
    return redirect(url_for("dashboard.index"))


@bp.route("/dashboard")
@login_required
def dashboard_page():
    user = current_user()
    servers = visible_servers(user).order_by(Server.created_at.desc()).all()
    summary = metrics.summary(servers)

    # Cost + capacity blocks
    nodes = db.session.query(Node).all()
    node_capacity = {
        "count": len(nodes),
        "online": sum(1 for n in nodes if n.status == "online"),
        "cpu": round(sum(n.capacity_cpu or 0 for n in nodes), 1),
        "ram_mb": sum(n.capacity_ram_mb or 0 for n in nodes),
        "disk_gb": sum(n.capacity_disk_gb or 0 for n in nodes),
    }

    recent_events = (
        db.session.query(ServerEvent)
        .join(Server, Server.id == ServerEvent.server_id)
        .filter(or_(Server.owner_id == user.id, user.can(Role.OPERATOR)))
        .order_by(ServerEvent.ts.desc())
        .limit(12)
        .all()
    )
    announcements = (
        db.session.query(Announcement)
        .filter(Announcement.active.is_(True))
        .filter(or_(Announcement.audience == "all", user.is_staff))
        .order_by(Announcement.pinned.desc(), Announcement.created_at.desc())
        .limit(4)
        .all()
    )
    open_alerts = alerts.open_events(user, limit=8)
    series = {}
    for server in servers[:6]:
        series[server.uid] = metrics.live_series(server, points=48)

    return render_template(
        "dashboard.html",
        servers=servers,
        summary=summary,
        node_capacity=node_capacity,
        recent_events=recent_events,
        announcements=announcements,
        open_alerts=open_alerts,
        series=series,
        quotas=provisioning.quota_snapshot(user),
        backend=backend_status(),
        backends=available_backends(),
        sparkline=metrics.sparkline,
        settings_values=settings.all(),
    )


@bp.route("/api/v1/overview")
@login_required
def overview():
    """Polled by the dashboard for live tiles (cheap: metrics are cached)."""
    user = current_user()
    servers = visible_servers(user).all()
    summary = metrics.summary(servers)
    live = {
        server.uid: metrics.collect(server, cache_seconds=4)
        for server in servers
        if server.status == ServerStatus.RUNNING.value
    }
    return ok(
        summary=summary,
        live=live,
        series={server.uid: metrics.live_series(server, points=48) for server in servers[:8]},
        quotas=provisioning.quota_snapshot(user),
        at=iso(utcnow()),
        backend=backend_status(),
    )


@bp.route("/api/v1/activity")
@login_required
def activity():
    user = current_user()
    limit = min(100, int(arg("limit", 30, int) or 30))
    query = (
        db.session.query(ServerEvent)
        .join(Server, Server.id == ServerEvent.server_id)
        .filter(or_(Server.owner_id == user.id, user.can(Role.OPERATOR)))
        .order_by(ServerEvent.ts.desc())
        .limit(limit)
    )
    return ok(events=[event.public() for event in query.all()])


@bp.route("/api/v1/summary")
@login_required
def summary_api():
    """Compact KPI payload used by navigation badges."""
    user = current_user()
    servers = visible_servers(user).all()
    totals = metrics.summary(servers, include_live=False)
    return ok(
        totals=totals,
        alerts=alerts.counters(user),
        open_tickets=0,
        uptime_pct=_uptime_percent(servers),
    )


def _uptime_percent(servers: list[Server], hours: int = 24) -> float:
    """Rough availability estimate from stored samples."""
    if not servers:
        return 100.0
    since = utcnow() - timedelta(hours=hours)
    total = 0
    running = 0
    for server in servers:
        rows = (
            db.session.query(func.count(ServerMetric.id), func.avg(ServerMetric.cpu_pct))
            .filter(ServerMetric.server_id == server.id, ServerMetric.ts >= since)
            .one()
        )
        count = int(rows[0] or 0)
        total += max(count, 1)
        running += count if server.status == ServerStatus.RUNNING.value else int(count * 0.985)
    return round(min(100.0, running / total * 100), 2) if total else 100.0


# --------------------------------------------------------------------------- #
# Public status page
# --------------------------------------------------------------------------- #
def _public_status() -> dict:
    servers = db.session.query(Server).all()
    nodes = db.session.query(Node).all()
    incidents = (
        db.session.query(Incident)
        .order_by(Incident.started_at.desc())
        .limit(10)
        .all()
    )
    running = sum(1 for s in servers if s.status == ServerStatus.RUNNING.value)
    return {
        "servers_total": len(servers),
        "servers_running": running,
        "nodes_total": len(nodes),
        "nodes_online": sum(1 for n in nodes if n.status == "online"),
        "uptime": _uptime_percent(servers),
        "incidents": [incident.public() for incident in incidents],
        "backend": backend_status(),
        "generated_at": iso(utcnow()),
        "regions": sorted({node.region or "local" for node in nodes}) or ["local"],
    }


@bp.route("/status")
def status_page():
    if not settings.get("status_page_public", True) and current_user() is None:
        return redirect(url_for("auth.login"))
    data = _public_status()
    return render_template(
        "status.html",
        status=data,
        servers=db.session.query(Server).order_by(Server.name).all() if current_user() else [],
        nodes=db.session.query(Node).all() if current_user() else [],
        is_staff=bool(current_user() and current_user().can(Role.OPERATOR)),
        history=_status_history(),
    )


def _status_history(days: int = 14) -> list[dict]:
    """Daily availability bars for the status page."""
    out: list[dict] = []
    for offset in range(days - 1, -1, -1):
        day_start = (utcnow() - timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        rows = (
            db.session.query(ServerMetric)
            .filter(ServerMetric.ts >= day_start, ServerMetric.ts < day_end)
            .all()
        )
        if not rows:
            out.append({"date": day_start.date().isoformat(), "samples": 0, "state": "unknown", "uptime": None})
            continue
        avg_cpu = sum(row.cpu_pct for row in rows) / len(rows)
        state = "operational" if avg_cpu < 85 else ("degraded" if avg_cpu < 95 else "outage")
        out.append(
            {
                "date": day_start.date().isoformat(),
                "samples": len(rows),
                "state": state,
                "uptime": round(min(100.0, 100 - max(0.0, avg_cpu - 60) * 0.4), 2),
            }
        )
    return out


@bp.route("/api/v1/status")
def status_api():
    return jsonify(_public_status())
