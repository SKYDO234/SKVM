"""Server lifecycle service.

Every mutation of a :class:`~skvm.models.Server` goes through here: quota
enforcement, backend calls, status bookkeeping, activity events, audit trail and
notifications.  Routes stay thin; the demo/docker/node drivers stay dumb.
"""

from __future__ import annotations

import logging
import re
import secrets
import string
from datetime import timedelta
from typing import Any

from flask import current_app

from ..backends import BackendError, ProvisionRequest, get_backend, pick_node
from ..backends.images import IMAGE_CATALOG, image_info
from ..db import db, utcnow
from ..models import Backup, Role, Server, ServerStatus, Severity, Snapshot, User
from . import audit
from .settings import settings

log = logging.getLogger("skvm.provisioning")

NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 _.\-]{1,62}$")
PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#%^&*()-_=+"


class ProvisioningError(RuntimeError):
    def __init__(self, message: str, code: str = "invalid") -> None:
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Quota accounting
# --------------------------------------------------------------------------- #
def user_usage(user: User) -> dict[str, float]:
    servers = db.session.query(Server).filter(Server.owner_id == user.id).all()
    return {
        "servers": len(servers),
        "cpu": sum(float(s.cpu_cores or 0) for s in servers),
        "ram_mb": sum(float(s.ram_mb or 0) for s in servers),
        "disk_gb": sum(float(s.disk_gb or 0) for s in servers),
        "snapshots": sum(len(s.snapshots) for s in servers),
        "snapshots_cap": sum(int(s.snapshot_limit or 0) for s in servers),
        "bandwidth_gb": round(sum(float(s.bandwidth_used_mb or 0) for s in servers) / 1024, 2),
        "monthly_cost": round(sum(float(s.monthly_price or 0) for s in servers if s.auto_renew), 2),
    }


def quota_snapshot(user: User) -> dict[str, Any]:
    """Quota vs. usage, ready for progress bars."""
    usage = user_usage(user)
    quotas = {**settings.quotas(), **(user.quotas or {})}
    rows = []
    for key, label, unit in (
        ("servers", "Servers", ""),
        ("cpu", "vCPU cores", "cores"),
        ("ram_mb", "Memory", "MB"),
        ("disk_gb", "Storage", "GB"),
        ("bandwidth_gb", "Bandwidth", "GB"),
    ):
        limit = float(quotas.get(key) or 0)
        used = float(usage.get(key) or 0)
        rows.append(
            {
                "key": key,
                "label": label,
                "unit": unit,
                "used": round(used, 2),
                "limit": limit,
                "percent": round(used / limit * 100, 1) if limit else 0.0,
            }
        )
    return {"rows": rows, "quotas": quotas, "usage": usage}


def check_quota(user: User, cpu: float, ram_mb: int, disk_gb: int, extra_servers: int = 1,
                ignore_server_id: int | None = None) -> None:
    quotas = {**settings.quotas(), **(user.quotas or {})}
    servers = db.session.query(Server).filter(Server.owner_id == user.id).all()
    if ignore_server_id:
        servers = [s for s in servers if s.id != ignore_server_id]
    used_cpu = sum(float(s.cpu_cores or 0) for s in servers)
    used_ram = sum(float(s.ram_mb or 0) for s in servers)
    used_disk = sum(float(s.disk_gb or 0) for s in servers)
    if len(servers) + extra_servers > float(quotas.get("servers") or 0):
        raise ProvisioningError(
            f"Server quota reached ({len(servers)}/{int(quotas.get('servers') or 0)}). "
            "Delete a server or ask an administrator for more capacity.",
            code="quota_servers",
        )
    if used_cpu + cpu > float(quotas.get("cpu") or 0) + 1e-6:
        raise ProvisioningError(
            f"CPU quota exceeded ({used_cpu:g}+{cpu:g} > {quotas.get('cpu')} cores)", code="quota_cpu"
        )
    if used_ram + ram_mb > float(quotas.get("ram_mb") or 0) + 1e-6:
        raise ProvisioningError(
            f"Memory quota exceeded ({used_ram + ram_mb:.0f} > {quotas.get('ram_mb')} MB)", code="quota_ram"
        )
    if used_disk + disk_gb > float(quotas.get("disk_gb") or 0) + 1e-6:
        raise ProvisioningError(
            f"Storage quota exceeded ({used_disk + disk_gb:.0f} > {quotas.get('disk_gb')} GB)", code="quota_disk"
        )
    cfg = current_app.config
    for value, maximum, label in (
        (cpu, cfg["MAX_CPU_PER_SERVER"], "vCPU"),
        (ram_mb, cfg["MAX_RAM_PER_SERVER"], "MB RAM"),
        (disk_gb, cfg["MAX_DISK_PER_SERVER"], "GB disk"),
    ):
        if value > maximum:
            raise ProvisioningError(f"Requested {value:g} {label} exceeds the platform maximum of {maximum:g}")


