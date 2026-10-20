"""Servers: list, create wizard, detail cockpit, power/snapshot/backup APIs."""

from __future__ import annotations

import logging
from datetime import timedelta

from flask import Blueprint, Response, current_app, jsonify, render_template, request
from sqlalchemy import or_

from ..backends import BackendError, get_backend
from ..backends.images import IMAGE_CATALOG, PLAN_PRESETS, REGIONS, image_info
from ..db import db, iso, utcnow
from ..models import Backup, Node, Role, Server, ServerEvent, ServerStatus, Snapshot, SshKey, User
from ..security import (
    admin_required,
    api_required,
    current_user,
    decrypt_secret,
    login_required,
    owner_or_staff,
    role_required,
    TokenScope,
)
from ..services import audit, metrics, provisioning
from ..services.mailer import send_server_ready
from ..services.settings import settings
from .helpers import (
    arg,
    body,
    fail,
    get_server,
    human_uptime,
    ok,
    paging,
    paginated,
    wants_json,
)

log = logging.getLogger("skvm.servers")

bp = Blueprint("servers", __name__, url_prefix="/servers")


def _visible(user: User):
    query = db.session.query(Server)
    if not user.can(Role.OPERATOR):
        query = query.filter(Server.owner_id == user.id)
    return query


# --------------------------------------------------------------------------- #
# Listing & creation wizard
# --------------------------------------------------------------------------- #
@bp.route("/")
@login_required
def index():
    user = current_user()
    status_filter = arg("status", "")
    search = (arg("q", "") or "").strip()
    sort = arg("sort", "created")
    query = _visible(user)

    if status_filter:
        query = query.filter(Server.status == status_filter)
    if search:
        needle = f"%{search}%"
        query = query.filter(
            or_(Server.name.ilike(needle), Server.hostname.ilike(needle), Server.ip_address.ilike(needle),
                Server.uid.ilike(needle), Server.os_image.ilike(needle))
        )
    order = {
        "name": Server.name.asc(),
        "name_desc": Server.name.desc(),
        "cpu": Server.cpu_cores.desc(),
        "ram": Server.ram_mb.desc(),
        "expiry": Server.expires_at.asc(),
        "cost": Server.monthly_price.desc(),
        "created": Server.created_at.desc(),
    }.get(sort, Server.created_at.desc())
    servers = query.order_by(order).all()

    return render_template(
        "servers.html",
        servers=servers,
        summary=metrics.summary(servers, include_live=False),
        statuses=[status.value for status in ServerStatus],
        filters={"status": status_filter, "q": search, "sort": sort},
        images=IMAGE_CATALOG,
        quotas=provisioning.quota_snapshot(user),
        plans=PLAN_PRESETS,
        regions=REGIONS,
        can_create=user.role_rank >= 1,
        backend=get_backend(),
    )


@bp.route("/new")
@login_required
def new():
    user = current_user()
    backend = get_backend()
    return render_template(
        "server_new.html",
        images=[image_info(key) for key in IMAGE_CATALOG if not IMAGE_CATALOG[key].get("kvm_only")],
        plans=PLAN_PRESETS,
        regions=REGIONS,
        nodes=db.session.query(Node).order_by(Node.priority).all(),
        quotas=provisioning.quota_snapshot(user),
        catalog=backend.catalog(),
        backend=backend,
        ssh_keys=db.session.query(SshKey).filter(SshKey.user_id == user.id).all(),
        defaults={
            "cpu": current_app.config.get("DEFAULT_CPU", 2),
            "ram_mb": current_app.config.get("DEFAULT_RAM_MB", 4096),
            "disk_gb": current_app.config.get("DEFAULT_DISK_GB", 50),
            "image": settings.get("default_os", "ubuntu-24.04"),
        },
        estimate=provisioning.estimate_price,
    )


