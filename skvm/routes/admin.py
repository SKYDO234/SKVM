"""Administration: users, fleet, hypervisor nodes, settings, audit, incidents."""

from __future__ import annotations

import csv
import io
import logging
import secrets
from datetime import timedelta

from flask import Blueprint, Response, current_app, render_template, request
from sqlalchemy import func, or_

from ..backends import BackendError, available_backends, backend_status, get_backend
from ..db import db, iso, utcnow
from ..models import (
    AlertEvent,
    AlertRule,
    Announcement,
    AuditLog,
    Backup,
    Incident,
    Node,
    Role,
    Server,
    ServerEvent,
    ServerMetric,
    ServerStatus,
    Session,
    Setting,
    Snapshot,
    User,
)
from ..security import (
    admin_required,
    client_ip,
    current_user,
    encrypt_secret,
    end_session,
    hash_password,
    login_required,
    password_problems,
    role_required,
    sha256,
    staff_required,
)
from ..services import audit, metrics, provisioning
from ..services.mailer import send_welcome
from ..services.settings import DEFAULTS, settings
from .helpers import arg, body, fail, get_server, ok, paging, paginated

log = logging.getLogger("skvm.admin")

bp = Blueprint("admin", __name__, url_prefix="/admin")


# --------------------------------------------------------------------------- #
# Overview
# --------------------------------------------------------------------------- #
@bp.route("/")
@staff_required
def index():
    servers = db.session.query(Server).all()
    users = db.session.query(User).all()
    nodes = db.session.query(Node).all()
    totals = metrics.summary(servers, include_live=False)
    recent_audit = audit.recent(limit=25)
    audit_stats = audit.stats(days=7)
    alert_rows = (
        db.session.query(AlertEvent).order_by(AlertEvent.ts.desc()).limit(10).all()
    )
    storage = db.session.query(func.coalesce(func.sum(Backup.size_mb), 0)).scalar() or 0
    return render_template(
        "admin/index.html",
        totals=totals,
        users=users,
        nodes=nodes,
        servers=servers,
        recent_audit=recent_audit,
        audit_stats=audit_stats,
        alerts=alert_rows,
        backend=backend_status(),
        backends=available_backends(),
        storage_mb=round(float(storage), 1),
        worker_stats=_worker_stats(),
        host=backend_status().get("host") or {},
    )


def _worker_stats() -> dict:
    try:
        from ..services.reaper import worker

        return dict(worker.stats)
    except Exception:  # pragma: no cover
        return {}


@bp.route("/api/v1/overview")
@staff_required
def overview_api():
    servers = db.session.query(Server).all()
    return ok(
        totals=metrics.summary(servers, include_live=False),
        backend=backend_status(),
        nodes=_node_rows(),
        worker=_worker_stats(),
        at=iso(utcnow()),
    )


def _node_rows() -> list[dict]:
    nodes = db.session.query(Node).order_by(Node.priority).all()
    out = []
    for node in nodes:
        data = node.public()
        data["servers"] = db.session.query(Server).filter(Server.node_id == node.id).count()
        out.append(data)
    return out


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #
@bp.route("/users")
@admin_required
def users():
    search = (arg("q", "") or "").strip()
    role = arg("role", "")
    query = db.session.query(User)
    if search:
        needle = f"%{search}%"
        query = query.filter(
            or_(User.username.ilike(needle), User.email.ilike(needle), User.full_name.ilike(needle))
        )
    if role:
        query = query.filter(User.role == role)
    rows = query.order_by(User.created_at.desc()).all()
    counts: dict[str, int] = {}
    for user in rows:
        counts[user.username] = db.session.query(Server).filter(Server.owner_id == user.id).count()
    return render_template(
        "admin/users.html",
        users=rows,
        server_counts=counts,
        roles=[r.value for r in Role],
        filters={"q": search, "role": role},
        quotas=provisioning.quota_snapshot(current_user()),
        default_quotas=settings.quotas(),
    )