# --------------------------------------------------------------------------- #
# Validation helpers
# --------------------------------------------------------------------------- #
def validate_name(name: str) -> str:
    name = (name or "").strip()
    if not NAME_RE.match(name):
        raise ProvisioningError(
            "Name must be 2-63 characters: letters, digits, spaces, dot, dash or underscore"
        )
    if len(name) < 2:
        raise ProvisioningError("Name is too short")
    return name


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(length))


def slugify_hostname(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (name or "server").lower()).strip("-")
    return (slug or "server")[:40] + "-" + secrets.token_hex(2)


def validate_specs(cpu: float, ram_mb: int, disk_gb: int, swap_mb: int = 0) -> tuple[float, int, int, int]:
    try:
        cpu = float(cpu)
        ram_mb = int(ram_mb)
        disk_gb = int(disk_gb)
        swap_mb = int(swap_mb or 0)
    except (TypeError, ValueError):
        raise ProvisioningError("CPU, memory and disk must be numeric") from None
    if cpu <= 0 or ram_mb <= 0 or disk_gb <= 0:
        raise ProvisioningError("CPU, memory and disk must all be greater than zero")
    if cpu > 128 or ram_mb > 1_048_576 or disk_gb > 16_384:
        raise ProvisioningError("Requested resources are outside the supported range")
    if swap_mb < 0 or swap_mb > ram_mb * 2:
        raise ProvisioningError("Swap must be between 0 and 2× RAM")
    return cpu, ram_mb, disk_gb, swap_mb


def estimate_price(cpu: float, ram_mb: int, disk_gb: int) -> float:
    """Simple transparent pricing model shown to the user before creation."""
    return round(cpu * 4.0 + (ram_mb / 1024) * 2.0 + disk_gb * 0.08, 2)


