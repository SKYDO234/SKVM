"""Data-access facade for SKVM PANEL (Flask-SQLAlchemy implementation).

Preserves the Omega V4 function-level API (create_user, create_vps,
create_job, ...) so the provisioning worker, node agent and web app keep
working unchanged, while storage is now managed through SQLAlchemy.
Requires an active Flask app context (web requests have one; job_runner
and node.py push one explicitly).
"""
from __future__ import annotations
import json
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from werkzeug.security import generate_password_hash, check_password_hash

from .extensions import db as sqla
from .models import User, Node, Plan, OSImage, VPS, Job, Setting, AuditLog
from .config import HOSTNAME, DEFAULT_VCPUS, DEFAULT_MEMORY_MB, DEFAULT_DISK_GB, MAX_VPS_PER_USER
from .os_profiles import OS_PROFILES

# Re-export models for callers that used row-style access.
__all__ = ["User", "Node", "Plan", "OSImage", "VPS", "Job", "Setting", "AuditLog"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------- schema
_ADDED_COLUMNS = {
    "users": [("email", "VARCHAR(255)"), ("plan_id", "INTEGER"),
              ("is_suspended", "INTEGER NOT NULL DEFAULT 0"), ("last_login_at", "VARCHAR(40)")],
    "nodes": [("backend", "VARCHAR(20) NOT NULL DEFAULT 'auto'")],
    "vps": [("backend", "VARCHAR(20) NOT NULL DEFAULT 'incus'"), ("plan_id", "INTEGER"),
            ("container_id", "VARCHAR(128)"), ("root_password_enc", "TEXT"),
            ("tmate_ssh", "TEXT"), ("tmate_web", "TEXT"), ("last_error", "TEXT")],
}

_DEFAULT_SETTINGS = {
    "site_name": "SKVM PANEL",
    "site_tagline": "Virtual servers, managed simply.",
    "logo_url": "",
    "accent": "#7c6aff",
    "website_url": "",
    "discord_url": "",
    "support_email": "",
    "maintenance_mode": "false",
    "allow_registration": "true",
    "host_name": HOSTNAME,
    "default_vcpus": str(DEFAULT_VCPUS),
    "default_memory_mb": str(DEFAULT_MEMORY_MB),
    "default_disk_gb": str(DEFAULT_DISK_GB),
    "max_vps_per_user": str(MAX_VPS_PER_USER),
    "auto_update": "true",
    "node_cpu_policy": "unlimited",
    "default_backend": "auto",
}

_PLANS = [
    ("Starter", 1, 1024, 15, 1),
    ("Standard", 2, 2048, 25, 2),
    ("Pro", 4, 4096, 50, 4),
    ("Business", 8, 8192, 100, 8),
]


def init_db():
    sqla.create_all()
    # Additive migration for databases created by Omega V4 / early SKVM.
    engine = sqla.engine
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    with engine.begin() as con:
        for table, cols in _ADDED_COLUMNS.items():
            if table not in existing:
                continue
            have = {c["name"] for c in insp.get_columns(table)}
            for col, ddl in cols:
                if col not in have:
                    con.execute(text(f'ALTER TABLE {table} ADD COLUMN {col} {ddl}'))
    for key, value in _DEFAULT_SETTINGS.items():
        if sqla.session.get(Setting, key) is None:
            sqla.session.add(Setting(key=key, value=value))
    if not sqla.session.query(Plan).first():
        for name, cpu, mem, disk, maxv in _PLANS:
            sqla.session.add(Plan(name=name, max_vcpus=cpu, max_memory_mb=mem,
                                  max_disk_gb=disk, max_vps=maxv, created_at=utcnow()))
    for p in OS_PROFILES:
        if not sqla.session.query(OSImage).filter_by(slug=p.slug).first():
            sqla.session.add(OSImage(slug=p.slug, label=p.label, incus_image=p.image,
                                     docker_image=p.docker_image, family=p.family,
                                     created_at=utcnow()))
    sqla.session.commit()


# ---------------------------------------------------------------- settings
def get_setting(key: str, default=None):
    row = sqla.session.get(Setting, key)
    return row.value if row else default


def set_setting(key: str, value: str):
    row = sqla.session.get(Setting, key)
    if row is None:
        sqla.session.add(Setting(key=key, value=str(value)))
    else:
        row.value = str(value)
    sqla.session.commit()


def all_settings() -> dict:
    return {s.key: s.value for s in sqla.session.query(Setting).all()}


# ---------------------------------------------------------------- users
def create_user(username: str, password: str, role="user", max_vps=None, email=None, plan_id=None):
    username = (username or "").strip()
    if not 3 <= len(username) <= 32:
        raise ValueError("Username must be 3–32 characters")
    if not all(c.isalnum() or c in "_.-" for c in username):
        raise ValueError("Username may contain letters, digits, dot, dash and underscore only")
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    if max_vps is None:
        plan = sqla.session.get(Plan, plan_id) if plan_id else None
        max_vps = plan.max_vps if plan else MAX_VPS_PER_USER
    user = User(username=username, password_hash=generate_password_hash(password),
                role=role, max_vps=int(max_vps), email=(email or None),
                plan_id=plan_id, created_at=utcnow())
    sqla.session.add(user)
    try:
        sqla.session.commit()
    except Exception:
        sqla.session.rollback()
        raise ValueError("Username is already taken")
    return user.id


def verify_user(username: str, password: str):
    user = sqla.session.query(User).filter_by(username=(username or "").strip()).first()
    if user and check_password_hash(user.password_hash, password or ""):
        return user
    return None


def get_user(user_id):
    if user_id is None:
        return None
    return sqla.session.get(User, int(user_id))


def get_user_by_username(username: str):
    return sqla.session.query(User).filter_by(username=(username or "").strip()).first()


def all_users():
    return sqla.session.query(User).order_by(User.id.desc()).all()


def set_user_quota(user_id: int, max_vps: int):
    user = get_user(user_id)
    if user:
        user.max_vps = max(0, int(max_vps))
        sqla.session.commit()


def update_user(user_id: int, **fields):
    allowed = {"role", "max_vps", "email", "plan_id", "is_suspended", "last_login_at", "password_hash"}
    user = get_user(user_id)
    if not user:
        return None
    for k, v in fields.items():
        if k in allowed:
            setattr(user, k, v)
    sqla.session.commit()
    return user


def set_user_password(user_id: int, password: str):
    if len(password or "") < 8:
        raise ValueError("Password must be at least 8 characters")
    user = get_user(user_id)
    if not user:
        raise ValueError("User not found")
    user.password_hash = generate_password_hash(password)
    sqla.session.commit()


def delete_user(user_id: int):
    user = get_user(user_id)
    if user:
        sqla.session.delete(user)
        sqla.session.commit()


def count_vps(user_id: int) -> int:
    return sqla.session.query(VPS).filter_by(user_id=user_id).count()


def count_all_vps() -> int:
    return sqla.session.query(VPS).count()


def user_usage(user_id: int) -> dict:
    rows = sqla.session.query(VPS).filter_by(user_id=user_id).all()
    return {"count": len(rows), "vcpus": sum(r.vcpus for r in rows),
            "memory_mb": sum(r.memory_mb for r in rows), "disk_gb": sum(r.disk_gb for r in rows)}


# ---------------------------------------------------------------- nodes
def list_nodes():
    return sqla.session.query(Node).order_by(Node.id).all()


def get_node(node_id):
    return sqla.session.get(Node, int(node_id)) if node_id else None


def ensure_local_node(token: str, backend: str = "auto"):
    node = sqla.session.query(Node).filter_by(name=HOSTNAME).first()
    if node:
        node.token = token
        node.host = "127.0.0.1"
        node.port = 5556
        node.enabled = 1
        node.backend = backend
        node.last_seen = utcnow()
        sqla.session.commit()
        return node.id
    node = Node(name=HOSTNAME, host="127.0.0.1", port=5556, token=token, enabled=1,
                backend=backend, last_seen=utcnow(), created_at=utcnow())
    sqla.session.add(node)
    sqla.session.commit()
    return node.id


def create_node(name, host, port, token, backend="auto", enabled=1):
    node = Node(name=name.strip(), host=host.strip(), port=int(port), token=token,
                backend=backend, enabled=int(enabled), created_at=utcnow(), last_seen=None)
    sqla.session.add(node)
    sqla.session.commit()
    return node.id


def update_node(node_id, **fields):
    allowed = {"name", "host", "port", "enabled", "backend", "cpu_limit",
               "memory_limit_mb", "disk_limit_gb", "last_seen", "token"}
    node = get_node(node_id)
    if not node:
        return None
    for k, v in fields.items():
        if k in allowed:
            setattr(node, k, v)
    sqla.session.commit()
    return node


def delete_node(node_id):
    node = get_node(node_id)
    if node:
        sqla.session.delete(node)
        sqla.session.commit()


def set_node_quota(node_id: int, cpu_limit, memory_limit_mb, disk_limit_gb):
    def norm(v):
        return None if v in (None, "", "UNL", "unlimited") else int(v)
    update_node(node_id, cpu_limit=norm(cpu_limit), memory_limit_mb=norm(memory_limit_mb),
                disk_limit_gb=norm(disk_limit_gb))


# ---------------------------------------------------------------- plans
def list_plans(only_enabled=False):
    q = sqla.session.query(Plan)
    if only_enabled:
        q = q.filter_by(enabled=1)
    return q.order_by(Plan.id).all()


def get_plan(plan_id):
    return sqla.session.get(Plan, int(plan_id)) if plan_id else None


def create_plan(name, max_vcpus, max_memory_mb, max_disk_gb, max_vps):
    plan = Plan(name=name.strip(), max_vcpus=int(max_vcpus), max_memory_mb=int(max_memory_mb),
                max_disk_gb=int(max_disk_gb), max_vps=int(max_vps), created_at=utcnow())
    sqla.session.add(plan)
    sqla.session.commit()
    return plan.id


def update_plan(plan_id, **fields):
    allowed = {"name", "max_vcpus", "max_memory_mb", "max_disk_gb", "max_vps", "enabled"}
    plan = get_plan(plan_id)
    if not plan:
        return None
    for k, v in fields.items():
        if k in allowed:
            setattr(plan, k, v)
    sqla.session.commit()
    return plan


def delete_plan(plan_id):
    plan = get_plan(plan_id)
    if plan:
        sqla.session.delete(plan)
        sqla.session.commit()


# ---------------------------------------------------------------- OS images
def list_os_images(only_enabled=False):
    q = sqla.session.query(OSImage)
    if only_enabled:
        q = q.filter_by(enabled=1)
    return q.order_by(OSImage.id).all()


def get_os_image(slug: str):
    return sqla.session.query(OSImage).filter_by(slug=slug).first()


def create_os_image(slug, label, incus_image, docker_image, family="debian"):
    img = OSImage(slug=slug.strip(), label=label.strip(), incus_image=incus_image.strip(),
                  docker_image=docker_image.strip(), family=family, created_at=utcnow())
    sqla.session.add(img)
    sqla.session.commit()
    return img.id


def update_os_image(image_id, **fields):
    allowed = {"slug", "label", "incus_image", "docker_image", "family", "enabled"}
    img = sqla.session.get(OSImage, int(image_id))
    if not img:
        return None
    for k, v in fields.items():
        if k in allowed:
            setattr(img, k, v)
    sqla.session.commit()
    return img


def delete_os_image(image_id):
    img = sqla.session.get(OSImage, int(image_id))
    if img:
        sqla.session.delete(img)
        sqla.session.commit()


# ---------------------------------------------------------------- VPS
def create_vps(user_id, node_id, name, os_slug, engine, vcpus, memory_mb, disk_gb,
               hostname=None, backend="incus", plan_id=None):
    vps = VPS(user_id=user_id, node_id=node_id, name=name, os_slug=os_slug, engine=engine,
              backend=backend, plan_id=plan_id, vcpus=int(vcpus), memory_mb=int(memory_mb),
              disk_gb=int(disk_gb), state="creating", hostname=hostname or name,
              created_at=utcnow(), updated_at=utcnow())
    sqla.session.add(vps)
    sqla.session.commit()
    return vps.id


def get_vps(vps_id):
    return sqla.session.get(VPS, int(vps_id)) if vps_id else None


def get_vps_by_name(name: str):
    return sqla.session.query(VPS).filter_by(name=name).first()


def list_vps_for_user(user_id):
    return sqla.session.query(VPS).filter_by(user_id=user_id).order_by(VPS.id.desc()).all()


def list_all_vps():
    return sqla.session.query(VPS).order_by(VPS.id.desc()).all()


def update_vps(vps_id, **fields):
    allowed = {"state", "ip_address", "desired_state", "hostname", "vcpus", "memory_mb",
               "disk_gb", "container_id", "backend", "engine", "plan_id", "node_id",
               "root_password_enc", "tmate_ssh", "tmate_web", "last_error", "os_slug"}
    vps = get_vps(vps_id)
    if not vps:
        return None
    for k, v in fields.items():
        if k in allowed:
            setattr(vps, k, v)
    vps.updated_at = utcnow()
    sqla.session.commit()
    return vps


def delete_vps(vps_id):
    vps = get_vps(vps_id)
    if vps:
        sqla.session.delete(vps)
        sqla.session.commit()


# ---------------------------------------------------------------- jobs
def create_job(vps_id, kind, message="Queued"):
    job = Job(vps_id=vps_id, kind=kind, status="queued", progress=0, message=message,
              created_at=utcnow(), updated_at=utcnow())
    sqla.session.add(job)
    sqla.session.commit()
    return job.id


def get_job(job_id):
    return sqla.session.get(Job, int(job_id)) if job_id else None


def list_active_jobs(limit=50):
    return (sqla.session.query(Job).filter(Job.status.in_(["queued", "running"]))
            .order_by(Job.id.desc()).limit(limit).all())


def list_jobs_for_user(user_id, limit=25):
    return (sqla.session.query(Job).join(VPS, VPS.id == Job.vps_id)
            .filter(VPS.user_id == user_id).order_by(Job.id.desc()).limit(limit).all())


def list_jobs_for_vps(vps_id, limit=25):
    return sqla.session.query(Job).filter_by(vps_id=vps_id).order_by(Job.id.desc()).limit(limit).all()


def list_recent_jobs(limit=100):
    return sqla.session.query(Job).order_by(Job.id.desc()).limit(limit).all()


def update_job(job_id, **fields):
    allowed = {"status", "progress", "message", "details", "attempts"}
    job = get_job(job_id)
    if not job:
        return None
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "details" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        setattr(job, k, v)
    job.updated_at = utcnow()
    sqla.session.commit()
    return job


def job_details(job) -> dict:
    try:
        return json.loads(job.details or "{}")
    except Exception:
        return {}


def reset_stale_jobs():
    for job in sqla.session.query(Job).filter_by(status="running").all():
        job.status = "queued"
        job.message = "Recovered after service restart"
        job.updated_at = utcnow()
    sqla.session.commit()


# ---------------------------------------------------------------- audit
def audit(user, action: str, target: str = "", detail: str = "", ip: str = ""):
    try:
        sqla.session.add(AuditLog(
            user_id=getattr(user, "id", None), username=getattr(user, "username", None) or "",
            action=action, target=target or "", detail=(detail or "")[:2000],
            ip=ip or "", created_at=utcnow()))
        sqla.session.commit()
    except Exception:
        sqla.session.rollback()


def list_audit(limit=200):
    return sqla.session.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