@bp.route("/api/v1/users", methods=["GET", "POST"])
@admin_required
def users_api():
    if request.method == "GET":
        page, size = paging(25, 100)
        query = db.session.query(User).order_by(User.created_at.desc())
        if arg("q"):
            needle = f"%{arg('q')}%"
            query = query.filter(or_(User.username.ilike(needle), User.email.ilike(needle)))
        data = paginated(query, page, size, serializer=lambda row: row.public())
        return ok(**data)

    data = body()
    username = str(data.get("username") or "").strip()
    email = str(data.get("email") or "").strip().lower()
    from ..security import EMAIL_RE, USERNAME_RE

    problems = []
    if not USERNAME_RE.match(username):
        problems.append("Username must be 4-64 characters (letters, digits, . _ -)")
    if not EMAIL_RE.match(email):
        problems.append("A valid e-mail address is required")
    if db.session.query(User).filter(User.username == username).first():
        problems.append("Username is taken")
    if db.session.query(User).filter(User.email == email).first():
        problems.append("E-mail is taken")
    if problems:
        return fail("; ".join(problems), 400, problems=problems)

    role = str(data.get("role") or Role.USER.value)
    if role not in [r.value for r in Role]:
        role = Role.USER.value
    if role == Role.OWNER.value and not current_user().is_owner:
        return fail("Only an owner may create another owner", 403)
    temporary = str(data.get("password") or "") or secrets.token_urlsafe(10)
    quotas = {**settings.quotas(), **(data.get("quotas") or {})}
    user = User(
        username=username,
        email=email,
        full_name=str(data.get("full_name") or username)[:128],
        company=str(data.get("company") or "")[:128],
        password_hash=hash_password(temporary),
        role=role,
        is_active=bool(data.get("is_active", True)),
        is_verified=True,
        must_change_password=bool(data.get("must_change_password", True)),
        quotas=quotas,
        credits=float(data.get("credits") or 0),
        notes=str(data.get("notes") or "")[:2000],
    )
    db.session.add(user)
    audit.record("admin.user_create", target_type="user", target_label=username,
                 meta={"role": role, "email": email})
    db.session.commit()
    if data.get("send_welcome", True):
        try:
            send_welcome(user, temporary)
        except Exception as exc:  # pragma: no cover
            log.debug("welcome mail failed: %s", exc)
    return ok(user=user.public(), temporary_password=temporary, message=f"{username} created")


@bp.route("/api/v1/users/<int:user_id>", methods=["PATCH", "DELETE"])
@admin_required
def user_detail(user_id: int):
    actor = current_user()
    user = db.session.get(User, user_id)
    if user is None:
        return fail("User not found", 404)
    if user.is_owner and not actor.is_owner:
        return fail("Only an owner may modify an owner account", 403)
    if request.method == "DELETE":
        if user.id == actor.id:
            return fail("You cannot delete your own account", 400)
        if user.is_owner and db.session.query(User).filter(User.role == Role.OWNER.value).count() <= 1:
            return fail("At least one owner must remain", 400)
        label = user.username
        server_ids = [s.uid for s in db.session.query(Server).filter(Server.owner_id == user.id).all()]
        for server in db.session.query(Server).filter(Server.owner_id == user.id).all():
            try:
                provisioning.delete_server(server, purge_data=True)
            except BackendError as exc:
                log.warning("could not purge server %s: %s", server.uid, exc)
        audit.record("admin.user_delete", target_type="user", target_id=user.id, target_label=label,
                     result="warn", meta={"servers": server_ids})
        db.session.delete(user)
        db.session.commit()
        return ok(message=f"{label} deleted")

    data = body()
    if "role" in data:
        new_role = str(data["role"])
        if new_role in [r.value for r in Role]:
            if new_role == Role.OWNER.value and not actor.is_owner:
                return fail("Only an owner may grant the owner role", 403)
            user.role = new_role
    if "is_active" in data:
        user.is_active = bool(data["is_active"])
        if not user.is_active:
            for record in db.session.query(Session).filter(Session.user_id == user.id).all():
                record.revoked_at = utcnow()
                record.revoked_by = "disabled by admin"
    if "full_name" in data:
        user.full_name = str(data["full_name"])[:128]
    if "email" in data:
        email = str(data["email"]).strip().lower()
        clash = db.session.query(User).filter(User.email == email, User.id != user.id).first()
        if clash:
            return fail("That e-mail is already used by another account", 409)
        user.email = email
    if "quotas" in data and isinstance(data["quotas"], dict):
        quotas = dict(user.quotas or {})
        for key, value in data["quotas"].items():
            try:
                quotas[key] = float(value) if key == "cpu" else int(value)
            except (TypeError, ValueError):
                continue
        user.quotas = quotas
    if "credits" in data:
        user.credits = float(data["credits"])
    if "notes" in data:
        user.notes = str(data["notes"])[:4000]
    if data.get("reset_password"):
        temporary = secrets.token_urlsafe(10)
        user.password_hash = hash_password(temporary)
        user.must_change_password = True
        for record in db.session.query(Session).filter(Session.user_id == user.id).all():
            record.revoked_at = utcnow()
            record.revoked_by = "password reset by admin"
        audit.record("admin.user_password_reset", target_type="user", target_id=user.id,
                     target_label=user.username, result="warn")
        db.session.commit()
        return ok(user=user.public(), temporary_password=temporary, message="Password reset")

    audit.record("admin.user_update", target_type="user", target_id=user.id, target_label=user.username,
                 meta={"fields": sorted(data.keys())})
    db.session.commit()
    return ok(user=user.public(), message="User updated")


