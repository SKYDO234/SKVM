"""
SKVM — Sovereign Cloud Control Panel
===================================

A provider-grade virtual-server control panel: provisioning, console, file
manager, snapshots, backups, metrics, alerting, quota and audit — built on
Flask + Docker with a bespoke glassmorphic front-end.

The application is exposed as a factory so it can be embedded in Gunicorn,
tests, or a CLI context::

    from skvm import create_app
    app = create_app()

Package layout
--------------
``skvm.backends``   pluggable virtualization drivers (docker / node agent / demo)
``skvm.routes``     blueprints — one module per product surface
``skvm.services``   domain services (metrics, alerting, mail, audit, reaper)
``skvm.models``     SQLAlchemy 2.0 ORM
``skvm.security``   password hashing, sessions, scopes, CSRF, 2FA
"""

from __future__ import annotations

__version__ = "2.0.0"
__all__ = ["create_app", "__version__"]


def create_app(config_object: str | object | None = None, **overrides) -> "object":
    """Application factory — see :func:`skvm.app.create_app`."""
    from .app import create_app as _create_app

    return _create_app(config_object, **overrides)
