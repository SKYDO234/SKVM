"""Authentication, authorisation, hashing and secret handling.

Design notes
------------
* Passwords: ``werkzeug.security`` scrypt hashes (PBKDF2 fallback), never stored
  or logged in clear text.
* Browser auth: opaque session tokens (32 bytes) stored **hashed** in the DB and
  referenced by an HttpOnly cookie — revocable, listable, expiring.
* API auth: ``sk_live_<prefix>.<secret>`` bearer tokens, hashed at rest, with
  scope checks and optional IP allow-lists.
* Secrets at rest (VPS root passwords, node API keys): Fernet (AES-128-CBC +
  HMAC) using the panel instance key.
"""

from __future__ import annotations

import base64
import functools
import hashlib
import hmac
import ipaddress
import logging
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable

import pyotp
from cryptography.fernet import Fernet, InvalidToken
from flask import current_app, flash, g, jsonify, redirect, request, session as flask_session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import iso, utcnow
from .models import ROLE_RANK, SCOPE_DESCRIPTIONS, ApiToken, Role, Session, TokenScope, User  # noqa: F401

log = logging.getLogger("skvm.security")

SESSION_COOKIE = "skvm_session"
CSRF_FIELD = "_csrf"
_CSRF_KEY = "csrf_token"
_FERNET_CACHE: dict[str, Fernet] = {}

