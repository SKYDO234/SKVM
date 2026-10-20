"""SQLAlchemy models for SKVM.

Domain map
----------
Users & access ......... User, Session, ApiToken, SshKey, ResetToken
Infrastructure ......... Node, Server, ServerMetric, ServerEvent
Data protection ........ Snapshot, Backup
Operations ............. AuditLog, AlertRule, AlertEvent, Setting, Announcement
"""

from __future__ import annotations

import enum
import secrets
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base, human_delta, iso, utcnow


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class StrEnum(str, enum.Enum):
    def __str__(self) -> str:  # pragma: no cover
        return str(self.value)

    @classmethod
    def values(cls) -> list[str]:
        return [m.value for m in cls]


class Role(StrEnum):
    OWNER = "owner"        # platform owner — all powers
    ADMIN = "admin"        # manages users & every server
    OPERATOR = "operator"  # helpdesk: power actions, no user deletion
    USER = "user"          # owns and manages only their own servers


ROLE_RANK = {Role.USER: 1, Role.OPERATOR: 2, Role.ADMIN: 3, Role.OWNER: 4}


class ServerStatus(StrEnum):
    PROVISIONING = "provisioning"
    RUNNING = "running"
    RESTARTING = "restarting"
    STOPPED = "stopped"
    SUSPENDED = "suspended"
    ERROR = "error"
    DELETING = "deleting"


class ServerType(StrEnum):
    CONTAINER = "container"  # LXC-style Docker container (fast, dense)
    KVM = "kvm"              # hardware-virtualised instance (driver dependent)


class Severity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


class AlertState(StrEnum):
    OK = "ok"
    FIRING = "firing"
    RESOLVED = "resolved"


class TokenScope(StrEnum):
    READ = "read"
    WRITE = "write"
    POWER = "power"
    ADMIN = "admin"


