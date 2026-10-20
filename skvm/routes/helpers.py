"""Route helpers shared by every blueprint."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Iterable

from flask import Flask, abort, g, jsonify, redirect, request, url_for
from markupsafe import Markup, escape

from ..db import human_delta, iso, parse_dt, utcnow
from ..models import Role, Server, ServerStatus, User
from ..security import current_user


# --------------------------------------------------------------------------- #
# JSON responses
# --------------------------------------------------------------------------- #
def ok(**payload: Any):
    body = {"ok": True, **payload}
    return jsonify(body)


def fail(message: str, status: int = 400, **extra):
    return jsonify({"ok": False, "error": message, "status": status, **extra}), status


def body() -> dict[str, Any]:
    """Tolerant JSON/form body reader."""
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if request.form:
        return {key: value for key, value in request.form.items()}
    return {}


def arg(name: str, default: Any = None, cast: Any = None) -> Any:
    value = request.args.get(name, default)
    if value is None or cast is None:
        return value
    try:
        if cast is bool:
            return str(value).lower() in ("1", "true", "yes", "on")
        return cast(value)
    except (TypeError, ValueError):
        return default


def paging(default_size: int = 25, maximum: int = 200) -> tuple[int, int]:
    page = max(1, int(arg("page", 1, int) or 1))
    size = min(maximum, max(1, int(arg("per_page", default_size, int) or default_size)))
    return page, size


def paginated(query, page: int, size: int, serializer=None) -> dict[str, Any]:
    total = query.count()
    rows = query.limit(size).offset((page - 1) * size).all()
    items = [serializer(row) if serializer else row for row in rows]
    pages = max(1, math.ceil(total / size))
    return {
        "items": items,
        "page": page,
        "per_page": size,
        "total": total,
        "pages": pages,
        "has_next": page < pages,
        "has_prev": page > 1,
    }


# --------------------------------------------------------------------------- #
# Lookups
# --------------------------------------------------------------------------- #
def get_server(uid: str, *, require_owner: bool = False, staff_ok: bool = True) -> Server:
    from ..db import db
    from ..security import role_required  # noqa: F401  (kept for parity)

    from ..models import Server as ServerModel

    key = (uid or "").strip()
    server = (
        db.session.query(ServerModel)
        .filter((ServerModel.uid == key) | (ServerModel.id == _as_int(key)))
        .one_or_none()
    )
    if server is None:
        abort(404 if not request.path.startswith("/api/") else 404)
    user = current_user()
    if user is None:
        return server
    allowed = server.owner_id == user.id or (staff_ok and user.can(Role.OPERATOR))
    if require_owner and server.owner_id != user.id and not user.can(Role.ADMIN):
        abort(403)
    if not allowed:
        abort(403)
    g.server = server
    return server


def _as_int(value: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def get_user(user_id: int) -> User:
    from ..db import db

    user = db.session.get(User, user_id)
    if user is None:
        abort(404)
    return user


# --------------------------------------------------------------------------- #
# Presentation helpers
# --------------------------------------------------------------------------- #
def human_size(value: float | int | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB", "PB"):
        if abs(size) < 1024 or unit == "PB":
            rendered = f"{size:.0f} {unit}" if unit in ("B", "KB") or size >= 100 else f"{size:.1f} {unit}"
            return f"{rendered}{suffix}"
        size /= 1024
    return f"{size:.1f} PB{suffix}"


def human_uptime(seconds: int | float | None) -> str:
    if not seconds:
        return "—"
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def fmt_dt(value: Any, pattern: str = "%d %b %Y · %H:%M") -> str:
    parsed = parse_dt(value)
    if parsed is None:
        return "—"
    return parsed.astimezone(timezone.utc).strftime(pattern)


def percent_class(value: float | None, warn: float = 70, critical: float = 90) -> str:
    if value is None:
        return "muted"
    if value >= critical:
        return "critical"
    if value >= warn:
        return "warn"
    return "good"


def status_meta(status: str) -> dict[str, str]:
    mapping = {
        ServerStatus.RUNNING.value: {"label": "Running", "tone": "good", "icon": "power"},
        ServerStatus.STOPPED.value: {"label": "Stopped", "tone": "muted", "icon": "power-off"},
        ServerStatus.PROVISIONING.value: {"label": "Provisioning", "tone": "info", "icon": "loader"},
        ServerStatus.RESTARTING.value: {"label": "Restarting", "tone": "info", "icon": "refresh"},
        ServerStatus.SUSPENDED.value: {"label": "Suspended", "tone": "warn", "icon": "pause"},
        ServerStatus.ERROR.value: {"label": "Error", "tone": "critical", "icon": "alert"},
        ServerStatus.DELETING.value: {"label": "Deleting", "tone": "muted", "icon": "trash"},
    }
    return mapping.get(status, {"label": str(status).title(), "tone": "muted", "icon": "server"})


def register_template_globals(app: Flask) -> None:
    app.jinja_env.filters["human_size"] = human_size
    app.jinja_env.filters["human_uptime"] = human_uptime
    app.jinja_env.filters["human_delta"] = human_delta
    app.jinja_env.filters["fmt_dt"] = fmt_dt
    app.jinja_env.filters["pct_class"] = percent_class
    app.jinja_env.filters["status_meta"] = status_meta
    app.jinja_env.filters["iso"] = lambda value: iso(parse_dt(value) if not isinstance(value, datetime) else value)
    app.jinja_env.globals.update(
        human_size=human_size,
        human_uptime=human_uptime,
        human_delta=human_delta,
        fmt_dt=fmt_dt,
        percent_class=percent_class,
        status_meta=status_meta,
        json=__import__("json"),
    )


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return request.accept_mimetypes.best == "application/json"


def back(default_endpoint: str, **values):
    target = request.form.get("next") or request.args.get("next")
    if target and target.startswith("/"):
        return redirect(target)
    return redirect(url_for(default_endpoint, **values))


def flash_json(message: str, level: str = "success"):
    """Small helper for AJAX endpoints that also want a toast message."""
    return {"ok": True, "message": message, "level": level}


def escape_html(value: Any) -> Markup:
    return escape(str(value if value is not None else ""))


def chunked(items: Iterable, size: int) -> list[list]:
    bucket: list[list] = []
    current: list = []
    for item in items:
        current.append(item)
        if len(current) >= size:
            bucket.append(current)
            current = []
    if current:
        bucket.append(current)
    return bucket
