"""Audit trail.

Every state-changing action in the panel funnels through :func:`record` so the
admin "Audit" view and the per-server activity feed stay complete.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from flask import g, has_request_context, request

from ..db import iso, utcnow

log = logging.getLogger("skvm.audit")


def _actor() -> tuple[int | None, str]:
    if has_request_context():
        user = getattr(g, "current_user", None) or getattr(g, "_skvm_user", None)
        if user is not None:
            return user.id, user.username
        token = getattr(g, "api_token", None)
        if token is not None:
            return token.user_id, f"token:{token.prefix}"
    return None, "system"


def _context() -> dict[str, Any]:
    if not has_request_context():
        return {"ip": "", "user_agent": "", "channel": "system"}
    channel = "api" if request.path.startswith("/api/") else "web"
    return {
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip(),
        "user_agent": (request.user_agent.string or "")[:250],
        "channel": channel,
    }


def record(
    action: str,
    *,
    target_type: str = "",
    target_id: Any = "",
    target_label: str = "",
    result: str = "ok",
    meta: dict[str, Any] | None = None,
    actor: str | None = None,
    actor_id: int | None = None,
    channel: str | None = None,
    commit: bool = True,
) -> None:
    """Persist an audit entry. Never raises — auditing must not break actions."""
    try:
        from ..db import db
        from ..models import AuditLog

        ctx = _context()
        auto_id, auto_actor = _actor()
        entry = AuditLog(
            actor_id=actor_id if actor_id is not None else auto_id,
            actor=actor or auto_actor,
            action=action,
            target_type=target_type,
            target_id=str(target_id or ""),
            target_label=target_label[:128],
            result=result,
            ip=ctx["ip"],
            user_agent=ctx["user_agent"],
            channel=channel or ctx["channel"],
            meta=meta or {},
        )
        db.session.add(entry)
        if commit:
            db.session.flush()
        log.info("audit %s %s %s -> %s", entry.actor, action, target_label or target_id, result)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("audit record failed: %s", exc)


def server_event(
    server,
    action: str,
    message: str = "",
    status: str = "ok",
    commit: bool = True,
) -> None:
    """Append to a server's activity timeline."""
    try:
        from ..db import db
        from ..models import ServerEvent

        actor_id, actor = _actor()
        db.session.add(
            ServerEvent(
                server_id=server.id,
                action=action,
                status=status,
                message=message[:255],
                actor=actor,
                actor_id=actor_id,
            )
        )
        if commit:
            db.session.flush()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("server_event failed: %s", exc)


def recent(limit: int = 50, action_prefix: str | None = None, actors_only: bool = False):
    from ..db import db
    from ..models import AuditLog

    query = db.session.query(AuditLog).order_by(AuditLog.ts.desc())
    if action_prefix:
        query = query.filter(AuditLog.action.like(f"{action_prefix}%"))
    if actors_only:
        query = query.filter(AuditLog.actor_id.isnot(None))
    return query.limit(limit).all()


def stats(days: int = 7) -> dict[str, Any]:
    from sqlalchemy import func

    from ..db import db
    from ..models import AuditLog

    since = utcnow() - timedelta(days=days)
    total = db.session.query(func.count(AuditLog.id)).filter(AuditLog.ts >= since).scalar() or 0
    failures = (
        db.session.query(func.count(AuditLog.id))
        .filter(AuditLog.ts >= since, AuditLog.result != "ok")
        .scalar()
        or 0
    )
    by_action = (
        db.session.query(AuditLog.action, func.count(AuditLog.id))
        .filter(AuditLog.ts >= since)
        .group_by(AuditLog.action)
        .order_by(func.count(AuditLog.id).desc())
        .limit(8)
        .all()
    )
    return {
        "window_days": days,
        "total": int(total),
        "failures": int(failures),
        "top_actions": [{"action": a, "count": int(c)} for a, c in by_action],
        "since": iso(since),
    }


def export_csv(days: int = 30, limit: int = 5000) -> str:
    """Audit export used by the admin panel (and compliance requests)."""
    from ..db import db
    from ..models import AuditLog

    since = utcnow() - timedelta(days=days)
    rows = (
        db.session.query(AuditLog)
        .filter(AuditLog.ts >= since)
        .order_by(AuditLog.ts.desc())
        .limit(limit)
        .all()
    )
    header = "timestamp,actor,action,target_type,target_id,target_label,result,ip,channel\n"
    lines = [header]
    for row in rows:
        safe = lambda v: '"' + str(v or "").replace('"', "'") + '"'  # noqa: E731
        lines.append(
            ",".join(
                [
                    iso(row.ts) or "",
                    safe(row.actor),
                    safe(row.action),
                    safe(row.target_type),
                    safe(row.target_id),
                    safe(row.target_label),
                    safe(row.result),
                    safe(row.ip),
                    safe(row.channel),
                ]
            )
            + "\n"
        )
    return "".join(lines)


def parse_since(raw: str | None, default_days: int = 7) -> datetime:
    from ..db import parse_dt

    parsed = parse_dt(raw)
    return parsed or (utcnow() - timedelta(days=default_days))