@bp.route("/api/v1/users/<int:user_id>/sessions", methods=["GET", "DELETE"])
@admin_required
def user_sessions(user_id: int):
    user = db.session.get(User, user_id)
    if user is None:
        return fail("User not found", 404)
    if request.method == "GET":
        return ok(sessions=[row.public() for row in user.sessions])
    count = 0
    for record in db.session.query(Session).filter(Session.user_id == user.id, Session.revoked_at.is_(None)).all():
        record.revoked_at = utcnow()
        record.revoked_by = "admin"
        count += 1
    audit.record("admin.user_sessions_revoke", target_type="user", target_id=user.id,
                 target_label=user.username, meta={"count": count}, result="warn")
    db.session.commit()
    return ok(count=count, message=f"Revoked {count} session(s)")


# --------------------------------------------------------------------------- #
# Fleet
# --------------------------------------------------------------------------- #
@bp.route("/servers")
@staff_required
def servers():
    status_filter = arg("status", "")
    owner_filter = arg("owner", "")
    search = (arg("q", "") or "").strip()
    query = db.session.query(Server)
    if status_filter:
        query = query.filter(Server.status == status_filter)
    if owner_filter:
        try:
            query = query.filter(Server.owner_id == int(owner_filter))
        except ValueError:
            pass
    if search:
        needle = f"%{search}%"
        query = query.filter(
            or_(Server.name.ilike(needle), Server.uid.ilike(needle), Server.ip_address.ilike(needle))
        )
    rows = query.order_by(Server.created_at.desc()).all()
    return render_template(
        "admin/servers.html",
        servers=rows,
        users=db.session.query(User).order_by(User.username).all(),
        totals=metrics.summary(rows, include_live=False),
        filters={"status": status_filter, "owner": owner_filter, "q": search},
        statuses=[s.value for s in ServerStatus],
        nodes=db.session.query(Node).all(),
    )


@bp.route("/api/v1/servers/<uid>", methods=["PATCH", "DELETE"])
@staff_required
def admin_server(uid: str):
    server = get_server(uid)
    if request.method == "DELETE":
        label = server.name
        provisioning.delete_server(server, purge_data=True)
        db.session.commit()
        return ok(message=f"{label} deleted")
    data = body()
    if "owner_id" in data:
        new_owner = db.session.get(User, int(data["owner_id"]))
        if new_owner is None:
            return fail("New owner not found", 404)
        server.owner_id = new_owner.id
    if "node_id" in data:
        node = db.session.get(Node, int(data["node_id"])) if data["node_id"] else None
        server.node_id = node.id if node else None
    if "status" in data and str(data["status"]) in [s.value for s in ServerStatus]:
        server.status = str(data["status"])
    if "monthly_price" in data:
        server.monthly_price = float(data["monthly_price"])
    if "expires_at" in data:
        from ..db import parse_dt

        server.expires_at = parse_dt(data["expires_at"])
    if "auto_renew" in data:
        server.auto_renew = bool(data["auto_renew"])
    if "suspended_reason" in data:
        server.suspended_reason = str(data["suspended_reason"])[:128]
    if "snapshot_limit" in data:
        server.snapshot_limit = max(0, int(data["snapshot_limit"]))
    if "bandwidth_gb" in data:
        server.bandwidth_gb = max(0, int(data["bandwidth_gb"]))
    if data.get("action") == "suspend":
        provisioning.suspend_server(server, str(data.get("reason") or "administrative"))
    if data.get("action") == "unsuspend":
        provisioning.unsuspend_server(server)
    audit.record("admin.server_update", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"fields": sorted(data.keys())}, result="warn")
    db.session.commit()
    return ok(server=server.public(detail=True), message="Server updated")


# --------------------------------------------------------------------------- #
# Nodes
# --------------------------------------------------------------------------- #
def _new_node_id() -> str:
    return secrets.token_hex(6)


