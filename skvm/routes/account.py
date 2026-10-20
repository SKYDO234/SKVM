"""Account area: profile, security (2FA + sessions), API tokens, SSH keys."""

from __future__ import annotations

import base64
import hashlib
import logging
from datetime import timedelta

from flask import Blueprint, current_app, jsonify, render_template, request

from ..db import db, iso, utcnow
from ..models import ApiToken, Role, Server, Session, SshKey, TokenScope, User
from ..security import (
    SCOPE_DESCRIPTIONS,
    client_ip,
    current_session_id,
    current_user,
    end_session,
    encrypt_secret,
    generate_api_token,
    generate_recovery_codes,
    hash_password,
    login_required,
    new_totp_secret,
    password_problems,
    password_strength,
    sha256,
    totp_uri,
    verify_password,
    verify_totp,
)
from ..services import audit, metrics, provisioning
from ..services.settings import settings
from .helpers import body, fail, ok

log = logging.getLogger("skvm.account")

bp = Blueprint("account", __name__, url_prefix="/account")


@bp.route("/")
@login_required
def index():
    user = current_user()
    tokens = (
        db.session.query(ApiToken)
        .filter(ApiToken.user_id == user.id)
        .order_by(ApiToken.created_at.desc())
        .all()
    )
    sessions = (
        db.session.query(Session)
        .filter(Session.user_id == user.id, Session.revoked_at.is_(None))
        .order_by(Session.last_seen_at.desc())
        .all()
    )
    keys = db.session.query(SshKey).filter(SshKey.user_id == user.id).order_by(SshKey.created_at.desc()).all()
    servers = db.session.query(Server).filter_by(owner_id=user.id).all()
    return render_template(
        "account.html",
        tokens=tokens,
        sessions=sessions,
        ssh_keys=keys,
        usage=provisioning.quota_snapshot(user),
        totals=metrics.summary(servers, include_live=False),
        scopes=[scope.value for scope in TokenScope],
        scope_descriptions={str(key): value for key, value in SCOPE_DESCRIPTIONS.items()},
        password_policy=current_app.config.get("PASSWORD_MIN_LENGTH", 10),
        active_session_id=current_session_id(),
    )


# --------------------------------------------------------------------------- #
# Profile
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/profile", methods=["POST"])
@login_required
def update_profile():
    user = current_user()
    data = body()
    if "full_name" in data:
        user.full_name = str(data["full_name"])[:128]
    if "company" in data:
        user.company = str(data["company"])[:128]
    if "timezone" in data:
        user.timezone = str(data["timezone"])[:64]
    if "theme" in data and str(data["theme"]) in ("midnight", "obsidian", "aurora", "daylight"):
        user.theme = str(data["theme"])
    if "email" in data:
        email = str(data["email"]).strip().lower()
        from ..security import EMAIL_RE

        if not EMAIL_RE.match(email):
            return fail("Enter a valid e-mail address", 400)
        clash = db.session.query(User).filter(User.email == email, User.id != user.id).first()
        if clash:
            return fail("That e-mail is already in use", 409)
        user.email = email
    if "notify_prefs" in data and isinstance(data["notify_prefs"], dict):
        prefs = dict(user.notify_prefs or {})
        prefs.update({str(k): v for k, v in data["notify_prefs"].items()})
        user.notify_prefs = prefs
    audit.record("account.profile", target_type="user", target_id=user.id, target_label=user.username,
                 meta={"fields": sorted(data.keys())})
    db.session.commit()
    return ok(user=user.public(), message="Profile updated")


@bp.route("/api/v1/password", methods=["POST"])
@login_required
def change_password():
    user = current_user()
    data = body()
    current = data.get("current_password") or ""
    password = data.get("password") or ""
    if not verify_password(user.password_hash, current):
        return fail("Current password is incorrect", 400)
    problems = password_problems(password, user.username, user.email)
    if problems:
        return fail("; ".join(problems), 400, problems=problems)
    user.password_hash = hash_password(password)
    user.must_change_password = False
    # Invalidate every other session — a password change must log others out.
    for record in db.session.query(Session).filter(Session.user_id == user.id).all():
        if not record.revoked_at:
            record.revoked_at = utcnow()
            record.revoked_by = "password change"
    audit.record("account.password", target_type="user", target_id=user.id, target_label=user.username)
    db.session.commit()
    end_session("password change")
    return ok(redirect="/login", message="Password changed — sign in again")