# --------------------------------------------------------------------------- #
# Creation
# --------------------------------------------------------------------------- #
def create_server(
    owner: User,
    *,
    name: str,
    os_image: str,
    cpu: float,
    ram_mb: int,
    disk_gb: int,
    swap_mb: int = 0,
    server_type: str = "container",
    hostname: str = "",
    root_password: str = "",
    ssh_keys: list[str] | None = None,
    tags: list[str] | None = None,
    plan: str = "custom",
    node_id: int | None = None,
    monthly_price: float | None = None,
    expires_in_days: int | None = None,
    bandwidth_gb: int | None = None,
    notes: str = "",
) -> Server:
    from ..security import encrypt_secret

    name = validate_name(name)
    cpu, ram_mb, disk_gb, swap_mb = validate_specs(cpu, ram_mb, disk_gb, swap_mb)
    if os_image not in IMAGE_CATALOG:
        raise ProvisioningError(f"Unknown OS image '{os_image}'")
    if IMAGE_CATALOG[os_image].get("kvm_only") and server_type != "container":
        raise ProvisioningError("That image requires dedicated KVM virtualization, which is not enabled")

    check_quota(owner, cpu, ram_mb, disk_gb)

    from ..models import Node

    node = db.session.get(Node, node_id) if node_id else pick_node(cpu=cpu, ram_mb=ram_mb)
    backend = get_backend(node=node)
    uid = "srv-" + secrets.token_hex(6)
    password = root_password or generate_password()
    hostname = hostname or slugify_hostname(name)

    server = Server(
        uid=uid,
        owner_id=owner.id,
        node_id=node.id if node else None,
        name=name,
        hostname=hostname,
        os_image=os_image,
        server_type=server_type,
        status=ServerStatus.PROVISIONING.value,
        plan=plan,
        tags=tags or [],
        cpu_cores=cpu,
        ram_mb=ram_mb,
        disk_gb=disk_gb,
        swap_mb=swap_mb,
        root_password_enc=encrypt_secret(password),
        snapshot_limit=int((settings.quotas().get("snapshots") or 5)),
        monthly_price=float(monthly_price if monthly_price is not None else estimate_price(cpu, ram_mb, disk_gb)),
        currency=str(settings.get("currency", "USD")),
        auto_renew=True,
        expires_at=utcnow() + timedelta(days=int(expires_in_days or 30)),
        bandwidth_gb=int(bandwidth_gb or settings.quotas().get("bandwidth_gb") or 2000),
        notes=notes,
        driver=backend.name,
    )
    db.session.add(server)
    db.session.flush()

    audit.record(
        "server.create", target_type="server", target_id=server.uid, target_label=server.name,
        meta={"cpu": cpu, "ram_mb": ram_mb, "disk_gb": disk_gb, "image": os_image, "backend": backend.name,
              "node": node.name if node else None},
    )
    audit.server_event(server, "create", f"Provisioning requested on {backend.label}", status="ok")

    request = ProvisionRequest(
        name=name,
        hostname=hostname,
        os_image=os_image,
        cpu=cpu,
        ram_mb=ram_mb,
        disk_gb=disk_gb,
        swap_mb=swap_mb,
        root_password=password,
        ssh_keys=ssh_keys or [],
        server_type=server_type,
        tags=tags or [],
        owner=owner.username,
        uid=uid,
        extra={"backend": backend.name, "node": node.name if node else None},
    )

    try:
        info = backend.create(request)
    except BackendError as exc:
        server.status = ServerStatus.ERROR.value
        server.last_error = str(exc)[:250]
        server.provision_log = (f"{exc}\n{exc.detail}")[-4000:]
        audit.server_event(server, "create", f"Provisioning failed: {exc}", status="error")
        audit.record(
            "server.create", target_type="server", target_id=server.uid, target_label=server.name,
            result="error", meta={"error": str(exc)},
        )
        db.session.commit()
        raise ProvisioningError(f"Provisioning failed: {exc}", code=exc.code) from exc

    server.container_id = info.id
    server.ip_address = info.ip or server.ip_address
    server.ssh_port = info.ssh_port or 22
    server.status = ServerStatus.RUNNING.value
    server.last_boot_at = utcnow()
    server.provision_log = str(info.detail.get("bootstrap", ""))[-4000:] or "provisioned"
    runtime_password = info.detail.get("runtime_password")
    if runtime_password:
        server.root_password_enc = encrypt_secret(runtime_password)
    audit.server_event(server, "create", f"Server online at {server.primary_ip()}", status="ok")

    # Attach the owner's stored SSH keys for future rebuilds.
    try:
        from .mailer import send_server_ready

        if owner.email and settings.boolean("alert_email_enabled"):
            pass  # welcome mail is sent by the route layer after commit
    except Exception:  # pragma: no cover
        pass

    db.session.flush()
    return server


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #
def power_action(server: Server, action: str, actor: str = "system") -> Server:
    backend = get_backend(server)
    handler = {
        "start": backend.start,
        "stop": backend.stop,
        "restart": backend.restart,
        "suspend": backend.suspend,
        "resume": backend.resume,
    }.get(action)
    if handler is None:
        raise ProvisioningError(f"Unknown power action '{action}'", code="bad_action")

    if action == "start" and server.status == ServerStatus.SUSPENDED.value:
        raise ProvisioningError("Server is suspended — resume it or contact support", code="suspended")

    previous = server.status
    server.status = {
        "stop": ServerStatus.STOPPED.value,
        "suspend": ServerStatus.SUSPENDED.value,
        "restart": ServerStatus.RESTARTING.value,
    }.get(action, ServerStatus.RUNNING.value)
    db.session.flush()

    try:
        info = handler(server.container_id)
    except BackendError as exc:
        server.status = previous
        server.last_error = str(exc)[:250]
        audit.server_event(server, action, f"Failed: {exc}", status="error")
        raise ProvisioningError(str(exc), code=exc.code) from exc

    mapping = {
        "running": ServerStatus.RUNNING.value,
        "exited": ServerStatus.STOPPED.value,
        "created": ServerStatus.STOPPED.value,
        "paused": ServerStatus.SUSPENDED.value,
        "restarting": ServerStatus.RESTARTING.value,
        "dead": ServerStatus.ERROR.value,
    }
    server.status = mapping.get(info.status, server.status)
    if server.status == ServerStatus.RUNNING.value and action in ("start", "restart", "resume"):
        server.last_boot_at = utcnow()
    server.last_error = ""
    audit.server_event(server, action, f"Power action '{action}' completed", status="ok")
    audit.record(
        "server.power", target_type="server", target_id=server.uid, target_label=server.name,
        meta={"action": action, "actor": actor, "status": server.status},
    )
    db.session.flush()
    return server


