"""Flask application factory, request hooks, error handling and CLI helpers."""

from __future__ import annotations

import logging
import os
import secrets
import sys
from datetime import timedelta
from typing import Any

from flask import (Flask, Response, current_app, g, has_request_context, jsonify, redirect,
                   render_template, request, url_for)
from werkzeug.exceptions import HTTPException

from . import __version__
from .config import (
    CONFIG_MAP,
    BaseConfig,
    INSTANCE_DIR,
    ensure_instances,
    resolve_database_url,
    resolve_encryption_key,
    resolve_secret_key,
)
from .db import db, iso, utcnow
from .models import Role
from .security import csrf_token, current_user, validate_csrf
from .services.settings import DEFAULTS as SETTING_DEFAULTS, settings

log = logging.getLogger("skvm")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _load_dotenv_if_present() -> None:
    for candidate in (INSTANCE_DIR / ".env", os.path.join(os.getcwd(), ".env")):
        path = candidate if hasattr(candidate, "is_file") else None
        if path and path.is_file():
            from .config import _load_dotenv

            _load_dotenv(path)
            return


def _reaper_wanted(app: Flask) -> bool:
    """Should this process run the sampling/alert worker in-process?

    Evaluated when the app is built rather than at config-import time, so a
    caller (``manage.py`` one-shot commands) can still switch it off.
    """
    if app.config.get("TESTING") or not app.config.get("REAPER_ENABLED", True):
        return False
    raw = os.environ.get("SKVM_REAPER")
    if raw is not None and raw.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return True


def _resolve_config(config_object: str | object | None) -> type[BaseConfig]:
    if isinstance(config_object, type):
        return config_object
    env_name = (
        config_object
        if isinstance(config_object, str)
        else os.environ.get("SKVM_ENV") or os.environ.get("FLASK_ENV") or "production"
    )
    if env_name == "development" and not os.environ.get("SKVM_ENV"):
        env_name = "development" if os.environ.get("SKVM_DEBUG") else "production"
    return CONFIG_MAP.get(str(env_name).lower(), CONFIG_MAP["default"])


def create_app(config_object: str | object | None = None, **overrides: Any) -> Flask:
    """Build the SKVM Flask application."""
    _load_dotenv_if_present()
    config_class = _resolve_config(config_object)
    app = Flask(__name__, template_folder="../templates", static_folder="../static")
    app.config.from_object(config_class)

    # Instance-dependent values
    instance_dir = ensure_instances(INSTANCE_DIR)
    app.config["INSTANCE_DIR"] = instance_dir
    app.config["SQLALCHEMY_DATABASE_URL"] = resolve_database_url(instance_dir)
    app.config["SECRET_KEY"] = resolve_secret_key(instance_dir)
    app.config["ENCRYPTION_KEY_RESOLVED"] = resolve_encryption_key(instance_dir)
    app.config["SKVM_VERSION"] = __version__
    app.config.update(overrides)

    _configure_logging(app)
    db.init_app(app)
    _init_limiter(app)
    _register_blueprints(app)
    _register_hooks(app)
    _register_error_handlers(app)
    _register_cli(app)
    _register_template_helpers(app)

    with app.app_context():
        db.create_all()
        from .bootstrap import ensure_defaults

        ensure_defaults(app)

    if _reaper_wanted(app):
        from .services.reaper import start_worker

        start_worker(app)

    log.info(
        "SKVM %s ready · env=%s · db=%s · backend=%s",
        __version__,
        app.config.get("ENV_NAME", "production"),
        app.config["SQLALCHEMY_DATABASE_URL"].split("@")[-1],
        app.config.get("BACKEND"),
    )
    return app


def _configure_logging(app: Flask) -> None:
    level = logging.DEBUG if app.config.get("DEBUG") else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(levelname)-7s %(name)-18s %(message)s", "%Y-%m-%d %H:%M:%S"))
    root = logging.getLogger("skvm")
    root.handlers = [handler]
    root.setLevel(level)
    root.propagate = False
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def _init_limiter(app: Flask) -> None:
    try:
        from flask_limiter import Limiter
        from flask_limiter.util import get_remote_address

        limiter = Limiter(
            key_func=get_remote_address,
            app=app,
            default_limits=[app.config.get("RATELIMIT_DEFAULT", "600 per minute")],
            storage_uri=app.config.get("RATELIMIT_STORAGE_URI", "memory://"),
            headers_enabled=True,
            enabled=not app.config.get("TESTING") and app.config.get("RATELIMIT_ENABLED", True),
        )
        app.extensions["skvm.limiter"] = limiter
    except Exception as exc:  # pragma: no cover - limiter optional
        log.warning("rate limiting disabled: %s", exc)
        app.extensions["skvm.limiter"] = None


def _register_blueprints(app: Flask) -> None:
    from .routes import register_blueprints

    register_blueprints(app)