@bp.route("/api/v1/theme", methods=["POST"])
@login_required
def set_theme():
    user = current_user()
    data = body()
    theme = str(data.get("theme") or "midnight")
    if theme not in ("midnight", "obsidian", "aurora", "daylight"):
        return fail("Unknown theme", 400)
    user.theme = theme
    db.session.commit()
    return ok(theme=theme)


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/sessions")
@login_required
def list_sessions():
    user = current_user()
    rows = (
        db.session.query(Session)
        .filter(Session.user_id == user.id)
        .order_by(Session.last_seen_at.desc())
        .limit(50)
        .all()
    )
    return ok(sessions=[row.public() for row in rows])


@bp.route("/api/v1/sessions/<int:session_id>/revoke", methods=["POST"])
@login_required
def revoke_session(session_id: int):
    user = current_user()
    record = db.session.get(Session, session_id)
    if record is None or record.user_id != user.id:
        return fail("Session not found", 404)
    record.revoked_at = utcnow()
    record.revoked_by = "user"
    audit.record("account.session_revoke", target_type="session", target_id=record.id,
                 target_label=record.device, meta={"ip": record.ip})
    db.session.commit()
    return ok(message="Session revoked")


@bp.route("/api/v1/sessions/revoke-all", methods=["POST"])
@login_required
def revoke_all():
    user = current_user()
    data = body()
    keep_current = bool(data.get("keep_current", True))
    current_raw = None
    from flask import session as flask_session

    if keep_current:
        current_raw = flask_session.get("skvm_session")
    current_hash = sha256(current_raw) if current_raw else None
    count = 0
    for record in db.session.query(Session).filter(Session.user_id == user.id, Session.revoked_at.is_(None)).all():
        if current_hash and record.token_hash == current_hash:
            continue
        record.revoked_at = utcnow()
        record.revoked_by = "revoke-all"
        count += 1
    audit.record("account.session_revoke_all", target_type="user", target_id=user.id,
                 target_label=user.username, meta={"count": count})
    db.session.commit()
    return ok(count=count, message=f"Revoked {count} session(s)")


# --------------------------------------------------------------------------- #
# API tokens
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/tokens", methods=["GET", "POST"])
@login_required
def tokens():
    user = current_user()
    if request.method == "GET":
        rows = db.session.query(ApiToken).filter(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc()).all()
        return ok(tokens=[row.public() for row in rows])

    if not settings.boolean("api_enabled"):
        return fail("The API is disabled by the administrator", 403)
    data = body()
    scopes = [str(scope) for scope in (data.get("scopes") or ["read"]) if str(scope) in [s.value for s in TokenScope]]
    if not scopes:
        scopes = ["read"]
    if "admin" in scopes and not user.can(Role.ADMIN):
        return fail("Only administrators may mint tokens with the 'admin' scope", 403)
    expires_in = data.get("expires_in_days")
    plain, prefix, digest = generate_api_token()
    token = ApiToken(
        user_id=user.id,
        name=str(data.get("name") or "API token")[:64],
        prefix=prefix,
        token_hash=digest,
        scopes=scopes,
        ip_allowlist=[str(ip).strip() for ip in (data.get("ip_allowlist") or []) if str(ip).strip()],
        expires_at=utcnow() + timedelta(days=int(expires_in)) if expires_in else None,
    )
    db.session.add(token)
    audit.record("account.token_create", target_type="token", target_label=token.name,
                 meta={"scopes": scopes, "expires_in_days": expires_in})
    db.session.commit()
    return ok(token=token.public(), secret=plain, message="Copy the token now — it is shown only once")


