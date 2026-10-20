"""First-run bootstrap: default settings, owner account, optional demo data."""

from __future__ import annotations

import logging
import os
import secrets
from datetime import timedelta
from typing import Any

from .db import db, utcnow
from .models import (
    AlertRule,
    Announcement,
    Node,
    Role,
    Server,
    ServerMetric,
    ServerStatus,
    Setting,
    User,
)
from .security import encrypt_secret, hash_password
from .services.settings import DEFAULTS, settings

log = logging.getLogger("skvm.bootstrap")

BANNER = """
   ███████╗██╗  ██╗██╗   ██╗███╗   ███╗
   ██╔════╝██║ ██╔╝██║   ██║████╗ ████║
   ███████╗█████╔╝ ██║   ██║██╔████╔██║
   ╚════██║██╔═██╗ ╚██╗ ██╔╝██║╚██╔╝██║
   ███████║██║  ██╗ ╚████╔╝ ██║ ╚═╝ ██║
   ╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚═╝     ╚═╝
"""


def ensure_defaults(app) -> dict[str, Any]:
    """Idempotently make the panel usable: settings, owner, worker flags."""
    result: dict[str, Any] = {"created_owner": False, "settings": 0}

    for key, value in DEFAULTS.items():
        if db.session.get(Setting, key) is None:
            db.session.add(Setting(key=key, value=value, updated_by="bootstrap"))
            result["settings"] += 1
    if result["settings"]:
        settings.invalidate()
    db.session.commit()

    owner = db.session.query(User).filter(User.role == Role.OWNER.value).first()
    if owner is None:
        username = os.environ.get("SKVM_ADMIN_USERNAME", "admin")
        email = os.environ.get("SKVM_ADMIN_EMAIL", "admin@localhost")
        password = os.environ.get("SKVM_ADMIN_PASSWORD") or secrets.token_urlsafe(12)
        existing = db.session.query(User).filter(User.username == username).first()
        if existing is not None:
            existing.role = Role.OWNER.value
            db.session.commit()
            owner = existing
        else:
            owner = User(
                username=username,
                email=email,
                full_name="Platform Owner",
                password_hash=hash_password(password),
                role=Role.OWNER.value,
                is_active=True,
                is_verified=True,
                quotas={**DEFAULTS["default_quotas"], "servers": 1000, "cpu": 512, "ram_mb": 1048576, "disk_gb": 20000},
                notes="Created automatically on first boot.",
            )
            db.session.add(owner)
            db.session.commit()
            result["created_owner"] = True
            print(BANNER)
            print(f"  SKVM first-run owner account created")
            print(f"    username: {owner.username}")
            print(f"    password: {password}")
            print("  Change it right after signing in.\n")
            if not os.environ.get("SKVM_ADMIN_PASSWORD"):
                with open(app.config["INSTANCE_DIR"] / "initial_admin.txt", "w", encoding="utf-8") as handle:
                    handle.write(f"{owner.username}\n{password}\n")
                os.chmod(app.config["INSTANCE_DIR"] / "initial_admin.txt", 0o600)

    # Starter alert rules for the owner (only once).
    if db.session.query(AlertRule).count() == 0 and owner is not None:
        for name, metric, comparator, threshold, duration, severity in (
            ("Fleet CPU high", "cpu", ">", 90, 300, "warning"),
            ("Fleet memory high", "ram", ">", 90, 300, "warning"),
            ("Disk nearly full", "disk", ">", 85, 600, "critical"),
            ("Server down", "offline", ">", 0.5, 120, "critical"),
        ):
            db.session.add(
                AlertRule(
                    user_id=owner.id,
                    name=name,
                    metric=metric,
                    comparator=comparator,
                    threshold=threshold,
                    duration_s=duration,
                    severity=severity,
                )
            )
        db.session.commit()

    # Register the local hypervisor so the fleet view has something to show.
    if app.config.get("BACKEND") in ("docker", "auto") and db.session.query(Node).count() == 0:
        from .backends import DockerBackend

        ok, detail = DockerBackend.available()
        db.session.add(
            Node(
                name="local-docker",
                endpoint="unix:///var/run/docker.sock",
                region="local",
                status="online" if ok else "offline",
                last_seen_at=utcnow() if ok else None,
                last_error="" if ok else detail[:250],
                version=detail[:32],
                labels={"role": "primary"},
                priority=10,
            )
        )
        db.session.commit()

    if app.config.get("DEMO_MODE") and db.session.query(Server).count() == 0 and owner is not None:
        seed_demo_data(owner=owner, server_count=4)

    return result


