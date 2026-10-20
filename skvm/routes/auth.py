"""Authentication: sign-in (with TOTP), registration, recovery, sessions."""

from __future__ import annotations

import logging
from datetime import timedelta

from flask import (
    Blueprint,
    current_app,
    flash,
    g,
    redirect,
    render_template,
    request,
    session as flask_session,
    url_for,
)

from ..db import db, utcnow
from ..models import ResetToken, Role, Session, User
from ..security import (
    client_ip,
    consume_recovery_code,
    current_user,
    end_session,
    hash_password,
    login_required,
    password_problems,
    password_strength,
    random_token,
    reset_failed_logins,
    register_failed_login,
    sha256,
    start_session,
    verify_password,
    verify_totp,
)
from ..services import audit
from ..services.mailer import send_password_reset, send_welcome
from ..services.settings import settings
from .helpers import back, body, fail, ok, wants_json

log = logging.getLogger("skvm.auth")

bp = Blueprint("auth", __name__)


def _limiter():
    return current_app.extensions.get("skvm.limiter")


# --------------------------------------------------------------------------- #
# Sign in / out
# --------------------------------------------------------------------------- #
@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user() is not None:
        return redirect(url_for("dashboard.index"))

    if request.method == "GET":
        return render_template("login.html", next=request.args.get("next", ""))

    data = body()
    identifier = (data.get("username") or "").strip()
    password = data.get("password") or ""
    remember = bool(data.get("remember"))
    code = (data.get("totp") or "").strip()

    limiter = _limiter()
    if limiter is not None:
        limiter.limit("12 per minute")(lambda: None)()
    ip = client_ip()

    user = (
        db.session.query(User)
        .filter((User.username == identifier) | (User.email == identifier.lower()))
        .one_or_none()
    )
    if user is None or not verify_password(user.password_hash, password):
        if user is not None:
            register_failed_login(user)
            db.session.commit()
        audit.record("auth.login", target_type="user", target_label=identifier, result="denied",
                     meta={"ip": ip}, actor=identifier or "anonymous")
        db.session.commit()
        if wants_json():
            return fail("Invalid credentials", 401)
        flash("Invalid credentials. Check your username and password.", "error")
        return render_template("login.html", next=data.get("next", ""), username=identifier), 401

    if not user.is_active:
        audit.record("auth.login", target_type="user", target_label=user.username, result="disabled", actor=user.username)
        db.session.commit()
        if wants_json():
            return fail("This account is disabled", 403)
        flash("This account has been disabled. Contact your administrator.", "error")
        return render_template("login.html"), 403

    if user.is_locked():
        if wants_json():
            return fail("Account temporarily locked after too many attempts", 429)
        flash("Too many failed attempts — the account is temporarily locked.", "error")
        return render_template("login.html"), 429

    if user.totp_enabled:
        ok_code = verify_totp(user.totp_secret or "", code) or consume_recovery_code(user, code)
        if not ok_code:
            g.pending_user_id = user.id
            register_failed_login(user)
            db.session.commit()
            if wants_json():
                return fail("Two-factor code required or invalid", 401, totp_required=True)
            return render_template("two_factor.html", username=user.username, password=password), 401

    reset_failed_logins(user)
    user.last_login_at = utcnow()
    user.last_login_ip = ip
    start_session(user, remember_days=30 if remember else None)
    audit.record("auth.login", target_type="user", target_id=user.id, target_label=user.username,
                 meta={"ip": ip, "device": request.user_agent.string[:120]}, actor=user.username)
    db.session.commit()

    if wants_json():
        return ok(redirect=url_for("dashboard.index"), user=user.public())
    target = data.get("next") or request.args.get("next")
    if target and target.startswith("/"):
        return redirect(target)
    return redirect(url_for("dashboard.index"))