@bp.route("/api/v1/tokens/<int:token_id>", methods=["DELETE", "PATCH"])
@login_required
def token_detail(token_id: int):
    user = current_user()
    token = db.session.get(ApiToken, token_id)
    if token is None or token.user_id != user.id:
        return fail("Token not found", 404)
    if request.method == "DELETE":
        token.revoked_at = utcnow()
        audit.record("account.token_revoke", target_type="token", target_id=token.id, target_label=token.name)
        db.session.commit()
        return ok(message="Token revoked")
    data = body()
    if "name" in data:
        token.name = str(data["name"])[:64]
    if "scopes" in data:
        scopes = [str(s) for s in data["scopes"] if str(s) in [x.value for x in TokenScope]]
        if "admin" in scopes and not user.can(Role.ADMIN):
            return fail("Only administrators may grant the 'admin' scope", 403)
        token.scopes = scopes or ["read"]
    if "ip_allowlist" in data:
        token.ip_allowlist = [str(ip).strip() for ip in (data["ip_allowlist"] or []) if str(ip).strip()]
    audit.record("account.token_update", target_type="token", target_id=token.id, target_label=token.name)
    db.session.commit()
    return ok(token=token.public(), message="Token updated")


# --------------------------------------------------------------------------- #
# SSH keys
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/ssh-keys", methods=["GET", "POST"])
@login_required
def ssh_keys():
    user = current_user()
    if request.method == "GET":
        rows = db.session.query(SshKey).filter(SshKey.user_id == user.id).all()
        return ok(keys=[row.public() for row in rows])
    data = body()
    public_key = str(data.get("public_key") or "").strip()
    parts = public_key.split()
    if len(parts) < 2 or not parts[0].startswith(("ssh-", "ecdsa-", "sk-")):
        return fail("That does not look like an OpenSSH public key", 400)
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4))).digest()
    ).decode().rstrip("=")
    if db.session.query(SshKey).filter(SshKey.user_id == user.id, SshKey.fingerprint == fingerprint).first():
        return fail("That key is already registered", 409)
    key = SshKey(
        user_id=user.id,
        name=str(data.get("name") or (parts[-1] if len(parts) > 2 else "key"))[:64],
        public_key=public_key[:4096],
        fingerprint=fingerprint,
        key_type=parts[0],
    )
    db.session.add(key)
    audit.record("account.ssh_key_add", target_type="key", target_label=key.name,
                 meta={"fingerprint": fingerprint})
    db.session.commit()
    return ok(key=key.public(), message="SSH key added")


@bp.route("/api/v1/ssh-keys/<int:key_id>", methods=["DELETE"])
@login_required
def delete_ssh_key(key_id: int):
    user = current_user()
    key = db.session.get(SshKey, key_id)
    if key is None or key.user_id != user.id:
        return fail("Key not found", 404)
    audit.record("account.ssh_key_delete", target_type="key", target_label=key.name)
    db.session.delete(key)
    db.session.commit()
    return ok(message="SSH key removed")


@bp.route("/api/v1/ssh-keys/generate", methods=["POST"])
@login_required
def generate_keypair():
    """Generate an ed25519 keypair server-side (private key shown once)."""
    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ed25519

        private = ed25519.Ed25519PrivateKey.generate()
        private_bytes = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.OpenSSH,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        public_bytes = private.public_key().public_bytes(
            encoding=serialization.Encoding.OpenSSH,
            format=serialization.PublicFormat.OpenSSH,
        ).decode()
    except Exception as exc:  # pragma: no cover
        return fail(f"Key generation unavailable: {exc}", 500)

    user = current_user()
    parts = public_bytes.split()
    fingerprint = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4))).digest()
    ).decode().rstrip("=")
    key = SshKey(
        user_id=user.id,
        name=(body().get("name") or "SKVM generated key")[:64],
        public_key=public_bytes,
        fingerprint=fingerprint,
        key_type=parts[0],
    )
    if not db.session.query(SshKey).filter(SshKey.user_id == user.id, SshKey.fingerprint == fingerprint).first():
        db.session.add(key)
    audit.record("account.ssh_key_generate", target_type="key", target_label=key.name)
    db.session.commit()
    return ok(
        public_key=public_bytes,
        private_key=private_bytes,
        fingerprint=fingerprint,
        message="Save the private key now — it is not stored on the panel",
    )