@bp.route("/nodes")
@admin_required
def nodes():
    return render_template(
        "admin/nodes.html",
        nodes=_node_rows(),
        backends=available_backends(),
        backend=backend_status(),
        server_counts={
            node.id: db.session.query(Server).filter(Server.node_id == node.id).count()
            for node in db.session.query(Node).all()
        },
    )


@bp.route("/api/v1/nodes", methods=["GET", "POST"])
@admin_required
def nodes_api():
    if request.method == "GET":
        return ok(nodes=_node_rows(), backends=available_backends())
    data = body()
    name = str(data.get("name") or "").strip()
    endpoint = str(data.get("endpoint") or "").strip().rstrip("/")
    if not name or not endpoint.startswith(("http://", "https://")):
        return fail("Provide a node name and an http(s) endpoint", 400)
    if db.session.query(Node).filter(Node.name == name).first():
        return fail("A node with that name already exists", 409)
    secret = str(data.get("secret") or "") or secrets.token_urlsafe(32)
    node = Node(
        name=name[:64],
        endpoint=endpoint[:255],
        region=str(data.get("region") or "local")[:64],
        key_id=str(data.get("key_id") or f"sk_{_new_node_id()}")[:32],
        key_hash=encrypt_secret(secret),
        tls_verify=bool(data.get("tls_verify", True)),
        capacity_cpu=float(data.get("capacity_cpu") or 0),
        capacity_ram_mb=int(data.get("capacity_ram_mb") or 0),
        capacity_disk_gb=int(data.get("capacity_disk_gb") or 0),
        labels=data.get("labels") or {},
        priority=int(data.get("priority") or 100),
    )
    db.session.add(node)
    audit.record("admin.node_create", target_type="node", target_label=node.name,
                 meta={"endpoint": endpoint, "region": node.region})
    db.session.commit()
    return ok(node=node.public(reveal_key_id=True), secret=secret,
              message="Node registered — copy the secret into the node's config")


@bp.route("/api/v1/nodes/<int:node_id>", methods=["PATCH", "DELETE"])
@admin_required
def node_detail(node_id: int):
    node = db.session.get(Node, node_id)
    if node is None:
        return fail("Node not found", 404)
    if request.method == "DELETE":
        if db.session.query(Server).filter(Server.node_id == node.id).count():
            return fail("Reassign or delete this node's servers first", 409)
        audit.record("admin.node_delete", target_type="node", target_id=node.id, target_label=node.name,
                     result="warn")
        db.session.delete(node)
        db.session.commit()
        return ok(message=f"{node.name} removed")

    data = body()
    if "name" in data:
        node.name = str(data["name"])[:64]
    if "endpoint" in data:
        node.endpoint = str(data["endpoint"]).rstrip("/")[:255]
    if "region" in data:
        node.region = str(data["region"])[:64]
    if "tls_verify" in data:
        node.tls_verify = bool(data["tls_verify"])
    if "priority" in data:
        node.priority = int(data["priority"])
    if "capacity_cpu" in data:
        node.capacity_cpu = float(data["capacity_cpu"])
    if "capacity_ram_mb" in data:
        node.capacity_ram_mb = int(data["capacity_ram_mb"])
    if "capacity_disk_gb" in data:
        node.capacity_disk_gb = int(data["capacity_disk_gb"])
    if "labels" in data and isinstance(data["labels"], dict):
        node.labels = data["labels"]
    secret = None
    if data.get("rotate_key"):
        secret = secrets.token_urlsafe(32)
        node.key_hash = encrypt_secret(secret)
        node.key_id = f"sk_{_new_node_id()}"
    audit.record("admin.node_update", target_type="node", target_id=node.id, target_label=node.name,
                 meta={"fields": sorted(data.keys())})
    db.session.commit()
    return ok(node=node.public(reveal_key_id=True), secret=secret, message="Node updated")


@bp.route("/api/v1/nodes/<int:node_id>/test", methods=["POST"])
@admin_required
def node_test(node_id: int):
    node = db.session.get(Node, node_id)
    if node is None:
        return fail("Node not found", 404)
    from ..backends.node_backend import NodeBackend

    try:
        backend = NodeBackend(node)
    except Exception as exc:  # missing or rotated agent secret
        return ok(online=False, detail=str(exc)[:250], node=node.public(),
                  message="This node has no usable agent secret")
    online, detail = backend.ping()
    node.status = "online" if online else "offline"
    node.last_error = "" if online else detail[:250]
    node.version = detail.split("·")[-1].strip()[:32] if online else node.version
    if online:
        node.last_seen_at = utcnow()
        backend.host_stats()
    audit.record("admin.node_test", target_type="node", target_id=node.id, target_label=node.name,
                 result="ok" if online else "error", meta={"detail": detail})
    db.session.commit()
    return ok(online=online, detail=detail, node=node.public())


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@bp.route("/settings")
@admin_required
def settings_page():
    rows = db.session.query(Setting).order_by(Setting.key).all()
    return render_template(
        "admin/settings.html",
        values=settings.all(),
        defaults=DEFAULTS,
        rows=rows,
        config_view=_config_view(),
        backend=backend_status(),
        backends=available_backends(),
    )