def sync_status(server: Server) -> Server:
    """Reconcile the DB row with the driver's view of the world."""
    backend = get_backend(server)
    if not server.container_id or server.status in (ServerStatus.PROVISIONING.value, ServerStatus.DELETING.value):
        return server
    try:
        info = backend.inspect(server.container_id)
    except BackendError as exc:
        if exc.code == "not_found":
            server.status = ServerStatus.ERROR.value
            server.last_error = "Instance missing on the hypervisor"
        return server
    mapping = {
        "running": ServerStatus.RUNNING.value,
        "exited": ServerStatus.STOPPED.value,
        "created": ServerStatus.STOPPED.value,
        "paused": ServerStatus.SUSPENDED.value,
        "restarting": ServerStatus.RESTARTING.value,
        "dead": ServerStatus.ERROR.value,
    }
    new_status = mapping.get(info.status)
    if new_status and new_status != server.status and server.status != ServerStatus.SUSPENDED.value:
        audit.server_event(server, "sync", f"State changed {server.status} → {new_status}", status="ok")
        server.status = new_status
    if info.ip and not server.ip_address:
        server.ip_address = info.ip
    if info.ssh_port and info.ssh_port != server.ssh_port:
        server.ssh_port = info.ssh_port
    return server


def delete_server(server: Server, purge_data: bool = True) -> None:
    backend = get_backend(server)
    server.status = ServerStatus.DELETING.value
    db.session.flush()
    errors: list[str] = []
    if server.container_id:
        try:
            backend.remove(server.container_id, force=True)
        except BackendError as exc:
            errors.append(str(exc))
    if purge_data:
        for snapshot in list(server.snapshots):
            if snapshot.image_ref:
                try:
                    backend.delete_snapshot(snapshot.image_ref)
                except BackendError as exc:
                    errors.append(str(exc))
    audit.record(
        "server.delete", target_type="server", target_id=server.uid, target_label=server.name,
        result="ok" if not errors else "warn", meta={"errors": errors[:3]},
    )
    db.session.delete(server)
    db.session.flush()