@bp.route("/api/v1/create", methods=["POST"])
@login_required
def create():
    user = current_user()
    data = body()
    images = [key for key in IMAGE_CATALOG if not IMAGE_CATALOG[key].get("kvm_only")]
    os_image = data.get("os_image") or settings.get("default_os", "ubuntu-24.04")
    if os_image not in images:
        return fail("Choose a supported operating system", 400)

    ssh_keys: list[str] = []
    payload_keys = data.get("ssh_keys") or []
    if isinstance(payload_keys, str):
        payload_keys = [payload_keys]
    stored = {
        key.id: key.public_key
        for key in db.session.query(SshKey).filter(SshKey.user_id == user.id).all()
    }
    for item in payload_keys:
        try:
            key_id = int(item)
        except (TypeError, ValueError):
            ssh_keys.append(str(item))
            continue
        if key_id in stored:
            ssh_keys.append(stored[key_id])
    if data.get("public_key"):
        ssh_keys.append(str(data["public_key"]))

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
            ssh_keys=ssh_keys,
            tags=[tag for tag in (data.get("tags") or []) if tag],
            plan=str(data.get("plan") or "custom"),
            node_id=int(data["node_id"]) if data.get("node_id") else None,
            expires_in_days=int(data["expires_in_days"]) if data.get("expires_in_days") else 30,
            notes=str(data.get("notes") or ""),
        )
    except provisioning.ProvisioningError as exc:
        return fail(str(exc), 400, code=exc.code)
    except (BackendError, ValueError) as exc:
        return fail(str(exc), 400)

    db.session.commit()
    if settings.boolean("alert_email_enabled"):
        try:
            send_server_ready(server)
        except Exception as exc:  # pragma: no cover
            log.debug("ready mail failed: %s", exc)

    return ok(
        server=server.public(detail=True, include_secrets=True),
        redirect=f"/servers/{server.uid}",
        message=f"{server.name} is provisioning",
    )


# --------------------------------------------------------------------------- #
# Cockpit
# --------------------------------------------------------------------------- #
@bp.route("/<uid>")
@login_required
def detail(uid: str):
    server = get_server(uid)
    stats = metrics.collect(server, cache_seconds=1)
    snapshots = (
        db.session.query(Snapshot).filter(Snapshot.server_id == server.id).order_by(Snapshot.created_at.desc()).all()
    )
    backups = (
        db.session.query(Backup).filter(Backup.server_id == server.id).order_by(Backup.created_at.desc()).all()
    )
    events = (
        db.session.query(ServerEvent)
        .filter(ServerEvent.server_id == server.id)
        .order_by(ServerEvent.ts.desc())
        .limit(30)
        .all()
    )
    filesystem_ok = True
    try:
        get_backend(server).require("files")
    except BackendError:
        filesystem_ok = False

    return render_template(
        "server_detail.html",
        server=server,
        stats=stats,
        series=metrics.live_series(server, points=90),
        history=metrics.history(server, hours=24, buckets=72),
        snapshots=snapshots,
        backups=backups,
        events=events,
        filesystem_ok=filesystem_ok,
        console_enabled=settings.boolean("console_enabled") and current_app.config.get("ALLOW_CONSOLE", True),
        quota=provisioning.quota_snapshot(server.owner),
        images=[image_info(key) for key in IMAGE_CATALOG if not IMAGE_CATALOG[key].get("kvm_only")],
        root_password=decrypt_secret(server.root_password_enc),
        backend=get_backend(server),
        is_owner=(server.owner_id == current_user().id),
    )


@bp.route("/<uid>/console")
@login_required
def console_page(uid: str):
    server = get_server(uid)
    if not (settings.boolean("console_enabled") and current_app.config.get("ALLOW_CONSOLE", True)):
        return render_template("error.html", code=403, title="Console disabled",
                               message="Interactive consoles are disabled by the administrator."), 403
    return render_template(
        "console.html",
        server=server,
        server_data=server.public(detail=True),
        ssh_command=server.ssh_command(),
        stats=metrics.collect(server, cache_seconds=2),
        series=metrics.live_series(server, points=40),
    )