SCOPE_DESCRIPTIONS = {
    TokenScope.READ: "Read servers, metrics, files listing, snapshots",
    TokenScope.WRITE: "Create/modify servers, snapshots, files, backups",
    TokenScope.POWER: "Start, stop, restart, suspend",
    TokenScope.ADMIN: "Everything, including user and node management",
}


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _new_uid(prefix: str = "") -> str:
    return f"{prefix}{secrets.token_hex(6)}"


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    company: Mapped[str] = mapped_column(String(128), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(16), default=Role.USER.value, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)
    credits: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")

    # quotas (JSON so operators can extend without migrations)
    quotas: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        default=lambda: {
            "servers": 5,
            "cpu": 16,
            "ram_mb": 32768,
            "disk_gb": 500,
            "snapshots": 10,
            "bandwidth_gb": 2000,
        },
    )

    # 2FA
    totp_secret: Mapped[str | None] = mapped_column(String(64), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_codes: Mapped[list[str]] = mapped_column(JSON, default=list)

    # hygiene
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_ip: Mapped[str] = mapped_column(String(64), default="")
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    theme: Mapped[str] = mapped_column(String(16), default="midnight")
    notify_prefs: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=lambda: {"email": True, "webhook": "", "downtime": True, "quota": True}
    )
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    servers: Mapped[list["Server"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    tokens: Mapped[list["ApiToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    sessions: Mapped[list["Session"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    ssh_keys: Mapped[list["SshKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    alert_rules: Mapped[list["AlertRule"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    # -- convenience ----------------------------------------------------- #
    @property
    def role_rank(self) -> int:
        return ROLE_RANK.get(Role(self.role), 0)

    @property
    def is_staff(self) -> bool:
        return self.role in (Role.ADMIN.value, Role.OWNER.value)

    @property
    def is_owner(self) -> bool:
        return self.role == Role.OWNER.value

    @property
    def quota(self) -> dict[str, Any]:
        return self.quotas or {}

    @property
    def initials(self) -> str:
        source = self.full_name or self.username
        parts = [p for p in source.replace("_", " ").split() if p]
        if not parts:
            return "?"
        if len(parts) == 1:
            return parts[0][:2].upper()
        return (parts[0][0] + parts[-1][0]).upper()

    def can(self, role: Role | str) -> bool:
        return self.role_rank >= ROLE_RANK[Role(str(role))]

    def public(self, include_email: bool = True) -> dict[str, Any]:
        data = {
            "id": self.id,
            "username": self.username,
            "full_name": self.full_name,
            "role": self.role,
            "initials": self.initials,
            "is_active": self.is_active,
            "is_verified": self.is_verified,
            "totp_enabled": self.totp_enabled,
            "credits": self.credits,
            "currency": self.currency,
            "quotas": self.quota,
            "created_at": iso(self.created_at),
            "last_login_at": iso(self.last_login_at),
            "last_login_human": human_delta(self.last_login_at),
            "server_count": len(self.servers) if "servers" in self.__dict__ else None,
        }
        if include_email:
            data["email"] = self.email
            data["company"] = self.company
            data["notes"] = self.notes
        return data

    def is_locked(self) -> bool:
        if self.locked_until is None:
            return False
        locked = self.locked_until
        if locked.tzinfo is None:
            locked = locked.replace(tzinfo=utcnow().tzinfo)
        return locked > utcnow()


class Session(Base):
    """A logged-in browser session (revocable by the user or an admin)."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    device: Mapped[str] = mapped_column(String(64), default="Browser")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_by: Mapped[str] = mapped_column(String(64), default="")

    user: Mapped[User] = relationship(back_populates="sessions")

    @property
    def is_active(self) -> bool:
        if self.revoked_at is not None:
            return False
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=utcnow().tzinfo)
        return exp > utcnow()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ip": self.ip,
            "device": self.device,
            "user_agent": self.user_agent,
            "created_at": iso(self.created_at),
            "last_seen_at": iso(self.last_seen_at),
            "last_seen_human": human_delta(self.last_seen_at),
            "active": self.is_active,
        }


class ApiToken(Base):
    """Hashed bearer token with a coarse scope set."""

    __tablename__ = "api_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    prefix: Mapped[str] = mapped_column(String(16), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    scopes: Mapped[list[str]] = mapped_column(JSON, default=lambda: [TokenScope.READ.value])
    ip_allowlist: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    call_count: Mapped[int] = mapped_column(Integer, default=0)

    user: Mapped[User] = relationship(back_populates="tokens")

    def has_scope(self, scope: TokenScope | str) -> bool:
        if TokenScope.ADMIN.value in (self.scopes or []):
            return True
        return str(scope) in (self.scopes or [])

    @property
    def expired(self) -> bool:
        if self.expires_at is None:
            return False
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=utcnow().tzinfo)
        return exp <= utcnow()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "prefix": self.prefix,
            "masked": f"{self.prefix}…{self.token_hash[-4:]}",
            "scopes": self.scopes or [],
            "ip_allowlist": self.ip_allowlist or [],
            "created_at": iso(self.created_at),
            "last_used_at": iso(self.last_used_at),
            "last_used_human": human_delta(self.last_used_at),
            "expires_at": iso(self.expires_at),
            "revoked": self.revoked_at is not None,
            "expired": self.expired,
            "call_count": self.call_count,
        }


class SshKey(Base):
    """Public key injected into new servers' ``authorized_keys``."""

    __tablename__ = "ssh_keys"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint", name="uq_key_per_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    public_key: Mapped[str] = mapped_column(Text)
    fingerprint: Mapped[str] = mapped_column(String(128), index=True)
    key_type: Mapped[str] = mapped_column(String(32), default="ssh-ed25519")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    user: Mapped[User] = relationship(back_populates="ssh_keys")

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "fingerprint": self.fingerprint,
            "key_type": self.key_type,
            "comment": self.public_key.split()[-1] if len(self.public_key.split()) > 2 else "",
            "created_at": iso(self.created_at),
            "created_human": human_delta(self.created_at),
        }


class ResetToken(Base):
    __tablename__ = "reset_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True)
    purpose: Mapped[str] = mapped_column(String(24), default="password")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# --------------------------------------------------------------------------- #
# Infrastructure
# --------------------------------------------------------------------------- #
class Node(Base):
    """A remote hypervisor running ``skvm-node``."""

    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    endpoint: Mapped[str] = mapped_column(String(255))
    region: Mapped[str] = mapped_column(String(64), default="local")
    key_id: Mapped[str] = mapped_column(String(32), index=True, default=lambda: _new_uid("sk_"))
    key_hash: Mapped[str] = mapped_column(String(128), default="")
    tls_verify: Mapped[bool] = mapped_column(Boolean, default=True)
    capacity_cpu: Mapped[float] = mapped_column(Float, default=0)
    capacity_ram_mb: Mapped[int] = mapped_column(Integer, default=0)
    capacity_disk_gb: Mapped[int] = mapped_column(Integer, default=0)
    labels: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # online|offline|unknown
    version: Mapped[str] = mapped_column(String(32), default="")
    overload: Mapped[float] = mapped_column(Float, default=0)  # used cpu cores on the node
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(String(255), default="")
    priority: Mapped[int] = mapped_column(Integer, default=100)  # lower = scheduled first
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    def public(self, reveal_key_id: bool = False) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "endpoint": self.endpoint,
            "region": self.region,
            "key_id": self.key_id if reveal_key_id else self.key_id,
            "tls_verify": self.tls_verify,
            "capacity_cpu": self.capacity_cpu,
            "capacity_ram_mb": self.capacity_ram_mb,
            "capacity_disk_gb": self.capacity_disk_gb,
            "labels": self.labels or {},
            "status": self.status,
            "version": self.version,
            "overload": self.overload,
            "last_seen_at": iso(self.last_seen_at),
            "last_seen_human": human_delta(self.last_seen_at),
            "last_error": self.last_error,
            "priority": self.priority,
            "created_at": iso(self.created_at),
        }


class Server(Base):
    """A virtual private server (one container / VM)."""

    __tablename__ = "servers"
    __table_args__ = (
        Index("ix_servers_owner_status", "owner_id", "status"),
        UniqueConstraint("uid", name="uq_servers_uid"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    uid: Mapped[str] = mapped_column(String(24), default=lambda: _new_uid("srv-"), index=True)
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int | None] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)

    # identity
    name: Mapped[str] = mapped_column(String(64))
    hostname: Mapped[str] = mapped_column(String(64), default="")
    os_image: Mapped[str] = mapped_column(String(64), default="ubuntu-24.04")
    server_type: Mapped[str] = mapped_column(String(16), default=ServerType.CONTAINER.value)
    status: Mapped[str] = mapped_column(String(16), default=ServerStatus.PROVISIONING.value, index=True)
    plan: Mapped[str] = mapped_column(String(32), default="custom")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    notes: Mapped[str] = mapped_column(Text, default="")

    # resources
    cpu_cores: Mapped[float] = mapped_column(Float, default=2)
    ram_mb: Mapped[int] = mapped_column(Integer, default=4096)
    disk_gb: Mapped[int] = mapped_column(Integer, default=50)
    swap_mb: Mapped[int] = mapped_column(Integer, default=0)
    cpu_pin: Mapped[str] = mapped_column(String(64), default="")
    backup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    backup_schedule: Mapped[str] = mapped_column(String(32), default="0 3 * * *")
    snapshot_limit: Mapped[int] = mapped_column(Integer, default=5)
    bandwidth_gb: Mapped[int] = mapped_column(Integer, default=2000)

    # access
    ip_address: Mapped[str] = mapped_column(String(64), default="")
    ipv6_address: Mapped[str] = mapped_column(String(64), default="")
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    root_username: Mapped[str] = mapped_column(String(32), default="root")
    root_password_enc: Mapped[str] = mapped_column(Text, default="")
    console_token: Mapped[str] = mapped_column(String(64), default=lambda: secrets.token_urlsafe(24))

    # runtime handle
    container_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    driver: Mapped[str] = mapped_column(String(16), default="docker")
    provision_log: Mapped[str] = mapped_column(Text, default="")
    last_error: Mapped[str] = mapped_column(String(255), default="")

    # billing / lifecycle
    monthly_price: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(8), default="USD")
    auto_renew: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    suspended_reason: Mapped[str] = mapped_column(String(128), default="")

    # observed
    last_metrics: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    bandwidth_used_mb: Mapped[float] = mapped_column(Float, default=0.0)
    uptime_seconds: Mapped[int] = mapped_column(Integer, default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    last_boot_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    owner: Mapped[User] = relationship(back_populates="servers")
    node: Mapped[Node | None] = relationship()
    metrics: Mapped[list["ServerMetric"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )
    events: Mapped[list["ServerEvent"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )
    snapshots: Mapped[list["Snapshot"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )
    backups: Mapped[list["Backup"]] = relationship(
        back_populates="server", cascade="all, delete-orphan", passive_deletes=True
    )

    # -- helpers --------------------------------------------------------- #
    @property
    def busy(self) -> bool:
        return self.status in (
            ServerStatus.PROVISIONING.value,
            ServerStatus.RESTARTING.value,
            ServerStatus.DELETING.value,
        )

    @property
    def running(self) -> bool:
        return self.status == ServerStatus.RUNNING.value

    @property
    def power_allowed(self) -> bool:
        return self.status not in (
            ServerStatus.PROVISIONING.value,
            ServerStatus.DELETING.value,
            ServerStatus.SUSPENDED.value,
        )

    @property
    def expires_in_days(self) -> int | None:
        if self.expires_at is None:
            return None
        exp = self.expires_at
        if exp.tzinfo is None:
            exp = exp.replace(tzinfo=utcnow().tzinfo)
        return (exp - utcnow()).days

    @property
    def bandwidth_used_gb(self) -> float:
        return round((self.bandwidth_used_mb or 0) / 1024, 3)

    def primary_ip(self) -> str:
        return self.ip_address or self.ipv6_address or "—"

    def ssh_command(self) -> str:
        port = "" if self.ssh_port == 22 else f" -p {self.ssh_port}"
        return f"ssh {self.root_username}@{self.primary_ip()}{port}"

    def public(self, detail: bool = False, include_secrets: bool = False) -> dict[str, Any]:
        metrics = self.last_metrics or {}
        data: dict[str, Any] = {
            "uid": self.uid,
            "id": self.id,
            "name": self.name,
            "hostname": self.hostname,
            "status": self.status,
            "status_label": self.status.title(),
            "busy": self.busy,
            "running": self.running,
            "power_allowed": self.power_allowed,
            "os_image": self.os_image,
            "server_type": self.server_type,
            "plan": self.plan,
            "tags": self.tags or [],
            "cpu_cores": self.cpu_cores,
            "ram_mb": self.ram_mb,
            "disk_gb": self.disk_gb,
            "swap_mb": self.swap_mb,
            "bandwidth_gb": self.bandwidth_gb,
            "bandwidth_used_gb": self.bandwidth_used_gb,
            "ip_address": self.ip_address,
            "ipv6_address": self.ipv6_address,
            "primary_ip": self.primary_ip(),
            "ssh_port": self.ssh_port,
            "root_username": self.root_username,
            "ssh_command": self.ssh_command(),
            "driver": self.driver,
            "monthly_price": self.monthly_price,
            "currency": self.currency,
            "auto_renew": self.auto_renew,
            "expires_at": iso(self.expires_at),
            "expires_in_days": self.expires_in_days,
            "snapshot_limit": self.snapshot_limit,
            "backup_enabled": self.backup_enabled,
            "backup_schedule": self.backup_schedule,
            "suspended_reason": self.suspended_reason,
            "owner": {
                "id": self.owner.id,
                "username": self.owner.username,
                "initials": self.owner.initials,
                "role": self.owner.role,
            }
            if self.owner is not None
            else None,
            "node": {"id": self.node.id, "name": self.node.name, "region": self.node.region} if self.node else None,
            "metrics": {
                "cpu_pct": metrics.get("cpu_pct"),
                "ram_pct": metrics.get("ram_pct"),
                "disk_pct": metrics.get("disk_pct"),
                "ram_used_mb": metrics.get("ram_used_mb"),
                "disk_used_gb": metrics.get("disk_used_gb"),
                "net_rx_kbps": metrics.get("net_rx_kbps"),
                "net_tx_kbps": metrics.get("net_tx_kbps"),
                "load1": metrics.get("load1"),
                "procs": metrics.get("procs"),
                "sampled_at": metrics.get("ts"),
            },
            "created_at": iso(self.created_at),
            "created_human": human_delta(self.created_at),
            "updated_at": iso(self.updated_at),
            "uptime_seconds": self.uptime_seconds,
            "last_error": self.last_error,
        }
        if self.created_at:
            data["age_days"] = max(0, (utcnow() - (
                self.created_at if self.created_at.tzinfo else self.created_at.replace(tzinfo=utcnow().tzinfo)
            )).days)
        if detail:
            data["notes"] = self.notes
            data["provision_log"] = self.provision_log
            data["cpu_pin"] = self.cpu_pin
            data["last_boot_at"] = iso(self.last_boot_at)
            data["snapshot_count"] = len(self.snapshots) if "snapshots" in self.__dict__ else None
            data["backup_count"] = len(self.backups) if "backups" in self.__dict__ else None
        if include_secrets:
            from .security import decrypt_secret

            data["console_token"] = self.console_token
            data["root_password"] = decrypt_secret(self.root_password_enc)
        return data


class ServerMetric(Base):
    """Rolled-up samples persisted for the history charts."""

    __tablename__ = "server_metrics"
    __table_args__ = (Index("ix_metrics_server_ts", "server_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    cpu_pct: Mapped[float] = mapped_column(Float, default=0.0)
    ram_used_mb: Mapped[float] = mapped_column(Float, default=0.0)
    ram_total_mb: Mapped[float] = mapped_column(Float, default=0.0)
    disk_used_gb: Mapped[float] = mapped_column(Float, default=0.0)
    disk_total_gb: Mapped[float] = mapped_column(Float, default=0.0)
    net_rx_kbps: Mapped[float] = mapped_column(Float, default=0.0)
    net_tx_kbps: Mapped[float] = mapped_column(Float, default=0.0)
    load1: Mapped[float] = mapped_column(Float, default=0.0)
    procs: Mapped[int] = mapped_column(Integer, default=0)

    server: Mapped[Server] = relationship(back_populates="metrics")

    def chart_row(self) -> dict[str, Any]:
        return {
            "ts": iso(self.ts),
            "cpu": round(self.cpu_pct, 2),
            "ram": round((self.ram_used_mb / self.ram_total_mb * 100) if self.ram_total_mb else 0, 2),
            "disk": round((self.disk_used_gb / self.disk_total_gb * 100) if self.disk_total_gb else 0, 2),
            "rx": round(self.net_rx_kbps, 2),
            "tx": round(self.net_tx_kbps, 2),
            "load": round(self.load1, 2),
        }


class ServerEvent(Base):
    __tablename__ = "server_events"
    __table_args__ = (Index("ix_events_server_ts", "server_id", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    action: Mapped[str] = mapped_column(String(48))
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok|warn|error
    message: Mapped[str] = mapped_column(String(255), default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    server: Mapped[Server] = relationship(back_populates="events")

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": iso(self.ts),
            "human": human_delta(self.ts),
            "action": self.action,
            "status": self.status,
            "message": self.message,
            "actor": self.actor,
        }


class Snapshot(Base):
    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    description: Mapped[str] = mapped_column(String(255), default="")
    image_ref: Mapped[str] = mapped_column(String(128), default="")
    size_mb: Mapped[float] = mapped_column(Float, default=0.0)
    state: Mapped[str] = mapped_column(String(16), default="ready")  # creating|ready|error
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(64), default="system")

    server: Mapped[Server] = relationship(back_populates="snapshots")

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "size_mb": round(self.size_mb, 1),
            "size_human": f"{self.size_mb / 1024:.2f} GB" if self.size_mb > 1024 else f"{self.size_mb:.0f} MB",
            "state": self.state,
            "created_at": iso(self.created_at),
            "created_human": human_delta(self.created_at),
            "created_by": self.created_by,
            "image_ref": self.image_ref,
        }


class Backup(Base):
    __tablename__ = "backups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(String(255), default="")
    size_mb: Mapped[float] = mapped_column(Float, default=0.0)
    checksum: Mapped[str] = mapped_column(String(128), default="")
    state: Mapped[str] = mapped_column(String(16), default="ready")
    kind: Mapped[str] = mapped_column(String(16), default="manual")  # manual|scheduled
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    server: Mapped[Server] = relationship(back_populates="backups")

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size_mb": round(self.size_mb, 1),
            "size_human": f"{self.size_mb / 1024:.2f} GB" if self.size_mb > 1024 else f"{self.size_mb:.0f} MB",
            "state": self.state,
            "kind": self.kind,
            "checksum": self.checksum,
            "verified": self.verified_at is not None,
            "created_at": iso(self.created_at),
            "created_human": human_delta(self.created_at),
            "expires_at": iso(self.expires_at),
        }


# --------------------------------------------------------------------------- #
# Operations
# --------------------------------------------------------------------------- #
class AuditLog(Base):
    __tablename__ = "audit_log"
    __table_args__ = (Index("ix_audit_ts", "ts"), Index("ix_audit_actor", "actor_id"))

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(64), default="system")
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="")
    target_id: Mapped[str] = mapped_column(String(64), default="")
    target_label: Mapped[str] = mapped_column(String(128), default="")
    result: Mapped[str] = mapped_column(String(16), default="ok")
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(255), default="")
    channel: Mapped[str] = mapped_column(String(16), default="web")  # web|api|cli|system
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "ts": iso(self.ts),
            "human": human_delta(self.ts),
            "actor": self.actor,
            "actor_id": self.actor_id,
            "action": self.action,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "target_label": self.target_label,
            "result": self.result,
            "ip": self.ip,
            "channel": self.channel,
            "meta": self.meta or {},
        }


class AlertRule(Base):
    __tablename__ = "alert_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(64))
    metric: Mapped[str] = mapped_column(String(24), default="cpu")  # cpu|ram|disk|net_rx|net_tx|offline|load
    comparator: Mapped[str] = mapped_column(String(4), default=">")  # > | <
    threshold: Mapped[float] = mapped_column(Float, default=90)
    duration_s: Mapped[int] = mapped_column(Integer, default=300)
    severity: Mapped[str] = mapped_column(String(16), default=Severity.WARNING.value)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_webhook: Mapped[str] = mapped_column(String(255), default="")
    state: Mapped[str] = mapped_column(String(16), default=AlertState.OK.value)
    last_value: Mapped[float] = mapped_column(Float, default=0.0)
    breach_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fire_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User] = relationship(back_populates="alert_rules")
    server: Mapped[Server | None] = relationship()
    events: Mapped[list["AlertEvent"]] = relationship(
        back_populates="rule", cascade="all, delete-orphan", passive_deletes=True
    )

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "metric": self.metric,
            "comparator": self.comparator,
            "threshold": self.threshold,
            "duration_s": self.duration_s,
            "severity": self.severity,
            "enabled": self.enabled,
            "notify_email": self.notify_email,
            "notify_webhook": self.notify_webhook,
            "state": self.state,
            "last_value": self.last_value,
            "breach_since": iso(self.breach_since),
            "last_fired_at": iso(self.last_fired_at),
            "fire_count": self.fire_count,
            "server_uid": self.server.uid if self.server else None,
            "server_name": self.server.name if self.server else "All servers",
            "target": self.server.name if self.server else "All servers",
            "expression": f"{self.metric.upper()} {self.comparator} {self.threshold:g}",
            "created_at": iso(self.created_at),
        }


class AlertEvent(Base):
    __tablename__ = "alert_events"
    __table_args__ = (Index("ix_alerts_ts", "ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("alert_rules.id", ondelete="CASCADE"), index=True)
    server_id: Mapped[int | None] = mapped_column(
        ForeignKey("servers.id", ondelete="CASCADE"), nullable=True, index=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    state: Mapped[str] = mapped_column(String(16), default=AlertState.FIRING.value)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    severity: Mapped[str] = mapped_column(String(16), default=Severity.WARNING.value)
    message: Mapped[str] = mapped_column(String(255), default="")
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    acknowledged_by: Mapped[str] = mapped_column(String(64), default="")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    rule: Mapped[AlertRule] = relationship(back_populates="events")
    server: Mapped[Server | None] = relationship()

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "rule_id": self.rule_id,
            "rule": self.rule.name if self.rule else "—",
            "server": self.server.name if self.server else "—",
            "server_uid": self.server.uid if self.server else None,
            "ts": iso(self.ts),
            "human": human_delta(self.ts),
            "state": self.state,
            "severity": self.severity,
            "value": self.value,
            "message": self.message,
            "acknowledged": self.acknowledged_at is not None,
            "acknowledged_by": self.acknowledged_by,
            "resolved_at": iso(self.resolved_at),
        }


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str] = mapped_column(String(64), default="system")


class Announcement(Base):
    __tablename__ = "announcements"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, default="")
    level: Mapped[str] = mapped_column(String(16), default=Severity.INFO.value)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    audience: Mapped[str] = mapped_column(String(16), default="all")  # all|staff
    created_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "level": self.level,
            "active": self.active,
            "pinned": self.pinned,
            "audience": self.audience,
            "created_by": self.created_by,
            "created_at": iso(self.created_at),
            "created_human": human_delta(self.created_at),
            "expires_at": iso(self.expires_at),
        }


class Incident(Base):
    """Status-page incident record."""

    __tablename__ = "incidents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    body: Mapped[str] = mapped_column(Text, default="")
    severity: Mapped[str] = mapped_column(String(16), default=Severity.INFO.value)
    status: Mapped[str] = mapped_column(String(16), default="investigating")  # investigating|identified|monitoring|resolved
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "body": self.body,
            "severity": self.severity,
            "status": self.status,
            "started_at": iso(self.started_at),
            "resolved_at": iso(self.resolved_at),
            "resolved": self.resolved_at is not None,
        }


class FileEntry(Base):
    """Lightweight index of files uploaded through the web file manager.

    Real content lives on disk (or inside the container); this table keeps the
    metadata needed for sharing links, listings and audit trails.
    """

    __tablename__ = "file_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(String(512))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    share_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)
    share_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


def utc_in(days: int = 0, hours: int = 0, minutes: int = 0) -> datetime:
    return utcnow() + timedelta(days=days, hours=hours, minutes=minutes)
