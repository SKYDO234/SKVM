"""Public REST API (v1) — token authenticated, scope-controlled, documented.

Everything under ``/api/v1`` is JSON-only and usable from scripts, Terraform
providers, CI pipelines and monitoring systems.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request
from sqlalchemy import or_

from ..backends import BackendError, available_backends, backend_status, get_backend
from ..backends.images import IMAGE_CATALOG, PLAN_PRESETS, REGIONS, image_info
from ..db import db, iso, utcnow
from ..models import (
    AlertEvent,
    AlertRule,
    AuditLog,
    Backup,
    Node,
    Role,
    Server,
    ServerEvent,
    ServerStatus,
    Snapshot,
    TokenScope,
    User,
)
from ..security import api_required, current_user
from ..services import alerts as alert_service
from ..services import audit, metrics, provisioning
from ..services.settings import settings
from .helpers import arg, body, fail, get_server, ok, paging, paginated

log = logging.getLogger("skvm.api")

bp = Blueprint("api", __name__, url_prefix="/api/v1")


def _scope_user():
    from flask import g

    return getattr(g, "current_user", None) or current_user()


def _scoped_servers(user: User):
    query = db.session.query(Server)
    if not user.can(Role.OPERATOR):
        query = query.filter(Server.owner_id == user.id)
    return query


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #
@bp.route("/")
@bp.route("/meta")
def meta():
    return ok(
        panel=current_app.config.get("PANEL_NAME"),
        version=current_app.config.get("SKVM_VERSION"),
        api="v1",
        time=iso(utcnow()),
        endpoints=_endpoint_catalog(),
        backends=available_backends(),
    )


def _endpoint_catalog() -> list[dict]:
    return [
        {"method": "GET", "path": "/api/v1/meta", "scope": None, "desc": "Panel metadata and endpoint index"},
        {"method": "GET", "path": "/api/v1/servers", "scope": "read", "desc": "List servers"},
        {"method": "POST", "path": "/api/v1/servers", "scope": "write", "desc": "Create a server"},
        {"method": "GET", "path": "/api/v1/servers/<uid>", "scope": "read", "desc": "Server detail"},
        {"method": "DELETE", "path": "/api/v1/servers/<uid>", "scope": "write", "desc": "Delete a server"},
        {"method": "POST", "path": "/api/v1/servers/<uid>/power", "scope": "power", "desc": "start|stop|restart|suspend|resume"},
        {"method": "GET", "path": "/api/v1/servers/<uid>/metrics", "scope": "read", "desc": "Live + historical metrics"},
        {"method": "GET", "path": "/api/v1/servers/<uid>/events", "scope": "read", "desc": "Activity timeline"},
        {"method": "POST", "path": "/api/v1/servers/<uid>/exec", "scope": "write", "desc": "Run a command"},
        {"method": "GET", "path": "/api/v1/servers/<uid>/snapshots", "scope": "read", "desc": "List snapshots"},
        {"method": "POST", "path": "/api/v1/servers/<uid>/snapshots", "scope": "write", "desc": "Create a snapshot"},
        {"method": "POST", "path": "/api/v1/servers/<uid>/snapshots/<id>/restore", "scope": "write", "desc": "Restore"},
        {"method": "GET", "path": "/api/v1/servers/<uid>/backups", "scope": "read", "desc": "List backups"},
        {"method": "POST", "path": "/api/v1/servers/<uid>/backups", "scope": "write", "desc": "Create a backup"},
        {"method": "GET", "path": "/api/v1/images", "scope": "read", "desc": "OS image catalog"},
        {"method": "GET", "path": "/api/v1/plans", "scope": "read", "desc": "Plan presets and pricing"},
        {"method": "GET", "path": "/api/v1/nodes", "scope": "read", "desc": "Hypervisor nodes"},
        {"method": "GET", "path": "/api/v1/account", "scope": "read", "desc": "Your account + quotas"},
        {"method": "GET", "path": "/api/v1/alerts/rules", "scope": "read", "desc": "Alert rules"},
        {"method": "GET", "path": "/api/v1/alerts/events", "scope": "read", "desc": "Alert events"},
        {"method": "GET", "path": "/api/v1/audit", "scope": "admin", "desc": "Audit log (staff only)"},
        {"method": "GET", "path": "/api/v1/summary", "scope": "read", "desc": "Fleet totals"},
    ]


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #
@bp.route("/images")
@api_required(TokenScope.READ)
def images():
    backend = get_backend()
    catalog = {entry["id"]: entry for entry in backend.catalog()}
    rows = []
    for key, meta in IMAGE_CATALOG.items():
        rows.append({**image_info(key), "availability": catalog.get(key, {})})
    return ok(images=rows)


@bp.route("/plans")
@api_required(TokenScope.READ)
def plans():
    return ok(plans=PLAN_PRESETS, regions=REGIONS, currency=settings.get("currency", "USD"))


@bp.route("/nodes")
@api_required(TokenScope.READ)
def nodes():
    rows = db.session.query(Node).order_by(Node.priority).all()
    return ok(nodes=[row.public() for row in rows], backend=backend_status())


# --------------------------------------------------------------------------- #
# Account
# --------------------------------------------------------------------------- #
@bp.route("/account")
@api_required(TokenScope.READ)
def account():
    from flask import g

    user = _scope_user()
    token = getattr(g, "api_token", None)
    servers = _scoped_servers(user).all()
    return ok(
        user=user.public(),
        token=token.public() if token else None,
        quotas=provisioning.quota_snapshot(user),
        totals=metrics.summary(servers, include_live=False),
    )


@bp.route("/summary")
@api_required(TokenScope.READ)
def summary():
    user = _scope_user()
    servers = _scoped_servers(user).all()
    return ok(totals=metrics.summary(servers), alerts=alert_service.counters(user))


# --------------------------------------------------------------------------- #
# Servers
# --------------------------------------------------------------------------- #
@bp.route("/servers", methods=["GET"])
@api_required(TokenScope.READ)
def list_servers():
    user = _scope_user()
    query = _scoped_servers(user)
    if arg("status"):
        query = query.filter(Server.status == arg("status"))
    if arg("q"):
        needle = f"%{arg('q')}%"
        query = query.filter(or_(Server.name.ilike(needle), Server.uid.ilike(needle), Server.ip_address.ilike(needle)))
    if arg("tag"):
        query = query.filter(Server.tags.contains(arg("tag")))  # type: ignore[attr-defined]
    page, size = paging(25, 200)
    data = paginated(
        query.order_by(Server.created_at.desc()), page, size,
        serializer=lambda row: row.public(detail=False),
    )
    return ok(**data)


@bp.route("/servers", methods=["POST"])
@api_required(TokenScope.WRITE)
def create_server():
    user = _scope_user()
    data = body()
    os_image = str(data.get("os_image") or settings.get("default_os", "ubuntu-24.04"))
    if os_image not in IMAGE_CATALOG:
        return fail("Unknown os_image", 400, available=list(IMAGE_CATALOG))
    try:
        server = provisioning.create_server(
            user,
            name=str(data.get("name") or "").strip(),
            os_image=os_image,
            cpu=float(data.get("cpu") or current_app.config["DEFAULT_CPU"]),
            ram_mb=int(data.get("ram_mb") or current_app.config["DEFAULT_RAM_MB"]),
            disk_gb=int(data.get("disk_gb") or current_app.config["DEFAULT_DISK_GB"]),
            swap_mb=int(data.get("swap_mb") or 0),
            server_type=str(data.get("server_type") or "container"),
            hostname=str(data.get("hostname") or "").strip(),
            root_password=str(data.get("root_password") or ""),
            ssh_keys=[str(k) for k in (data.get("ssh_keys") or [])],
            tags=[str(t) for t in (data.get("tags") or [])],
            plan=str(data.get("plan") or "custom"),
            node_id=int(data["node_id"]) if data.get("node_id") else None,
            expires_in_days=int(data["expires_in_days"]) if data.get("expires_in_days") else 30,
            notes=str(data.get("notes") or ""),
        )
    except provisioning.ProvisioningError as exc:
        db.session.rollback()
        return fail(str(exc), 400, code=exc.code)
    except BackendError as exc:
        db.session.rollback()
        return fail(str(exc), 502, code=exc.code)
    db.session.commit()
    return ok(server=server.public(detail=True, include_secrets=True), status=201), 201


@bp.route("/servers/<uid>", methods=["GET"])
@api_required(TokenScope.READ)
def get_server_api(uid: str):
    server = get_server(uid)
    return ok(server=server.public(detail=True), stats=metrics.collect(server, cache_seconds=2))


@bp.route("/servers/<uid>", methods=["DELETE"])
@api_required(TokenScope.WRITE)
def delete_server_api(uid: str):
    server = get_server(uid)
    name = server.name
    try:
        provisioning.delete_server(server, purge_data=bool(arg("purge", True, bool)))
    except BackendError as exc:
        return fail(str(exc), 502, code=exc.code)
    db.session.commit()
    return ok(message=f"{name} deleted")


@bp.route("/servers/<uid>", methods=["PATCH"])
@api_required(TokenScope.WRITE)
def patch_server_api(uid: str):
    server = get_server(uid)
    data = body()
    if "name" in data:
        try:
            server.name = provisioning.validate_name(str(data["name"]))
        except provisioning.ProvisioningError as exc:
            return fail(str(exc), 400)
    if "tags" in data:
        server.tags = [str(tag)[:24] for tag in (data["tags"] if isinstance(data["tags"], list) else [])]
    if "notes" in data:
        server.notes = str(data["notes"])[:4000]
    if "auto_renew" in data:
        server.auto_renew = bool(data["auto_renew"])
    audit.record("api.server_update", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"fields": sorted(data.keys())})
    db.session.commit()
    return ok(server=server.public(detail=True))


@bp.route("/servers/<uid>/power", methods=["POST"])
@api_required(TokenScope.POWER)
def power_api(uid: str):
    server = get_server(uid)
    data = body()
    action = str(data.get("action") or "").lower()
    if action not in ("start", "stop", "restart", "suspend", "resume"):
        return fail("action must be one of start|stop|restart|suspend|resume", 400)
    try:
        provisioning.power_action(server, action, actor=f"api:{_scope_user().username}")
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(server=server.public(detail=True), action=action)


@bp.route("/servers/<uid>/metrics")
@api_required(TokenScope.READ)
def metrics_api(uid: str):
    server = get_server(uid)
    window = arg("window", "live")
    if window == "live":
        return ok(stats=metrics.collect(server, cache_seconds=2), series=metrics.live_series(server, points=60))
    hours = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}.get(window)
    if hours is None:
        return fail("window must be one of live|1h|6h|24h|7d|30d", 400)
    return ok(stats=metrics.collect(server, cache_seconds=5),
              history=metrics.history(server, hours=hours, buckets=120), window=window)


@bp.route("/servers/<uid>/exec", methods=["POST"])
@api_required(TokenScope.WRITE)
def exec_api(uid: str):
    server = get_server(uid)
    data = body()
    command = str(data.get("command") or "").strip()
    if not command:
        return fail("Provide a command", 400)
    backend = get_backend(server)
    try:
        result = backend.exec_command(server.container_id, command, timeout=int(data.get("timeout") or 30))
    except BackendError as exc:
        return fail(str(exc), 502, code=exc.code)
    audit.record("api.exec", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"command": command[:200], "exit_code": result.exit_code}, result="warn")
    audit.server_event(server, "exec", f"API command exited {result.exit_code}")
    db.session.commit()
    return ok(exit_code=result.exit_code, output=result.output[-20000:], ok=result.ok)


@bp.route("/servers/<uid>/events")
@api_required(TokenScope.READ)
def events_api(uid: str):
    server = get_server(uid)
    page, size = paging(50, 200)
    query = db.session.query(ServerEvent).filter(ServerEvent.server_id == server.id).order_by(ServerEvent.ts.desc())
    data = paginated(query, page, size, serializer=lambda row: row.public())
    return ok(**data)


@bp.route("/servers/<uid>/snapshots", methods=["GET", "POST"])
@api_required(TokenScope.READ)
def snapshots_api(uid: str):
    from flask import g

    server = get_server(uid)
    if request.method == "GET":
        rows = db.session.query(Snapshot).filter(Snapshot.server_id == server.id).order_by(Snapshot.created_at.desc()).all()
        return ok(snapshots=[row.public() for row in rows])

    token = getattr(g, "api_token", None)
    if token is not None and not token.has_scope(TokenScope.WRITE):
        return fail("Token lacks the 'write' scope", 403, code="insufficient_scope")
    data = body()
    try:
        snapshot = provisioning.create_snapshot(
            server,
            str(data.get("name") or f"api-{utcnow().strftime('%Y%m%d-%H%M%S')}"),
            str(data.get("description") or ""),
            actor=f"api:{_scope_user().username}",
        )
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(snapshot=snapshot.public(), status=201), 201


@bp.route("/servers/<uid>/snapshots/<int:snapshot_id>/restore", methods=["POST"])
@api_required(TokenScope.WRITE)
def snapshot_restore_api(uid: str, snapshot_id: int):
    server = get_server(uid)
    snapshot = db.session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.server_id != server.id:
        return fail("Snapshot not found", 404)
    try:
        provisioning.restore_snapshot(server, snapshot)
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(server=server.public(detail=True), message="Snapshot restored")


@bp.route("/servers/<uid>/snapshots/<int:snapshot_id>", methods=["DELETE"])
@api_required(TokenScope.WRITE)
def snapshot_delete_api(uid: str, snapshot_id: int):
    server = get_server(uid)
    snapshot = db.session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.server_id != server.id:
        return fail("Snapshot not found", 404)
    provisioning.delete_snapshot(server, snapshot)
    db.session.commit()
    return ok(message="Snapshot deleted")


@bp.route("/servers/<uid>/backups", methods=["GET", "POST"])
@api_required(TokenScope.READ)
def backups_api(uid: str):
    from flask import g

    server = get_server(uid)
    if request.method == "GET":
        rows = db.session.query(Backup).filter(Backup.server_id == server.id).order_by(Backup.created_at.desc()).all()
        return ok(backups=[row.public() for row in rows])
    token = getattr(g, "api_token", None)
    if token is not None and not token.has_scope(TokenScope.WRITE):
        return fail("Token lacks the 'write' scope", 403, code="insufficient_scope")
    try:
        backup = provisioning.create_backup(
            server, str(body().get("name") or ""), actor=f"api:{_scope_user().username}"
        )
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(backup=backup.public(), status=201), 201


@bp.route("/servers/<uid>/backups/<int:backup_id>", methods=["DELETE"])
@api_required(TokenScope.WRITE)
def backup_delete_api(uid: str, backup_id: int):
    server = get_server(uid)
    backup = db.session.get(Backup, backup_id)
    if backup is None or backup.server_id != server.id:
        return fail("Backup not found", 404)
    provisioning.delete_backup(server, backup)
    db.session.commit()
    return ok(message="Backup deleted")


# --------------------------------------------------------------------------- #
# Alerts & audit
# --------------------------------------------------------------------------- #
@bp.route("/alerts/rules", methods=["GET", "POST"])
@api_required(TokenScope.READ)
def alert_rules_api():
    from flask import g

    user = _scope_user()
    if request.method == "GET":
        rows = db.session.query(AlertRule).filter(AlertRule.user_id == user.id).all()
        return ok(rules=[row.public() for row in rows])
    token = getattr(g, "api_token", None)
    if token is not None and not token.has_scope(TokenScope.WRITE):
        return fail("Token lacks the 'write' scope", 403, code="insufficient_scope")
    data = body()
    metric = str(data.get("metric") or "cpu")
    if metric not in alert_service.METRICS:
        return fail("Unsupported metric", 400)
    server = None
    if data.get("server_uid"):
        server = get_server(str(data["server_uid"]))
    rule = AlertRule(
        user_id=user.id,
        server_id=server.id if server else None,
        name=str(data.get("name") or f"{metric} watch")[:64],
        metric=metric,
        comparator="<" if str(data.get("comparator")) == "<" else ">",
        threshold=float(data.get("threshold") or alert_service.METRICS[metric]["default"]),
        duration_s=int(data.get("duration_s") or 300),
        severity=str(data.get("severity") or "warning"),
        notify_email=bool(data.get("notify_email", True)),
        notify_webhook=str(data.get("notify_webhook") or "")[:255],
    )
    db.session.add(rule)
    audit.record("api.alert_rule", target_type="alert", target_label=rule.name,
                 meta={"metric": metric, "threshold": rule.threshold})
    db.session.commit()
    return ok(rule=rule.public(), status=201), 201


@bp.route("/alerts/events")
@api_required(TokenScope.READ)
def alert_events_api():
    user = _scope_user()
    page, size = paging(50, 200)
    query = db.session.query(AlertEvent).join(AlertRule, AlertRule.id == AlertEvent.rule_id)
    if not user.can(Role.OPERATOR):
        query = query.filter(AlertRule.user_id == user.id)
    data = paginated(query.order_by(AlertEvent.ts.desc()), page, size, serializer=lambda row: row.public())
    return ok(**data)


@bp.route("/audit")
@api_required(TokenScope.ADMIN)
def audit_api():
    user = _scope_user()
    if not user.can(Role.OPERATOR):
        return fail("Staff access required", 403)
    page, size = paging(50, 500)
    query = db.session.query(AuditLog).order_by(AuditLog.ts.desc())
    if arg("action"):
        query = query.filter(AuditLog.action.ilike(f"%{arg('action')}%"))
    if arg("actor"):
        query = query.filter(AuditLog.actor.ilike(f"%{arg('actor')}%"))
    data = paginated(query, page, size, serializer=lambda row: row.public())
    return ok(**data)