@bp.route("/<uid>/files")
@login_required
def files_page(uid: str):
    server = get_server(uid)
    if not settings.boolean("file_manager_enabled"):
        return render_template("error.html", code=403, title="File manager disabled",
                               message="The file manager is disabled by the administrator."), 403
    return render_template("files.html", server=server, server_data=server.public())


@bp.route("/<uid>/metrics")
@login_required
def metrics_page(uid: str):
    server = get_server(uid)
    return render_template(
        "server_metrics.html",
        server=server,
        stats=metrics.collect(server, cache_seconds=1),
        series=metrics.live_series(server, points=120),
        history_24h=metrics.history(server, hours=24, buckets=96),
        history_7d=metrics.history(server, hours=24 * 7, buckets=120),
    )


# --------------------------------------------------------------------------- #
# Power / lifecycle API
# --------------------------------------------------------------------------- #
@bp.route("/<uid>/api/v1/power", methods=["POST"])
@login_required
def power(uid: str):
    server = get_server(uid)
    data = body()
    action = (data.get("action") or "").lower()
    if action not in ("start", "stop", "restart", "suspend", "resume"):
        return fail("Unsupported power action", 400)
    if action == "suspend" and not current_user().can(Role.ADMIN):
        return fail("Only administrators may suspend servers", 403)
    if action in ("start", "stop", "restart", "resume") and server.owner_id != current_user().id:
        if not current_user().can(Role.OPERATOR):
            return fail("You cannot control this server", 403)
    try:
        provisioning.power_action(server, action, actor=current_user().username)
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(server=server.public(detail=True), message=f"{action.title()} issued")


@bp.route("/<uid>/api/v1/sync", methods=["POST"])
@login_required
def sync(uid: str):
    server = get_server(uid)
    provisioning.sync_status(server)
    db.session.commit()
    return ok(server=server.public(detail=True))


@bp.route("/<uid>/api/v1/resize", methods=["POST"])
@login_required
def resize(uid: str):
    server = get_server(uid)
    data = body()
    try:
        provisioning.resize_server(
            server,
            float(data.get("cpu", server.cpu_cores)),
            int(data.get("ram_mb", server.ram_mb)),
            int(data.get("disk_gb", server.disk_gb)),
        )
    except provisioning.ProvisioningError as exc:
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(server=server.public(detail=True), message="Resources updated")


@bp.route("/<uid>/api/v1/reinstall", methods=["POST"])
@login_required
def reinstall(uid: str):
    server = get_server(uid)
    data = body()
    try:
        provisioning.reinstall_server(
            server,
            str(data.get("os_image") or server.os_image),
            str(data.get("root_password") or ""),
            [key.public_key for key in db.session.query(SshKey).filter(SshKey.user_id == server.owner_id).all()],
        )
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(server=server.public(detail=True, include_secrets=True), message="Reinstall complete")


@bp.route("/<uid>/api/v1/settings", methods=["POST"])
@login_required
def update_settings(uid: str):
    server = get_server(uid)
    data = body()
    if "name" in data:
        try:
            server.name = provisioning.validate_name(str(data["name"]))
        except provisioning.ProvisioningError as exc:
            return fail(str(exc), 400)
    if "hostname" in data:
        server.hostname = str(data["hostname"])[:63]
    if "tags" in data:
        tags = data["tags"]
        server.tags = [str(tag)[:24] for tag in (tags if isinstance(tags, list) else str(tags).split(",")) if str(tag).strip()]
    if "notes" in data:
        server.notes = str(data["notes"])[:4000]
    if "auto_renew" in data:
        server.auto_renew = bool(data["auto_renew"])
    if "backup_enabled" in data:
        server.backup_enabled = bool(data["backup_enabled"])
    if "backup_schedule" in data:
        server.backup_schedule = str(data["backup_schedule"])[:32]
    if "snapshot_limit" in data and current_user().can(Role.ADMIN):
        server.snapshot_limit = max(0, min(100, int(data["snapshot_limit"])))
    if "bandwidth_gb" in data and current_user().can(Role.ADMIN):
        server.bandwidth_gb = max(0, int(data["bandwidth_gb"]))
    audit.server_event(server, "settings", "Server settings updated")
    audit.record("server.update", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"fields": sorted(data.keys())})
    db.session.commit()
    return ok(server=server.public(detail=True), message="Settings saved")