def seed_owner(username: str = "admin", password: str | None = None, email: str | None = None) -> User:
    user = db.session.query(User).filter(User.username == username).first()
    if user is None:
        user = User(
            username=username,
            email=email or f"{username}@localhost",
            full_name="Platform Owner",
            password_hash=hash_password(password or secrets.token_urlsafe(12)),
            role=Role.OWNER.value,
            is_active=True,
            is_verified=True,
        )
        db.session.add(user)
    else:
        user.role = Role.OWNER.value
        user.is_active = True
        if password:
            user.password_hash = hash_password(password)
    db.session.commit()
    return user


DEMO_SERVERS = [
    ("prod-edge-01", "ubuntu-24.04", 4, 8192, 80, "web-prod"),
    ("db-primary", "debian-12", 8, 16384, 320, "database"),
    ("staging-app", "ubuntu-22.04", 2, 4096, 60, "staging"),
    ("ci-runner-3", "alpine-3.20", 2, 2048, 40, "ci"),
    ("mail-relay", "rocky-9", 1, 2048, 25, "mail"),
]


def seed_demo_data(owner: User | None = None, server_count: int = 4, with_history: bool = True) -> int:
    """Create believable servers + metric history for previews and screenshots."""
    owner = owner or db.session.query(User).filter(User.role == Role.OWNER.value).first()
    if owner is None:
        owner = seed_owner()
    from .backends import get_backend
    from .services import provisioning

    created = 0
    for index, (name, image, cpu, ram, disk, tag) in enumerate(DEMO_SERVERS[: max(1, server_count)]):
        if db.session.query(Server).filter(Server.name == name).first():
            continue
        try:
            server = provisioning.create_server(
                owner,
                name=name,
                os_image=image,
                cpu=cpu,
                ram_mb=ram,
                disk_gb=disk,
                plan="pro",
                tags=[tag],
                notes="Seeded demo server — safe to delete.",
                expires_in_days=30 + index * 5,
            )
        except Exception as exc:
            log.warning("demo seed skipped %s: %s", name, exc)
            continue
        created += 1
        if with_history:
            _seed_history(server)
            if index == 3:
                server.status = ServerStatus.STOPPED.value
            if index == 4:
                server.tags = [*server.tags, "legacy"]
        db.session.commit()

    if db.session.query(Announcement).count() == 0:
        db.session.add(
            Announcement(
                title="Welcome to SKVM 2.0",
                body=(
                    "This build ships the glass cockpit UI: live metric streaming, an in-browser "
                    "root console, a file manager, snapshots, alerting and a signed node-agent API.\n\n"
                    "Demo infrastructure is simulated locally — plug in Docker or register a node "
                    "agent to go live."
                ),
                level="info",
                pinned=True,
                created_by="bootstrap",
            )
        )
    db.session.commit()
    return created


def _seed_history(server: Server, hours: int = 36, step_minutes: int = 10) -> None:
    """Give charts something to draw on a brand-new install."""
    import math
    import random

    rng = random.Random(server.name)
    now = utcnow()
    points = int(hours * 60 / step_minutes)
    for index in range(points):
        moment = now - timedelta(minutes=step_minutes * (points - index))
        phase = rng.random() * 6.28
        cpu = 18 + 16 * math.sin(index / 24 + phase) + rng.uniform(-5, 7) + (8 if server.name.startswith("prod") else 0)
        ram_pct = 42 + 12 * math.sin(index / 40 + phase) + rng.uniform(-4, 4)
        db.session.add(
            ServerMetric(
                server_id=server.id,
                ts=moment,
                cpu_pct=max(1.0, min(98.0, cpu)),
                ram_used_mb=max(64.0, float(server.ram_mb) * max(5.0, min(95.0, ram_pct)) / 100),
                ram_total_mb=float(server.ram_mb),
                disk_used_gb=float(server.disk_gb) * (0.24 + index / points * 0.3),
                disk_total_gb=float(server.disk_gb),
                net_rx_kbps=max(0.0, 260 + 220 * math.sin(index / 9 + phase) + rng.uniform(-80, 160)),
                net_tx_kbps=max(0.0, 180 + 160 * math.sin(index / 11 + phase) + rng.uniform(-60, 120)),
                load1=max(0.0, cpu / 25 + rng.uniform(-0.4, 0.6)),
                procs=rng.randint(40, 130),
            )
        )
    server.last_metrics = {
        "cpu_pct": 24.5,
        "ram_pct": 51.2,
        "disk_pct": 38.0,
        "load1": 1.1,
        "procs": 88,
        "ts": now.isoformat(),
    }