def reinstall_server(server: Server, os_image: str, root_password: str = "", ssh_keys: list[str] | None = None) -> Server:
    if not settings.boolean("allow_user_reinstall"):
        raise ProvisioningError("Reinstalls are disabled by the administrator", code="disabled")
    if os_image not in IMAGE_CATALOG:
        raise ProvisioningError(f"Unknown OS image '{os_image}'")
    backend = get_backend(server)
    from ..security import encrypt_secret

    password = root_password or generate_password()
    request = ProvisionRequest(
        name=server.name,
        hostname=server.hostname,
        os_image=os_image,
        cpu=float(server.cpu_cores),
        ram_mb=int(server.ram_mb),
        disk_gb=int(server.disk_gb),
        swap_mb=int(server.swap_mb or 0),
        root_password=password,
        ssh_keys=ssh_keys or [],
        owner=server.owner.username if server.owner else "",
        uid=server.uid,
        extra={"instance_id": server.container_id},
    )
    server.status = ServerStatus.PROVISIONING.value
    db.session.flush()
    try:
        info = backend.reinstall(request)
    except BackendError as exc:
        server.status = ServerStatus.ERROR.value
        server.last_error = str(exc)[:250]
        audit.server_event(server, "reinstall", f"Failed: {exc}", status="error")
        raise ProvisioningError(str(exc), code=exc.code) from exc
    server.container_id = info.id
    server.os_image = os_image
    server.ip_address = info.ip or server.ip_address
    server.ssh_port = info.ssh_port or server.ssh_port
    server.root_password_enc = encrypt_secret(password)
    server.status = ServerStatus.RUNNING.value
    server.last_boot_at = utcnow()
    server.provision_log = "reinstalled"
    audit.server_event(server, "reinstall", f"Reinstalled with {image_info(os_image)['label']}", status="ok")
    audit.record(
        "server.reinstall", target_type="server", target_id=server.uid, target_label=server.name,
        meta={"image": os_image},
    )
    db.session.flush()
    return server


def resize_server(server: Server, cpu: float, ram_mb: int, disk_gb: int) -> Server:
    """Change resources; disk changes require a driver rebuild."""
    owner = server.owner
    check_quota(owner, 0, 0, 0, extra_servers=0, ignore_server_id=server.id)
    cpu, ram_mb, disk_gb, _ = validate_specs(cpu, ram_mb, disk_gb, server.swap_mb or 0)
    usage = user_usage(owner)
    quotas = {**settings.quotas(), **(owner.quotas or {})}
    delta_cpu = cpu - float(server.cpu_cores)
    delta_ram = ram_mb - int(server.ram_mb)
    delta_disk = disk_gb - int(server.disk_gb)
    if usage["cpu"] + delta_cpu > float(quotas.get("cpu") or 0) + 1e-6:
        raise ProvisioningError("Resize would exceed your CPU quota", code="quota_cpu")
    if usage["ram_mb"] + delta_ram > float(quotas.get("ram_mb") or 0) + 1e-6:
        raise ProvisioningError("Resize would exceed your memory quota", code="quota_ram")
    if usage["disk_gb"] + delta_disk > float(quotas.get("disk_gb") or 0) + 1e-6:
        raise ProvisioningError("Resize would exceed your storage quota", code="quota_disk")

    old = (server.cpu_cores, server.ram_mb, server.disk_gb)
    server.cpu_cores, server.ram_mb, server.disk_gb = cpu, ram_mb, disk_gb
    server.monthly_price = estimate_price(cpu, ram_mb, disk_gb)
    audit.server_event(
        server, "resize",
        f"{old[0]:g}vCPU/{old[1]}MB/{old[2]}GB → {cpu:g}vCPU/{ram_mb}MB/{disk_gb}GB",
        status="ok",
    )
    audit.record(
        "server.resize", target_type="server", target_id=server.uid, target_label=server.name,
        meta={"from": old, "to": (cpu, ram_mb, disk_gb)},
    )
    # Live drivers pick up CPU/RAM limits on next restart; flag it for the user.
    if (old[0], old[1]) != (cpu, ram_mb):
        audit.server_event(server, "resize", "New limits apply after the next restart", status="warn")
    db.session.flush()
    return server