USERNAME_RE = re.compile(r"^[a-zA-Z0-9](?:[a-zA-Z0-9_.\-]{2,62})[a-zA-Z0-9]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
HOSTNAME_RE = re.compile(r"^(?!-)[a-z0-9-]{1,63}(?<!-)(\.(?!-)[a-z0-9-]{1,63}(?<!-))*$")

PASSWORD_STRENGTHS = (
    ("Very weak", "#ff4d6d", 0, 25),
    ("Weak", "#ff7b54", 1, 45),
    ("Fair", "#ffd166", 2, 60),
    ("Strong", "#4ade80", 3, 80),
    ("Elite", "#22d3ee", 4, 100),
)
COMMON_PASSWORDS = {
    "password", "password1", "password123", "12345678", "123456789", "qwerty123",
    "letmein", "admin", "admin123", "welcome", "welcome1", "iloveyou", "changeme",
    "vpspanel", "root1234", "passw0rd", "administrator", "qwertyuiop",
}


# --------------------------------------------------------------------------- #
# Password policy
# --------------------------------------------------------------------------- #
def password_problems(password: str, username: str = "", email: str = "") -> list[str]:
    """Return a list of policy violations (empty list == compliant)."""
    problems: list[str] = []
    min_len = current_app.config.get("PASSWORD_MIN_LENGTH", 10) if current_app else 10
    if len(password) < min_len:
        problems.append(f"Use at least {min_len} characters")
    classes = sum(
        bool(re.search(pattern, password))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    if classes < 3:
        problems.append("Mix upper case, lower case, digits and symbols (3 of 4)")
    if password.lower() in COMMON_PASSWORDS:
        problems.append("That password is on the common-password list")
    if username and username.lower() in password.lower():
        problems.append("Do not include your username")
    if email and email.split("@")[0].lower() in password.lower():
        problems.append("Do not include your email local-part")
    return problems


def password_strength(password: str) -> dict[str, Any]:
    """Score a password 0-100 for the live strength meter."""
    if not password:
        return {"score": 0, "label": "Empty", "color": "#64748b"}
    score = min(len(password) * 4, 40)
    classes = sum(
        bool(re.search(pattern, password))
        for pattern in (r"[a-z]", r"[A-Z]", r"\d", r"[^A-Za-z0-9]")
    )
    score += classes * 12
    if len(set(password)) > len(password) * 0.6:
        score += 8
    if re.search(r"(.)\1{2,}", password):
        score -= 10
    if password.lower() in COMMON_PASSWORDS:
        score = min(score, 12)
    score = max(0, min(100, score))
    label, color = "Very weak", "#ff4d6d"
    for name, hex_color, _idx, floor in PASSWORD_STRENGTHS:
        if score >= floor:
            label, color = name, hex_color
    return {"score": score, "label": label, "color": color}


def hash_password(password: str, rounds: int | None = None) -> str:
    method = "scrypt"
    return generate_password_hash(password, method=method)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return check_password_hash(password_hash, password)
    except Exception:  # pragma: no cover - corrupt hash
        return False


# --------------------------------------------------------------------------- #
# Symmetric secret encryption
# --------------------------------------------------------------------------- #
def _fernet() -> Fernet:
    key = current_app.config.get("ENCRYPTION_KEY_RESOLVED") or ""
    cache = _FERNET_CACHE.get(key)
    if cache is None:
        if not key:
            raise RuntimeError("Encryption key is not configured")
        cache = Fernet(key.encode() if isinstance(key, str) else key)
        _FERNET_CACHE[key] = cache
    return cache


def encrypt_secret(plaintext: str | None) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt_secret(ciphertext: str | None) -> str:
    if not ciphertext:
        return ""
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except (InvalidToken, RuntimeError, ValueError):
        log.warning("could not decrypt a stored secret (wrong ENCRYPTION_KEY?)")
        return ""


# --------------------------------------------------------------------------- #
# Tokens
# --------------------------------------------------------------------------- #
def random_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def generate_api_token() -> tuple[str, str, str]:
    """Return ``(full_token, prefix, hash)`` for a brand new API token."""
    prefix = "sk_" + secrets.token_hex(4)
    secret = secrets.token_urlsafe(24)
    token = f"{prefix}.{secret}"
    return token, prefix, sha256(token)


def password_reset_token() -> tuple[str, str]:
    raw = random_token(32)
    return raw, sha256(raw)


# --------------------------------------------------------------------------- #
# CSRF
# --------------------------------------------------------------------------- #
def csrf_token() -> str:
    token = flask_session.get(_CSRF_KEY)
    if not token:
        token = random_token(24)
        flask_session[_CSRF_KEY] = token
    return token


def csrf_enabled() -> bool:
    if not current_app.config.get("CSRF_ENABLED", True):
        return False
    return True


def validate_csrf() -> bool:
    if not csrf_enabled():
        return True
    if request.method in ("GET", "HEAD", "OPTIONS", "TRACE"):
        return True
    supplied = (
        request.headers.get("X-CSRF-Token")
        or request.form.get(CSRF_FIELD)
        or (request.get_json(silent=True) or {}).get(CSRF_FIELD)
        or request.args.get(CSRF_FIELD)
    )
    expected = flask_session.get(_CSRF_KEY)
    if not supplied or not expected:
        return False
    return constant_time_equals(str(supplied), str(expected))


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
def _device_label(user_agent: str) -> str:
    ua = (user_agent or "").lower()
    for needle, label in (
        ("iphone", "iPhone"), ("ipad", "iPad"), ("android", "Android"),
        ("windows", "Windows PC"), ("macintosh", "macOS"), ("mac os", "macOS"),
        ("linux", "Linux desktop"), ("curl", "curl"), ("python", "Python client"),
        ("postman", "Postman"), ("wget", "wget"),
    ):
        if needle in ua:
            browser = ""
            for b_needle, b_label in (
                ("edg/", "Edge"), ("chrome", "Chrome"), ("firefox", "Firefox"), ("safari", "Safari"),
            ):
                if b_needle in ua:
                    browser = f" · {b_label}"
                    break
            return label + browser
    if not ua:
        return "Unknown client"
    return (user_agent or "Client")[:48]


def client_ip() -> str:
    """Resolve the client IP honouring ``TRUSTED_PROXY_COUNT`` hops."""
    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = int(current_app.config.get("TRUSTED_PROXY_COUNT", 1) or 0)
    if forwarded and hops:
        chain = [part.strip() for part in forwarded.split(",") if part.strip()]
        if chain:
            index = max(0, len(chain) - hops)
            return chain[index]
    return request.remote_addr or "0.0.0.0"


def start_session(user: User, remember_days: int | None = None) -> Session:
    """Create a DB-backed login session and set the cookie."""
    lifetime = current_app.config.get("PERMANENT_SESSION_LIFETIME", 43200)
    if remember_days:
        lifetime = int(timedelta(days=remember_days).total_seconds())
    raw = random_token(32)
    record = Session(
        user_id=user.id,
        token_hash=sha256(raw),
        ip=client_ip(),
        user_agent=(request.user_agent.string or "")[:250],
        device=_device_label(request.user_agent.string or ""),
        expires_at=utcnow() + timedelta(seconds=lifetime),
    )
    from .db import db

    db.session.add(record)
    db.session.flush()

    flask_session.clear()
    flask_session[SESSION_COOKIE] = raw
    flask_session["uid"] = user.id
    flask_session["sid"] = record.id
    flask_session.permanent = True
    csrf_token()  # rotate
    return record


def end_session(reason: str = "logout", by: str = "") -> None:
    from .db import db

    raw = flask_session.get(SESSION_COOKIE)
    if raw:
        record = db.session.query(Session).filter_by(token_hash=sha256(raw)).one_or_none()
        if record:
            record.revoked_at = utcnow()
            record.revoked_by = by or reason
    flask_session.clear()


def current_user() -> User | None:
    """Resolve the session cookie to a live :class:`User` (cached on ``g``)."""
    if "_skvm_user" in g:
        return g._skvm_user
    from .db import db

    user: User | None = None
    raw = flask_session.get(SESSION_COOKIE)
    if raw:
        token_hash = sha256(raw)
        record = db.session.query(Session).filter_by(token_hash=token_hash).one_or_none()
        if record and record.is_active:
            candidate = db.session.get(User, record.user_id)
            if candidate and candidate.is_active:
                user = candidate
                record.last_seen_at = utcnow()
                g._skvm_session_record = record
    g._skvm_user = user
    return user


def current_session_id() -> int | None:
    return flask_session.get("sid")


# --------------------------------------------------------------------------- #
# 2FA
# --------------------------------------------------------------------------- #
def new_totp_secret() -> str:
    return pyotp.random_base32()


def totp_uri(secret: str, username: str) -> str:
    issuer = current_app.config.get("PANEL_NAME", "SKVM")
    return pyotp.totp.TOTP(secret).provisioning_uri(name=username, issuer_name=issuer)


def verify_totp(secret: str, code: str, valid_window: int = 1) -> bool:
    if not secret or not code:
        return False
    code = code.strip().replace(" ", "")
    return pyotp.TOTP(secret).verify(code, valid_window=valid_window)


def generate_recovery_codes(count: int = 8) -> tuple[list[str], list[str]]:
    plain = [f"{secrets.token_hex(2)}-{secrets.token_hex(2)}-{secrets.token_hex(2)}" for _ in range(count)]
    return plain, [sha256(code) for code in plain]


def consume_recovery_code(user: User, code: str) -> bool:
    digest = sha256(code.strip().lower())
    hashes = list(user.recovery_codes or [])
    if digest in hashes:
        hashes.remove(digest)
        user.recovery_codes = hashes
        return True
    return False


# --------------------------------------------------------------------------- #
# Decorators / guards
# --------------------------------------------------------------------------- #
ApiResult = tuple[Any, int]


def _wants_json() -> bool:
    if request.path.startswith("/api/") or request.path.startswith("/ws/"):
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    accept = request.accept_mimetypes
    return accept["application/json"] >= accept["text/html"]


def json_error(message: str, status: int = 400, **extra) -> ApiResult:
    payload = {"ok": False, "error": message, "status": status}
    payload.update(extra)
    return jsonify(payload), status


def login_required(view: Callable) -> Callable:
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        user = current_user()
        if user is None:
            if _wants_json():
                return json_error("Authentication required", 401)
            return redirect(url_for("auth.login", next=request.full_path if request.query_string else request.path))
        if user.must_change_password and request.endpoint not in ("auth.force_password", "auth.logout"):
            if _wants_json():
                return json_error("Password change required", 403, code="password_change_required")
            return redirect(url_for("auth.force_password"))
        return view(*args, **kwargs)

    return wrapper


def role_required(role: Role | str):
    """Require at least ``role`` (owner > admin > operator > user)."""
    minimum = Role(str(role))

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            user = current_user()
            assert user is not None
            if user.role_rank < ROLE_RANK[minimum]:
                from .services.audit import record

                record("auth.denied", target_type="route", target_label=request.path, result="denied")
                if _wants_json():
                    return json_error("Insufficient privileges", 403)
                flash("You do not have access to that area.", "error")
                return redirect(url_for("dashboard.index"))
            return view(*args, **kwargs)

        return wrapper

    return decorator


def admin_required(view: Callable) -> Callable:
    return role_required(Role.ADMIN)(view)


def staff_required(view: Callable) -> Callable:
    return role_required(Role.OPERATOR)(view)


# --------------------------------------------------------------------------- #
# API authentication
# --------------------------------------------------------------------------- #
def api_token_from_request() -> str | None:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return request.headers.get("X-API-Key") or request.args.get("token")


def api_user(required_scope: TokenScope | None = None) -> tuple[User | None, ApiToken | None, ApiResult | None]:
    """Authenticate an API request; returns ``(user, token, error_response)``."""
    from .db import db

    raw = api_token_from_request()
    if not raw:
        return None, None, json_error("Missing API token", 401, code="missing_token")
    digest = sha256(raw)
    token = db.session.query(ApiToken).filter_by(token_hash=digest).one_or_none()
    if token is None:
        prefix = raw.split(".")[0]
        return None, None, json_error("Invalid API token", 401, code="invalid_token", prefix=prefix)
    if token.revoked_at is not None:
        return None, None, json_error("Token revoked", 401, code="revoked")
    if token.expired:
        return None, None, json_error("Token expired", 401, code="expired")
    allow = token.ip_allowlist or []
    if allow and client_ip() not in allow:
        try:
            addr = ipaddress.ip_address(client_ip())
            if not any(addr in ipaddress.ip_network(cidr, strict=False) for cidr in allow):
                return None, None, json_error("Caller IP not allowed for this token", 403, code="ip_not_allowed")
        except ValueError:
            return None, None, json_error("Caller IP not allowed for this token", 403, code="ip_not_allowed")
    user = db.session.get(User, token.user_id)
    if user is None or not user.is_active:
        return None, None, json_error("Account disabled", 403, code="disabled")
    if required_scope and not token.has_scope(required_scope):
        return None, None, json_error(
            f"Token lacks the '{required_scope}' scope", 403, code="insufficient_scope",
            required=str(required_scope), granted=token.scopes or [],
        )
    token.last_used_at = utcnow()
    token.call_count = (token.call_count or 0) + 1
    g.api_token = token
    g.current_user = user
    return user, token, None


def api_required(scope: TokenScope | str = TokenScope.READ):
    """Decorator for JSON API endpoints: bearer token **or** browser session."""

    def decorator(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            if request.method not in ("GET", "HEAD", "OPTIONS") and csrf_enabled() and api_token_from_request() is None:
                if not validate_csrf():
                    return json_error("CSRF validation failed", 403, code="csrf")
            if api_token_from_request():
                user, _token, err = api_user(TokenScope(scope))
                if err is not None:
                    return err
                g.api_scope = scope
            else:
                user = current_user()
                if user is None:
                    return json_error("Authentication required", 401, code="unauthenticated")
                g.api_scope = "session"
            g.current_user = user
            return view(*args, **kwargs)

        return wrapper

    return decorator


def owner_or_staff(server, user: User | None = None) -> bool:
    user = user or current_user()
    if user is None or server is None:
        return False
    return server.owner_id == user.id or user.can(Role.OPERATOR)


# --------------------------------------------------------------------------- #
# Login throttling helpers
# --------------------------------------------------------------------------- #
def register_failed_login(user: User) -> None:
    from .db import db

    user.failed_logins = (user.failed_logins or 0) + 1
    limit = current_app.config.get("LOGIN_MAX_ATTEMPTS", 8)
    if user.failed_logins >= limit:
        user.locked_until = utcnow() + timedelta(seconds=current_app.config.get("LOGIN_LOCKOUT_SECONDS", 900))
        user.failed_logins = 0
    db.session.flush()


def reset_failed_logins(user: User) -> None:
    user.failed_logins = 0
    user.locked_until = None


def security_summary() -> dict[str, Any]:
    """Small security posture report shown on the admin dashboard."""
    from .db import db
    from .models import Server as ServerModel

    user = current_user()
    two_factor = bool(user and user.totp_enabled)
    weak = db.session.query(User).filter(User.totp_enabled.is_(False)).count()
    return {
        "must_change_password": bool(user and user.must_change_password),
        "two_factor": two_factor,
        "users_without_2fa": weak,
        "csrf": csrf_enabled(),
        "session_expires_at": iso(utcnow() + timedelta(seconds=current_app.config.get("PERMANENT_SESSION_LIFETIME", 0))),
        "servers": db.session.query(ServerModel).count(),
    }


def mask_secret(value: str, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep:
        return "•" * len(value)
    return "•" * (len(value) - keep) + value[-keep:]


def b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode()


# Re-exports used by templates / routes that import from this module.
__all__ = [
    "SCOPE_DESCRIPTIONS", "api_required", "api_user", "client_ip", "constant_time_equals", "csrf_token",
    "current_user", "decrypt_secret", "encrypt_secret", "end_session", "hash_password",
    "login_required", "password_problems", "password_strength", "random_token", "role_required",
    "sha256", "start_session", "validate_csrf", "verify_password", "verify_totp",
    "admin_required", "staff_required", "owner_or_staff", "generate_api_token", "mask_secret",
]