# --------------------------------------------------------------------------- #
# Two-factor authentication
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/2fa/setup", methods=["POST"])
@login_required
def twofa_setup():
    user = current_user()
    if user.totp_enabled:
        return fail("Two-factor authentication is already enabled", 409)
    secret = new_totp_secret()
    user.totp_secret = secret
    db.session.commit()
    uri = totp_uri(secret, user.username)
    return ok(
        secret=secret,
        uri=uri,
        qr=uri,
        message="Scan the QR code, then confirm with a 6-digit code",
    )


@bp.route("/api/v1/2fa/enable", methods=["POST"])
@login_required
def twofa_enable():
    user = current_user()
    data = body()
    code = str(data.get("code") or "")
    if not user.totp_secret:
        return fail("Start the 2FA setup first", 400)
    if not verify_totp(user.totp_secret, code):
        return fail("That code did not verify — check your device clock", 400)
    plain, hashed = generate_recovery_codes()
    user.totp_enabled = True
    user.recovery_codes = hashed
    audit.record("account.2fa_enable", target_type="user", target_id=user.id, target_label=user.username)
    db.session.commit()
    return ok(recovery_codes=plain, message="Two-factor authentication enabled")


@bp.route("/api/v1/2fa/disable", methods=["POST"])
@login_required
def twofa_disable():
    user = current_user()
    data = body()
    if not verify_password(user.password_hash, data.get("password") or ""):
        return fail("Password is incorrect", 400)
    if settings.boolean("require_2fa_for_staff") and user.is_staff:
        return fail("Your role requires two-factor authentication", 403)
    user.totp_enabled = False
    user.totp_secret = None
    user.recovery_codes = []
    audit.record("account.2fa_disable", target_type="user", target_id=user.id, target_label=user.username,
                 result="warn")
    db.session.commit()
    return ok(message="Two-factor authentication disabled")


@bp.route("/api/v1/2fa/codes", methods=["POST"])
@login_required
def twofa_codes():
    user = current_user()
    data = body()
    if not verify_password(user.password_hash, data.get("password") or ""):
        return fail("Password is incorrect", 400)
    if not user.totp_enabled:
        return fail("Enable two-factor authentication first", 400)
    plain, hashed = generate_recovery_codes()
    user.recovery_codes = hashed
    audit.record("account.2fa_codes", target_type="user", target_id=user.id, target_label=user.username,
                 result="warn")
    db.session.commit()
    return ok(recovery_codes=plain, message="New recovery codes generated")


@bp.route("/api/v1/strength", methods=["POST"])
def strength():
    return ok(strength=password_strength(body().get("password") or ""))


@bp.route("/api/v1/export", methods=["GET"])
@login_required
def export_data():
    """GDPR-style self-service export of everything SKVM stores about you."""
    user = current_user()
    servers = db.session.query(Server).filter_by(owner_id=user.id).all()
    payload = {
        "exported_at": iso(utcnow()),
        "panel": current_app.config.get("PANEL_NAME"),
        "account": user.public(),
        "servers": [server.public(detail=True) for server in servers],
        "api_tokens": [token.public() for token in user.tokens],
        "sessions": [record.public() for record in user.sessions],
        "ssh_keys": [key.public() for key in user.ssh_keys],
        "alert_rules": [rule.public() for rule in user.alert_rules],
        "quotas": provisioning.quota_snapshot(user),
    }
    audit.record("account.export", target_type="user", target_id=user.id, target_label=user.username)
    db.session.commit()
    response = jsonify(payload)
    response.headers["Content-Disposition"] = f'attachment; filename="skvm-account-{user.username}.json"'
    return response