def suspend_server(server: Server, reason: str = "policy") -> Server:
    try:
        power_action(server, "suspend")
    except ProvisioningError:
        server.status = ServerStatus.SUSPENDED.value
    server.status = ServerStatus.SUSPENDED.value
    server.suspended_reason = reason[:128]
    audit.record("server.suspend", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"reason": reason})
    db.session.flush()
    return server


def unsuspend_server(server: Server) -> Server:
    try:
        power_action(server, "resume")
    except ProvisioningError:
        pass
    server.status = ServerStatus.RUNNING.value
    server.suspended_reason = ""
    audit.record("server.unsuspend", target_type="server", target_id=server.uid, target_label=server.name)
    db.session.flush()
    return server


# --------------------------------------------------------------------------- #
# Snapshots & backups
# --------------------------------------------------------------------------- #
def create_snapshot(server: Server, name: str, description: str = "", actor: str = "system") -> Snapshot:
    if not settings.boolean("allow_user_snapshots") and actor not in ("admin", "owner"):
        raise ProvisioningError("Snapshots are disabled by the administrator", code="disabled")
    if len(server.snapshots) >= int(server.snapshot_limit or 0):
        raise ProvisioningError(
            f"Snapshot limit reached ({len(server.snapshots)}/{server.snapshot_limit}). Delete one first.",
            code="limit",
        )
    safe = re.sub(r"[^a-z0-9\-]+", "-", name.lower()).strip("-")[:40] or "snapshot"
    backend = get_backend(server)
    snapshot = Snapshot(
        server_id=server.id, name=name[:64], description=description[:255], state="creating",
        created_by=actor,
    )
    db.session.add(snapshot)
    db.session.flush()
    try:
        result = backend.snapshot(server.container_id, f"{server.uid}-{safe}-{snapshot.id}")
    except BackendError as exc:
        snapshot.state = "error"
        audit.server_event(server, "snapshot", f"Snapshot failed: {exc}", status="error")
        raise ProvisioningError(str(exc), code=exc.code) from exc
    snapshot.state = "ready"
    snapshot.image_ref = result.get("reference", "")
    snapshot.size_mb = float(result.get("size_mb") or 0)
    audit.server_event(server, "snapshot", f"Snapshot '{snapshot.name}' created ({snapshot.size_mb:.0f} MB)")
    audit.record("snapshot.create", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"snapshot": snapshot.name, "size_mb": snapshot.size_mb})
    db.session.flush()
    return snapshot


def restore_snapshot(server: Server, snapshot: Snapshot) -> Server:
    if not snapshot.image_ref:
        raise ProvisioningError("This snapshot has no image reference", code="invalid")
    backend = get_backend(server)
    try:
        info = backend.restore_snapshot(server.container_id, snapshot.image_ref)
    except BackendError as exc:
        audit.server_event(server, "restore", f"Restore failed: {exc}", status="error")
        raise ProvisioningError(str(exc), code=exc.code) from exc
    server.container_id = info.id or server.container_id
    server.status = ServerStatus.RUNNING.value
    server.last_boot_at = utcnow()
    audit.server_event(server, "restore", f"Restored from snapshot '{snapshot.name}'")
    audit.record("snapshot.restore", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"snapshot": snapshot.name})
    db.session.flush()
    return server


def delete_snapshot(server: Server, snapshot: Snapshot) -> None:
    backend = get_backend(server)
    if snapshot.image_ref:
        try:
            backend.delete_snapshot(snapshot.image_ref)
        except BackendError as exc:
            log.warning("snapshot image removal failed: %s", exc)
    audit.server_event(server, "snapshot", f"Snapshot '{snapshot.name}' deleted")
    audit.record("snapshot.delete", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"snapshot": snapshot.name})
    db.session.delete(snapshot)
    db.session.flush()