def _register_hooks(app: Flask) -> None:
    @app.before_request
    def _before() -> Response | None:
        g.request_started = utcnow()
        g.current_user = None
        settings.all()  # warm the cache

        path = request.path
        if path.startswith("/static/") or path in ("/favicon.ico", "/healthz", "/metrics"):
            return None

        # TLS enforcement for deployments behind an https-only proxy
        # (Codespaces, rigbox, most PaaS). X-Forwarded-Proto is trusted because
        # TRUSTED_PROXY_COUNT documents that a proxy sits in front.
        if current_app.config.get("FORCE_HTTPS") and request.headers.get(
            "X-Forwarded-Proto", request.scheme
        ).split(",")[0].strip() != "https":
            if request.path not in ("/readyz", "/version"):
                target = request.url.replace("http://", "https://", 1)
                return redirect(target, code=308)

        # Maintenance window (staff can still sign in)
        if settings.maintenance() and not path.startswith("/login") and not path.startswith("/api/v1/auth"):
            user = current_user()
            if user is None or not user.is_staff:
                if path.startswith("/api/"):
                    return jsonify({"ok": False, "error": "maintenance", "message": settings.get("maintenance_message")}), 503
                return render_template("maintenance.html"), 503

        # CSRF for cookie-authenticated browser requests
        if path.startswith("/api/") and request.headers.get("Authorization"):
            return None
        if not validate_csrf():
            if path.startswith("/api/"):
                return jsonify({"ok": False, "error": "CSRF validation failed", "code": "csrf"}), 403
            return render_template("error.html", code=403, title="Security check failed",
                                   message="Your session token expired or did not match. Reload the page."), 403
        return None

    @app.after_request
    def _after(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        if current_app.config.get("HSTS"):
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        csp = (
            "default-src 'self'; "
            "img-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "font-src 'self' data:; "
            "connect-src 'self' ws: wss:; "
            "frame-ancestors 'self'; base-uri 'self'; form-action 'self'"
        )
        if not request.path.startswith("/static/vendor/xterm"):
            response.headers.setdefault("Content-Security-Policy", csp)
        if request.path.startswith("/static/"):
            response.cache_timeout = 604800
            response.headers["Cache-Control"] = "public, max-age=604800"
        return response

    @app.teardown_request
    def _teardown(exc: BaseException | None) -> None:
        return None


def _register_error_handlers(app: Flask) -> None:
    def _json_wanted() -> bool:
        return request.path.startswith("/api/") or request.accept_mimetypes.best == "application/json"

    @app.errorhandler(HTTPException)
    def _http_error(error: HTTPException):
        if _json_wanted():
            return jsonify({
                "ok": False,
                "error": error.name.lower().replace(" ", "_"),
                "status": error.code,
                "message": error.description,
            }), error.code
        return render_template("error.html", code=error.code, title=error.name, message=error.description), error.code

    @app.errorhandler(Exception)
    def _unhandled(error: Exception):  # pragma: no cover - safety net
        log.exception("unhandled error on %s", request.path)
        if _json_wanted():
            return jsonify({"ok": False, "error": "internal_error", "status": 500,
                            "message": "The control plane hit an unexpected error."}), 500
        return render_template(
            "error.html", code=500, title="Internal error",
            message="The control plane hit an unexpected error. The incident was logged.",
        ), 500


def _register_cli(app: Flask) -> None:
    import click

    @app.cli.command("init-db")
    def init_db() -> None:
        """Create all tables."""
        db.create_all()
        click.echo("database schema created")

    @app.cli.command("seed")
    @click.option("--username", default="admin")
    @click.option("--password", default=None)
    @click.option("--email", default=None)
    def seed(username: str, password: str | None, email: str | None) -> None:
        """Create the owner account and default settings."""
        from .bootstrap import seed_owner

        password = password or secrets.token_urlsafe(12)
        user = seed_owner(username=username, password=password, email=email)
        click.echo(f"owner account ready: {user.username} / {password}")

    @app.cli.command("demo-data")
    @click.option("--servers", default=4)
    def demo_data(servers: int) -> None:
        """Populate the panel with sample servers, metrics and alerts."""
        from .bootstrap import seed_demo_data

        created = seed_demo_data(server_count=servers)
        click.echo(f"demo data ready ({created} servers)")

    @app.cli.command("worker")
    def worker_cmd() -> None:
        """Run the background sampling/alert worker in the foreground."""
        from .services.reaper import Worker

        runner = Worker(app)
        runner.install_signal_handlers()
        runner.start()
        click.echo("worker running — press Ctrl+C to stop")
        try:
            while runner.thread and runner.thread.is_alive():
                runner.thread.join(timeout=1)
        except KeyboardInterrupt:  # pragma: no cover
            runner.stop()
        click.echo("worker stopped")


def _register_template_helpers(app: Flask) -> None:
    from .routes.helpers import register_template_globals

    register_template_globals(app)

    @app.context_processor
    def _inject():
        if not has_request_context():
            # E-mails rendered from the worker run in an app context only.
            return {}
        user = current_user()
        palette_servers = []
        nav_alerts = 0
        if user is not None:
            try:
                from .models import AlertEvent, AlertRule, Server

                query = db.session.query(Server)
                if not user.can(Role.OPERATOR):
                    query = query.filter(Server.owner_id == user.id)
                palette_servers = query.order_by(Server.name).limit(30).all()
                alert_query = db.session.query(AlertEvent).filter(AlertEvent.acknowledged_at.is_(None))
                if not user.can(Role.OPERATOR):
                    alert_query = alert_query.join(AlertRule, AlertRule.id == AlertEvent.rule_id).filter(
                        AlertRule.user_id == user.id
                    )
                nav_alerts = alert_query.count()
            except Exception:  # pragma: no cover - never break rendering
                palette_servers, nav_alerts = [], 0
        return {
            "palette_servers": palette_servers,
            "nav_alerts": nav_alerts,
            "current_user": user,
            "csrf_token": csrf_token,
            "panel": {
                "name": app.config.get("PANEL_NAME"),
                "tagline": app.config.get("PANEL_TAGLINE"),
                "version": __version__,
                "env": app.config.get("ENV_NAME", "production"),
                "backend": app.config.get("BACKEND"),
                "demo": app.config.get("DEMO_MODE"),
                "support_email": app.config.get("SUPPORT_EMAIL"),
                "accent": app.config.get("BRAND_ACCENT"),
            },
            "settings_values": settings.all(),
            "now": utcnow(),
            "iso": iso,
        }

    app.jinja_env.globals["getattr"] = getattr
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
