"""SQLAlchemy models for SKVM PANEL.

Core tables are users, nodes, vps, jobs and settings, plus SKVM's
plans, os_images and audit_logs. New columns are nullable/additive and
are created by the lightweight migration in core.db.init_db(), so older
SKVM databases upgrade in place without data loss.
"""
from __future__ import annotations
from flask_login import UserMixin
from .extensions import db


class User(UserMixin, db.Model):
    __tablename__ = "users"
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="user")  # user | admin
    max_vps = db.Column(db.Integer, nullable=False, default=1)
    email = db.Column(db.String(255))
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"))
    is_suspended = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.String(40), nullable=False)
    last_login_at = db.Column(db.String(40))

    def get_id(self):
        return str(self.id)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def suspended(self) -> bool:
        return bool(self.is_suspended)

    def is_active(self):  # Flask-Login: suspended users cannot sign in
        return not self.suspended


class Node(db.Model):
    __tablename__ = "nodes"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    host = db.Column(db.String(255), nullable=False, default="127.0.0.1")
    port = db.Column(db.Integer, nullable=False, default=5556)
    token = db.Column(db.String(255), nullable=False)
    enabled = db.Column(db.Integer, nullable=False, default=1)
    backend = db.Column(db.String(20), nullable=False, default="auto")
    cpu_limit = db.Column(db.Integer)
    memory_limit_mb = db.Column(db.Integer)
    disk_limit_gb = db.Column(db.Integer)
    last_seen = db.Column(db.String(40))
    created_at = db.Column(db.String(40), nullable=False)


class Plan(db.Model):
    __tablename__ = "plans"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    max_vcpus = db.Column(db.Integer, nullable=False, default=2)
    max_memory_mb = db.Column(db.Integer, nullable=False, default=2048)
    max_disk_gb = db.Column(db.Integer, nullable=False, default=25)
    max_vps = db.Column(db.Integer, nullable=False, default=1)
    enabled = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.String(40), nullable=False)


class OSImage(db.Model):
    __tablename__ = "os_images"
    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(80), unique=True, nullable=False)
    label = db.Column(db.String(120), nullable=False)
    incus_image = db.Column(db.String(255), nullable=False)
    docker_image = db.Column(db.String(255), nullable=False)
    family = db.Column(db.String(40), nullable=False, default="debian")
    enabled = db.Column(db.Integer, nullable=False, default=1)
    created_at = db.Column(db.String(40), nullable=False)


class VPS(db.Model):
    __tablename__ = "vps"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    node_id = db.Column(db.Integer, db.ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    name = db.Column(db.String(55), unique=True, nullable=False)
    os_slug = db.Column(db.String(80), nullable=False)
    engine = db.Column(db.String(20), nullable=False, default="container")  # container | vm (incus)
    backend = db.Column(db.String(20), nullable=False, default="incus")     # incus | docker
    plan_id = db.Column(db.Integer, db.ForeignKey("plans.id"))
    vcpus = db.Column(db.Integer, nullable=False, default=2)
    memory_mb = db.Column(db.Integer, nullable=False, default=2048)
    disk_gb = db.Column(db.Integer, nullable=False, default=25)
    state = db.Column(db.String(20), nullable=False, default="creating")
    hostname = db.Column(db.String(120), nullable=False)
    ip_address = db.Column(db.String(64))
    container_id = db.Column(db.String(128))
    root_password_enc = db.Column(db.Text)
    tmate_ssh = db.Column(db.Text)
    tmate_web = db.Column(db.Text)
    desired_state = db.Column(db.String(20), nullable=False, default="running")
    last_error = db.Column(db.Text)
    created_at = db.Column(db.String(40), nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)

    owner = db.relationship("User", backref=db.backref("vps_list", lazy=True),
                            foreign_keys=[user_id])
    node = db.relationship("Node", backref=db.backref("vps_list", lazy=True))
    plan = db.relationship("Plan", backref=db.backref("vps_list", lazy=True))


class Job(db.Model):
    __tablename__ = "jobs"
    id = db.Column(db.Integer, primary_key=True)
    vps_id = db.Column(db.Integer, db.ForeignKey("vps.id", ondelete="CASCADE"), index=True)
    kind = db.Column(db.String(40), nullable=False)
    status = db.Column(db.String(20), nullable=False)  # queued|running|done|error
    progress = db.Column(db.Integer, nullable=False, default=0)
    message = db.Column(db.Text, nullable=False, default="")
    details = db.Column(db.Text, nullable=False, default="{}")
    created_at = db.Column(db.String(40), nullable=False)
    updated_at = db.Column(db.String(40), nullable=False)
    attempts = db.Column(db.Integer, nullable=False, default=0)


class Setting(db.Model):
    __tablename__ = "settings"
    key = db.Column(db.String(120), primary_key=True)
    value = db.Column(db.Text, nullable=False)


class AuditLog(db.Model):
    __tablename__ = "audit_logs"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer)
    username = db.Column(db.String(32))
    action = db.Column(db.String(80), nullable=False)
    target = db.Column(db.String(255))
    detail = db.Column(db.Text)
    ip = db.Column(db.String(64))
    created_at = db.Column(db.String(40), nullable=False, index=True)