def create_backup(server: Server, name: str = "", kind: str = "manual", actor: str = "system") -> Backup:
    if not settings.boolean("allow_user_backups") and kind == "manual":
        raise ProvisioningError("Backups are disabled by the administrator", code="disabled")
    from ..config import INSTANCE_DIR

    backend = get_backend(server)
    folder = INSTANCE_DIR / "backups" / str(server.uid)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%d-%H%M%S")
    backup = Backup(
        server_id=server.id,
        name=name or f"{server.name}-{stamp}",
        state="creating",
        kind=kind,
        storage_path=str(folder / f"{stamp}.tar"),
        expires_at=utcnow() + timedelta(days=30),
    )
    db.session.add(backup)
    db.session.flush()
    try:
        result = backend.export_backup(server.container_id, backup.storage_path)
    except BackendError as exc:
        backup.state = "error"
        raise ProvisioningError(str(exc), code=exc.code) from exc
    backup.size_mb = float(result.get("size_mb") or 0)
    backup.state = "ready"
    backup.verified_at = utcnow()
    import hashlib

    try:
        digest = hashlib.sha256()
        with open(backup.storage_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        backup.checksum = digest.hexdigest()[:32]
    except OSError:
        backup.checksum = ""
    audit.server_event(server, "backup", f"Backup '{backup.name}' completed ({backup.size_mb:.1f} MB)")
    audit.record("backup.create", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"size_mb": backup.size_mb, "kind": kind})
    db.session.flush()
    return backup


def delete_backup(server: Server, backup: Backup) -> None:
    import os

    try:
        if backup.storage_path and os.path.exists(backup.storage_path):
            os.remove(backup.storage_path)
    except OSError as exc:  # pragma: no cover
        log.warning("could not remove backup file: %s", exc)
    audit.server_event(server, "backup", f"Backup '{backup.name}' deleted")
    audit.record("backup.delete", target_type="server", target_id=server.uid, target_label=server.name,
                 meta={"backup": backup.name})
    db.session.delete(backup)
    db.session.flush()


# --------------------------------------------------------------------------- #
# Fleet helpers
# --------------------------------------------------------------------------- #
def expire_overdue_servers() -> list[dict[str, Any]]:
    """Suspend servers past their due date (unless auto-renew is on/paid)."""
    actions: list[dict[str, Any]] = []
    overdue = (
        db.session.query(Server)
        .filter(Server.expires_at.isnot(None), Server.expires_at < utcnow())
        .filter(Server.status.notin_([ServerStatus.SUSPENDED.value, ServerStatus.DELETING.value]))
        .all()
    )
    for server in overdue:
        if server.auto_renew:
            server.expires_at = utcnow() + timedelta(days=30)
            audit.server_event(server, "billing", "Auto-renewed for another 30 days")
            actions.append({"server": server.uid, "action": "renewed"})
            continue
        suspend_server(server, reason="Payment overdue")
        actions.append({"server": server.uid, "action": "suspended"})
    return actions


def detach_node_servers(node) -> int:
    """Mark servers of an unreachable node as errored (keeps them listed)."""
    count = 0
    for server in node_servers(node):
        server.status = ServerStatus.ERROR.value
        server.last_error = f"Node '{node.name}' is offline"
        count += 1
    return count


def node_servers(node) -> list[Server]:
    return db.session.query(Server).filter(Server.node_id == node.id).all()


def severity_for(status: str) -> Severity:
    return {
        ServerStatus.RUNNING.value: Severity.INFO,
        ServerStatus.ERROR.value: Severity.CRITICAL,
        ServerStatus.SUSPENDED.value: Severity.WARNING,
    }.get(status, Severity.INFO)


def role_can_manage(user: User, server: Server) -> bool:
    return server.owner_id == user.id or user.can(Role.OPERATOR)