@bp.route("/<uid>/api/v1/password", methods=["POST"])
@login_required
def rotate_password(uid: str):
    server = get_server(uid)
    from ..security import encrypt_secret

    password = provisioning.generate_password()
    server.root_password_enc = encrypt_secret(password)
    backend = get_backend(server)
    try:
        backend.exec_command(
            server.container_id,
            ["/bin/sh", "-c", f"echo 'root:{password}' | chpasswd"],
            timeout=20,
        )
    except BackendError as exc:
        log.debug("password rotation on host failed: %s", exc)
    audit.server_event(server, "password", "Root password rotated")
    audit.record("server.password", target_type="server", target_id=server.uid, target_label=server.name)
    db.session.commit()
    return ok(password=password, message="Root password rotated")


@bp.route("/<uid>/api/v1/delete", methods=["POST"])
@login_required
def destroy(uid: str):
    server = get_server(uid)
    data = body()
    if (data.get("confirm") or "").strip() != server.name:
        return fail("Type the server name to confirm deletion", 400)
    if server.owner_id == current_user().id and not settings.boolean("allow_self_delete") and not current_user().can(Role.ADMIN):
        return fail("Self-service deletion is disabled — contact support", 403)
    label = server.name
    try:
        provisioning.delete_server(server, purge_data=bool(data.get("purge_snapshots", True)))
    except BackendError as exc:
        return fail(str(exc), 400)
    db.session.commit()
    return ok(redirect="/servers/", message=f"{label} deleted")