@bp.route("/logout", methods=["GET", "POST"])
@login_required
def logout():
    user = current_user()
    audit.record("auth.logout", target_type="user", target_id=user.id if user else None,
                 target_label=user.username if user else "")
    end_session("logout", by=user.username if user else "")
    db.session.commit()
    if wants_json():
        return ok(redirect=url_for("auth.login"))
    flash("You have been signed out.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user() is not None:
        return redirect(url_for("dashboard.index"))
    if not settings.registration_open():
        if wants_json():
            return fail("Registration is closed", 403)
        flash("Registration is currently closed. Contact your provider.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "GET":
        return render_template("register.html")

    data = body()
    username = (data.get("username") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    confirm = data.get("confirm") or ""
    full_name = (data.get("full_name") or "").strip()

    problems: list[str] = []
    from ..security import EMAIL_RE, USERNAME_RE

    if not USERNAME_RE.match(username):
        problems.append("Username must be 4-64 characters (letters, digits, . _ -)")
    if not EMAIL_RE.match(email):
        problems.append("Enter a valid e-mail address")
    if password != confirm:
        problems.append("Passwords do not match")
    problems.extend(password_problems(password, username, email))
    if db.session.query(User).filter(User.username == username).first():
        problems.append("That username is taken")
    if db.session.query(User).filter(User.email == email).first():
        problems.append("That e-mail is already registered")

    if problems:
        if wants_json():
            return fail("; ".join(problems), 400, problems=problems)
        return render_template("register.html", problems=problems, username=username, email=email,
                               full_name=full_name), 400

    role = Role.OWNER.value if db.session.query(User).count() == 0 else Role.USER.value
    quotas = settings.quotas()
    user = User(
        username=username,
        email=email,
        full_name=full_name or username,
        password_hash=hash_password(password),
        role=role,
        is_active=not bool(settings.get("require_approval", False)),
        is_verified=not current_app.config.get("REQUIRE_EMAIL_VERIFICATION", False),
        quotas=quotas,
        credits=float(settings.get("signup_credits") or 0),
    )
    db.session.add(user)
    db.session.flush()
    audit.record("auth.register", target_type="user", target_id=user.id, target_label=user.username,
                 meta={"email": email}, actor=user.username)
    if settings.boolean("alert_email_enabled") and user.is_active:
        try:
            send_welcome(user)
        except Exception as exc:  # pragma: no cover
            log.warning("welcome mail failed: %s", exc)
    db.session.commit()

    if not user.is_active:
        if wants_json():
            return ok(message="Account created — pending administrator approval")
        flash("Account created. An administrator must approve it before you can sign in.", "info")
        return redirect(url_for("auth.login"))

    start_session(user)
    db.session.commit()
    if wants_json():
        return ok(redirect=url_for("dashboard.index"))
    flash(f"Welcome to {current_app.config.get('PANEL_NAME', 'SKVM')}, {user.username}!", "success")
    return redirect(url_for("dashboard.index"))


# --------------------------------------------------------------------------- #
# Password recovery
# --------------------------------------------------------------------------- #
@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "GET":
        return render_template("forgot.html")
    data = body()
    identifier = (data.get("identifier") or "").strip().lower()
    user = (
        db.session.query(User)
        .filter((User.username == identifier) | (User.email == identifier))
        .one_or_none()
    )
    if user and user.is_active:
        raw = random_token(32)
        db.session.add(
            ResetToken(
                user_id=user.id,
                token_hash=sha256(raw),
                purpose="password",
                expires_at=utcnow() + timedelta(minutes=60),
            )
        )
        audit.record("auth.reset_request", target_type="user", target_id=user.id, target_label=user.username)
        try:
            send_password_reset(user, raw)
        except Exception as exc:  # pragma: no cover
            log.warning("reset mail failed: %s", exc)
        db.session.commit()
    # Never reveal whether the account exists.
    message = "If that account exists, a reset link is on its way."
    if wants_json():
        return ok(message=message)
    flash(message, "info")
    return redirect(url_for("auth.login"))


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token: str):
    record = db.session.query(ResetToken).filter_by(token_hash=sha256(token)).one_or_none()
    valid = record is not None and record.used_at is None and record.expires_at > utcnow().replace(tzinfo=None) if record else False
    if record is not None and record.expires_at.tzinfo is None:
        valid = record.used_at is None and record.expires_at > utcnow().replace(tzinfo=None)
    if not valid:
        flash("That reset link is invalid or has expired.", "error")
        return redirect(url_for("auth.login"))

    if request.method == "GET":
        return render_template("reset.html", token=token)

    data = body()
    password = data.get("password") or ""
    confirm = data.get("confirm") or ""
    user = db.session.get(User, record.user_id)
    problems = password_problems(password, user.username if user else "", user.email if user else "")
    if password != confirm:
        problems.append("Passwords do not match")
    if problems:
        return render_template("reset.html", token=token, problems=problems), 400

    if user is not None:
        user.password_hash = hash_password(password)
        user.must_change_password = False
        user.failed_logins = 0
        user.locked_until = None
        record.used_at = utcnow()
        for session_record in db.session.query(Session).filter(Session.user_id == user.id).all():
            session_record.revoked_at = utcnow()
            session_record.revoked_by = "password reset"
        audit.record("auth.reset", target_type="user", target_id=user.id, target_label=user.username)
        db.session.commit()
    flash("Password updated — you can sign in now.", "success")
    return redirect(url_for("auth.login"))


@bp.route("/2fa", methods=["GET", "POST"])
def two_factor():
    """Dedicated 2FA challenge page (used by the JS sign-in flow)."""
    if current_user() is not None:
        return redirect(url_for("dashboard.index"))
    if request.method == "GET":
        return render_template("two_factor.html", username=flask_session.get("pending_username", ""))

    data = body()
    username = (data.get("username") or flask_session.get("pending_username") or "").strip()
    password = data.get("password") or ""
    code = (data.get("totp") or "").strip()
    user = db.session.query(User).filter(User.username == username).one_or_none()
    if user is None or not verify_password(user.password_hash, password):
        return fail("Invalid credentials", 401)
    if not (verify_totp(user.totp_secret or "", code) or consume_recovery_code(user, code)):
        register_failed_login(user)
        db.session.commit()
        return fail("Invalid authentication code", 401)
    reset_failed_logins(user)
    start_session(user, remember_days=30 if data.get("remember") else None)
    user.last_login_at = utcnow()
    user.last_login_ip = client_ip()
    audit.record("auth.login", target_type="user", target_id=user.id, target_label=user.username,
                 meta={"method": "2fa"})
    db.session.commit()
    return ok(redirect=url_for("dashboard.index"))


@bp.route("/force-password", methods=["GET", "POST"])
@login_required
def force_password():
    user = current_user()
    if request.method == "GET":
        return render_template("force_password.html")

    data = body()
    current = data.get("current_password") or ""
    password = data.get("password") or ""
    confirm = data.get("confirm") or ""
    problems = password_problems(password, user.username, user.email)
    if not verify_password(user.password_hash, current):
        problems.append("Current password is incorrect")
    if password != confirm:
        problems.append("Passwords do not match")
    if problems:
        if wants_json():
            return fail("; ".join(problems), 400, problems=problems)
        return render_template("force_password.html", problems=problems), 400

    user.password_hash = hash_password(password)
    user.must_change_password = False
    audit.record("account.password", target_type="user", target_id=user.id, target_label=user.username,
                 meta={"forced": True})
    db.session.commit()
    if wants_json():
        return ok(redirect=url_for("dashboard.index"))
    flash("Password updated.", "success")
    return redirect(url_for("dashboard.index"))


# --------------------------------------------------------------------------- #
# Utilities used by the front-end
# --------------------------------------------------------------------------- #
@bp.route("/api/v1/auth/strength", methods=["POST"])
def strength():
    data = body()
    return ok(strength=password_strength(data.get("password") or ""))


@bp.route("/api/v1/auth/session")
@login_required
def session_info():
    user = current_user()
    record = getattr(g, "_skvm_session_record", None)
    return ok(
        user=user.public(),
        session=record.public() if record else None,
        csrf=flask_session.get("csrf_token"),
    )