def _config_view() -> dict:
    """Non-secret configuration surfaced for operators (secrets are masked)."""
    cfg = current_app.config
    secret_keys = ("SECRET_KEY", "ENCRYPTION_KEY_RESOLVED", "PROMETHEUS_TOKEN", "SMTP_PASSWORD")
    view = {}
    for key, value in cfg.items():
        if not key.isupper() or key.startswith("SQLALCHEMY_ENGINE"):
            continue
        if key in secret_keys:
            view[key] = "••••••••" if value else ""
        elif isinstance(value, (str, int, float, bool)) or value is None:
            view[key] = value
    return dict(sorted(view.items()))


@bp.route("/api/v1/settings", methods=["POST"])
@admin_required
def settings_update():
    data = body()
    values: dict[str, object] = {}
    for key, value in data.items():
        if key not in DEFAULTS:
            continue
        default = DEFAULTS[key]
        try:
            if isinstance(default, bool):
                values[key] = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "yes", "on")
            elif isinstance(default, int) and not isinstance(default, bool):
                values[key] = int(value)
            elif isinstance(default, float):
                values[key] = float(value)
            elif isinstance(default, dict):
                merged = dict(settings.get(key) or {})
                merged.update(value if isinstance(value, dict) else {})
                values[key] = merged
            else:
                values[key] = str(value)
        except (TypeError, ValueError):
            return fail(f"Invalid value for {key}", 400)
    changed = settings.update(values, updated_by=current_user().username)
    audit.record("admin.settings_update", target_type="settings", target_label="panel settings",
                 meta={"changed": changed}, result="warn")
    db.session.commit()
    return ok(changed=changed, values=settings.all(), message=f"{len(changed)} setting(s) updated")


@bp.route("/api/v1/settings/reset", methods=["POST"])
@admin_required
def settings_reset():
    data = body()
    key = str(data.get("key") or "")
    if key not in DEFAULTS:
        return fail("Unknown setting", 404)
    settings.set(key, DEFAULTS[key], updated_by=current_user().username)
    audit.record("admin.settings_reset", target_type="settings", target_label=key, result="warn")
    db.session.commit()
    return ok(value=DEFAULTS[key], message=f"{key} reset to default")


# --------------------------------------------------------------------------- #
# Audit & incidents
# --------------------------------------------------------------------------- #
@bp.route("/audit")
@staff_required
def audit_page():
    page, size = paging(40, 200)
    query = db.session.query(AuditLog).order_by(AuditLog.ts.desc())
    actor = arg("actor", "")
    action = arg("action", "")
    result = arg("result", "")
    channel = arg("channel", "")
    since = arg("since", "")
    if actor:
        query = query.filter(AuditLog.actor.ilike(f"%{actor}%"))
    if action:
        query = query.filter(AuditLog.action.ilike(f"%{action}%"))
    if result:
        query = query.filter(AuditLog.result == result)
    if channel:
        query = query.filter(AuditLog.channel == channel)
    if since:
        parsed = audit.parse_since(since, default_days=7)
        query = query.filter(AuditLog.ts >= parsed)
    data = paginated(query, page, size, serializer=lambda row: row.public())
    actions = [row[0] for row in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()]
    return render_template(
        "admin/audit.html",
        entries=data["items"],
        page=data["page"],
        pages=data["pages"],
        total=data["total"],
        actions=actions,
        stats=audit.stats(days=int(arg("days", 7, int) or 7)),
        filters={"actor": actor, "action": action, "result": result, "channel": channel, "since": since},
    )


@bp.route("/audit/export")
@admin_required
def audit_export():
    days = int(arg("days", 30, int) or 30)
    payload = audit.export_csv(days=days)
    audit.record("admin.audit_export", target_type="audit", target_label=f"last {days} days")
    db.session.commit()
    return Response(
        payload,
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="skvm-audit-{days}d.csv"'},
    )