# --------------------------------------------------------------------------- #
# Metrics & console tokens
# --------------------------------------------------------------------------- #
@bp.route("/<uid>/api/v1/metrics")
@login_required
def server_metrics(uid: str):
    server = get_server(uid)
    window = arg("window", "live")
    if window == "live":
        return ok(
            stats=metrics.collect(server, cache_seconds=1.5),
            series=metrics.live_series(server, points=int(arg("points", 60, int) or 60)),
            at=iso(utcnow()),
        )
    hours = {"1h": 1, "6h": 6, "24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(window, 24)
    return ok(stats=metrics.collect(server, cache_seconds=5), history=metrics.history(server, hours=hours, buckets=120),
              window=window, at=iso(utcnow()))


@bp.route("/<uid>/api/v1/console-token", methods=["POST"])
@login_required
def console_token(uid: str):
    """Mint a short-lived token consumed by the WebSocket handshake."""
    server = get_server(uid)
    import secrets

    token = secrets.token_urlsafe(24)
    current_app.extensions.setdefault("skvm.console_tokens", {})
    store = current_app.extensions["skvm.console_tokens"]
    now = utcnow()
    store[token] = {"server_id": server.id, "user_id": current_user().id, "expires": now + timedelta(minutes=2)}
    # opportunistic cleanup
    for key in [k for k, v in store.items() if v["expires"] < now]:
        store.pop(key, None)
    return ok(token=token, ws_path=f"/ws/server/{server.uid}/console", expires_in=120)


# --------------------------------------------------------------------------- #
# Snapshots & backups
# --------------------------------------------------------------------------- #
@bp.route("/<uid>/api/v1/snapshots", methods=["GET", "POST"])
@login_required
def snapshots(uid: str):
    server = get_server(uid)
    if request.method == "GET":
        rows = db.session.query(Snapshot).filter(Snapshot.server_id == server.id).order_by(Snapshot.created_at.desc()).all()
        return ok(snapshots=[row.public() for row in rows])
    data = body()
    try:
        snapshot = provisioning.create_snapshot(
            server,
            str(data.get("name") or f"snapshot-{utcnow().strftime('%Y%m%d-%H%M%S')}"),
            str(data.get("description") or ""),
            actor=current_user().username,
        )
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(snapshot=snapshot.public(), message="Snapshot created")


@bp.route("/<uid>/api/v1/snapshots/<int:snapshot_id>/restore", methods=["POST"])
@login_required
def snapshot_restore(uid: str, snapshot_id: int):
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


@bp.route("/<uid>/api/v1/snapshots/<int:snapshot_id>", methods=["DELETE"])
@login_required
def snapshot_delete(uid: str, snapshot_id: int):
    server = get_server(uid)
    snapshot = db.session.get(Snapshot, snapshot_id)
    if snapshot is None or snapshot.server_id != server.id:
        return fail("Snapshot not found", 404)
    provisioning.delete_snapshot(server, snapshot)
    db.session.commit()
    return ok(message="Snapshot deleted")


@bp.route("/<uid>/api/v1/backups", methods=["GET", "POST"])
@login_required
def backups(uid: str):
    server = get_server(uid)
    if request.method == "GET":
        rows = db.session.query(Backup).filter(Backup.server_id == server.id).order_by(Backup.created_at.desc()).all()
        return ok(backups=[row.public() for row in rows])
    data = body()
    try:
        backup = provisioning.create_backup(server, str(data.get("name") or ""), actor=current_user().username)
    except provisioning.ProvisioningError as exc:
        db.session.commit()
        return fail(str(exc), 400, code=exc.code)
    db.session.commit()
    return ok(backup=backup.public(), message="Backup complete")


@bp.route("/<uid>/api/v1/backups/<int:backup_id>", methods=["DELETE"])
@login_required
def backup_delete(uid: str, backup_id: int):
    server = get_server(uid)
    backup = db.session.get(Backup, backup_id)
    if backup is None or backup.server_id != server.id:
        return fail("Backup not found", 404)
    provisioning.delete_backup(server, backup)
    db.session.commit()
    return ok(message="Backup deleted")


@bp.route("/<uid>/api/v1/backups/<int:backup_id>/download")
@login_required
def backup_download(uid: str, backup_id: int):
    server = get_server(uid)
    backup = db.session.get(Backup, backup_id)
    if backup is None or backup.server_id != server.id:
        return fail("Backup not found", 404)
    if not backup.storage_path:
        return fail("This backup has no stored artefact (driver-level snapshot only)", 400)
    import os

    if not os.path.exists(backup.storage_path):
        return fail("Backup file is no longer on disk", 410)
    audit.record("backup.download", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"backup": backup.name})
    db.session.commit()

    def stream():
        with open(backup.storage_path, "rb") as handle:
            while True:
                chunk = handle.read(1024 * 256)
                if not chunk:
                    break
                yield chunk

    filename = f"{server.name}-{backup.name}.tar".replace(" ", "_")
    return Response(
        stream(),
        mimetype="application/x-tar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/<uid>/api/v1/events")
@login_required
def events(uid: str):
    server = get_server(uid)
    page, size = paging(20, 100)
    query = db.session.query(ServerEvent).filter(ServerEvent.server_id == server.id).order_by(ServerEvent.ts.desc())
    data = paginated(query, page, size, serializer=lambda row: row.public())
    return ok(**data)


@bp.route("/<uid>/api/v1/ssh-command")
@login_required
def ssh_command(uid: str):
    server = get_server(uid)
    return ok(
        command=server.ssh_command(),
        ip=server.primary_ip(),
        port=server.ssh_port,
        user=server.root_username,
        hostname=server.hostname,
    )
