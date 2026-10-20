"""Health, Prometheus metrics, manifests and small "system" endpoints."""

from __future__ import annotations

import json
import platform
import time
from datetime import timedelta

from flask import Blueprint, Response, current_app, jsonify, render_template, request

from ..backends import available_backends, backend_status
from ..db import db, iso, utcnow
from ..models import ApiToken, Node, Server, ServerStatus, User
from ..security import current_user
from ..services import metrics
from ..services.settings import settings

bp = Blueprint("system", __name__)

STARTED_AT = time.time()


@bp.route("/healthz")
def healthz():
    ok_db, db_detail = db.health()
    body = {
        "status": "ok" if ok_db else "degraded",
        "version": current_app.config.get("SKVM_VERSION"),
        "uptime_seconds": round(time.time() - STARTED_AT, 1),
        "database": {"ok": ok_db, "detail": db_detail},
        "backend": backend_status(),
        "time": iso(utcnow()),
    }
    return jsonify(body), (200 if ok_db else 503)


@bp.route("/readyz")
def readyz():
    ok_db, _detail = db.health()
    return jsonify({"ready": ok_db}), (200 if ok_db else 503)


@bp.route("/version")
def version():
    return jsonify(
        panel=current_app.config.get("PANEL_NAME"),
        version=current_app.config.get("SKVM_VERSION"),
        python=platform.python_version(),
        env=current_app.config.get("ENV_NAME"),
        backend=current_app.config.get("BACKEND"),
    )


@bp.route("/manifest.webmanifest")
def manifest():
    panel = current_app.config.get("PANEL_NAME", "SKVM")
    payload = {
        "name": f"{panel} Cloud Control",
        "short_name": panel,
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#070b16",
        "theme_color": "#7c5cff",
        "description": current_app.config.get("PANEL_TAGLINE"),
        "icons": [
            {"src": "/static/img/icon.svg", "sizes": "any", "type": "image/svg+xml", "purpose": "any"},
        ],
    }
    return Response(json.dumps(payload), mimetype="application/manifest+json")


@bp.route("/metrics")
def prometheus():
    """Prometheus scrape endpoint (protect it with a token or network policy)."""
    if not current_app.config.get("ENABLE_PROMETHEUS", True):
        return "prometheus metrics disabled", 404
    token = current_app.config.get("PROMETHEUS_TOKEN") or ""
    if token:
        supplied = request.args.get("token") or request.headers.get("Authorization", "").removeprefix("Bearer ")
        if supplied != token:
            return "forbidden", 403
    servers = db.session.query(Server).all()
    backend = backend_status()
    payload = metrics.prometheus_lines(servers, node_stats=backend.get("host"))
    return Response(payload, mimetype="text/plain; version=0.0.4; charset=utf-8")


@bp.route("/api/v1/system/health")
def system_health():
    """Rich health view for the operations screen."""
    ok_db, db_detail = db.health()
    servers = db.session.query(Server).all()
    nodes = db.session.query(Node).all()
    return jsonify(
        ok=ok_db,
        uptime_seconds=round(time.time() - STARTED_AT, 1),
        database={"ok": ok_db, "detail": db_detail},
        backend=backend_status(),
        counts={
            "servers": len(servers),
            "running": sum(1 for s in servers if s.status == ServerStatus.RUNNING.value),
            "users": db.session.query(User).count(),
            "nodes": len(nodes),
            "tokens": db.session.query(ApiToken).filter(ApiToken.revoked_at.is_(None)).count(),
        },
        maintenance=settings.maintenance(),
        version=current_app.config.get("SKVM_VERSION"),
    )


@bp.route("/robots.txt")
def robots():
    return Response("User-agent: *\nDisallow: /admin\nDisallow: /api\nDisallow: /account\n", mimetype="text/plain")


@bp.route("/favicon.ico")
def favicon():
    return current_app.send_static_file("img/favicon.svg")


@bp.app_template_filter("filesize")
def filesize(value):
    from .helpers import human_size

    return human_size(value)