@bp.route("/announcements", methods=["GET", "POST"])
@admin_required
def announcements():
    if request.method == "GET":
        rows = db.session.query(Announcement).order_by(Announcement.created_at.desc()).all()
        return render_template("admin/announcements.html", announcements=rows)
    data = body()
    row = Announcement(
        title=str(data.get("title") or "Notice")[:128],
        body=str(data.get("body") or "")[:4000],
        level=str(data.get("level") or "info"),
        active=bool(data.get("active", True)),
        pinned=bool(data.get("pinned", False)),
        audience=str(data.get("audience") or "all"),
        created_by=current_user().username,
    )
    db.session.add(row)
    audit.record("admin.announcement", target_type="announcement", target_label=row.title)
    db.session.commit()
    return ok(announcement=row.public(), message="Announcement published")


@bp.route("/api/v1/announcements/<int:announcement_id>", methods=["DELETE", "PATCH"])
@admin_required
def announcement_detail(announcement_id: int):
    row = db.session.get(Announcement, announcement_id)
    if row is None:
        return fail("Announcement not found", 404)
    if request.method == "DELETE":
        db.session.delete(row)
        db.session.commit()
        return ok(message="Announcement removed")
    data = body()
    for field in ("title", "body", "level", "audience"):
        if field in data:
            setattr(row, field, str(data[field])[:4000])
    if "active" in data:
        row.active = bool(data["active"])
    if "pinned" in data:
        row.pinned = bool(data["pinned"])
    db.session.commit()
    return ok(announcement=row.public(), message="Announcement updated")


@bp.route("/incidents", methods=["GET", "POST"])
@staff_required
def incidents():
    if request.method == "GET":
        rows = db.session.query(Incident).order_by(Incident.started_at.desc()).limit(50).all()
        return render_template("admin/incidents.html", incidents=rows)
    data = body()
    row = Incident(
        title=str(data.get("title") or "Incident")[:128],
        body=str(data.get("body") or "")[:4000],
        severity=str(data.get("severity") or "info"),
        status=str(data.get("status") or "investigating"),
    )
    db.session.add(row)
    audit.record("admin.incident_create", target_type="incident", target_label=row.title, result="warn")
    db.session.commit()
    return ok(incident=row.public(), message="Incident opened")


@bp.route("/api/v1/incidents/<int:incident_id>", methods=["PATCH", "DELETE"])
@staff_required
def incident_detail(incident_id: int):
    row = db.session.get(Incident, incident_id)
    if row is None:
        return fail("Incident not found", 404)
    if request.method == "DELETE":
        db.session.delete(row)
        db.session.commit()
        return ok(message="Incident deleted")
    data = body()
    if "status" in data:
        row.status = str(data["status"])[:16]
        if row.status == "resolved":
            row.resolved_at = utcnow()
    if "body" in data:
        row.body = str(data["body"])[:4000]
    if "severity" in data:
        row.severity = str(data["severity"])[:16]
    if "title" in data:
        row.title = str(data["title"])[:128]
    audit.record("admin.incident_update", target_type="incident", target_id=row.id, target_label=row.title)
    db.session.commit()
    return ok(incident=row.public(), message="Incident updated")


# --------------------------------------------------------------------------- #
# Maintenance tools
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/maintenance/<action>", methods=["POST"])
@admin_required
def maintenance(action: str):
    backend = get_backend()
    if action == "prune":
        result = backend.prune()
        audit.record("admin.prune", target_type="system", target_label="docker prune", result="warn")
        db.session.commit()
        return ok(result=result, message="Prune complete")
    if action == "sync":
        count = 0
        for server in db.session.query(Server).all():
            provisioning.sync_status(server)
            count += 1
        db.session.commit()
        return ok(count=count, message=f"Synced {count} server(s) with the hypervisor")
    if action == "retention":
        from ..services.reaper import worker

        worker.retention()
        return ok(message="Retention job executed")
    if action == "worker-stats":
        return ok(stats=_worker_stats())
    if action == "prometheus-token":
        token = secrets.token_urlsafe(24)
        current_app.config["PROMETHEUS_TOKEN"] = token
        db.session.add(Setting(key="prometheus_token", value=token, updated_by=current_user().username))
        db.session.commit()
        audit.record("admin.prometheus_token", target_type="system", target_label="rotate scrape token",
                     result="warn")
        return ok(token=token, message="Scrape token rotated (restart the service to persist)")
    return fail("Unknown maintenance action", 404)
