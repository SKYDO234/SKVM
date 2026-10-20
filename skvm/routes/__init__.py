"""Blueprint registration."""

from __future__ import annotations

from flask import Flask


def register_blueprints(app: Flask) -> None:
    from .account import bp as account_bp
    from .admin import bp as admin_bp
    from .alerts import bp as alerts_bp
    from .api import bp as api_bp
    from .auth import bp as auth_bp
    from .console import bp as console_bp
    from .dashboard import bp as dashboard_bp
    from .docs import bp as docs_bp
    from .files import bp as files_bp
    from .servers import bp as servers_bp
    from .system import bp as system_bp

    for blueprint in (
        auth_bp,
        dashboard_bp,
        servers_bp,
        console_bp,
        files_bp,
        alerts_bp,
        account_bp,
        admin_bp,
        api_bp,
        docs_bp,
        system_bp,
    ):
        app.register_blueprint(blueprint)
