"""Runtime-editable panel settings (stored in the ``settings`` table).

Environment variables stay authoritative for secrets and infrastructure; this
store covers operator-tunable *product* switches such as registration policy,
default quotas, notices and maintenance mode.  Values are cached for a few
seconds to keep request-time overhead negligible.
"""

from __future__ import annotations

import threading
import time
from typing import Any

DEFAULTS: dict[str, Any] = {
    "allow_registration": True,
    "require_approval": False,
    "maintenance_mode": False,
    "maintenance_message": "SKVM is undergoing scheduled maintenance. Panels will return shortly.",
    "panel_notice": "",
    "panel_notice_level": "info",
    "welcome_message": "Welcome to your sovereign cloud console.",
    "default_quotas": {"servers": 5, "cpu": 16, "ram_mb": 32768, "disk_gb": 500, "snapshots": 10, "bandwidth_gb": 2000},
    "default_os": "ubuntu-24.04",
    "allow_user_snapshots": True,
    "allow_user_backups": True,
    "allow_user_reinstall": True,
    "allow_self_delete": True,
    "console_enabled": True,
    "file_manager_enabled": True,
    "api_enabled": True,
    "signup_credits": 0.0,
    "currency": "USD",
    "alert_email_enabled": True,
    "metrics_retention_days": 30,
    "status_page_public": True,
    "session_lifetime_hours": 12,
    "require_2fa_for_staff": False,
    "billing_enabled": False,
}

_LOCKS = threading.Lock()


class SettingsStore:
    """Cache-fronted key/value store backed by the ``settings`` table."""

    def __init__(self, ttl: float = 5.0) -> None:
        self.ttl = ttl
        self._cache: dict[str, Any] = {}
        self._loaded_at = 0.0

    # -- reading --------------------------------------------------------- #
    def all(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        if not force and self._cache and (now - self._loaded_at) < self.ttl:
            return self._cache
        values = dict(DEFAULTS)
        try:
            from ..db import db
            from ..models import Setting

            for row in db.session.query(Setting).all():
                if isinstance(row.value, dict) and "value" in row.value and len(row.value) == 1:
                    values[row.key] = row.value["value"]
                else:
                    values[row.key] = row.value
        except Exception:
            # Outside an app/DB context (CLI, worker boot) fall back to defaults.
            pass
        with _LOCKS:
            self._cache = values
            self._loaded_at = now
        return values

    def get(self, key: str, default: Any = None) -> Any:
        values = self.all()
        if key in values:
            return values[key]
        return DEFAULTS.get(key, default)

    def boolean(self, key: str) -> bool:
        return bool(self.get(key, False))

    # -- writing --------------------------------------------------------- #
    def set(self, key: str, value: Any, updated_by: str = "system") -> None:
        from ..db import db
        from ..models import Setting

        row = db.session.get(Setting, key)
        payload = value
        if row is None:
            row = Setting(key=key, value=payload, updated_by=updated_by)
            db.session.add(row)
        else:
            row.value = payload
            row.updated_by = updated_by
        db.session.flush()
        self.invalidate()

    def update(self, values: dict[str, Any], updated_by: str = "system") -> list[str]:
        changed = []
        for key, value in values.items():
            if key not in DEFAULTS:
                continue
            if self.get(key) != value:
                self.set(key, value, updated_by=updated_by)
                changed.append(key)
        return changed

    def invalidate(self) -> None:
        with _LOCKS:
            self._cache = {}
            self._loaded_at = 0.0

    # -- domain helpers -------------------------------------------------- #
    def quotas(self) -> dict[str, int]:
        quotas = dict(DEFAULTS["default_quotas"])
        quotas.update(self.get("default_quotas") or {})
        return quotas

    def registration_open(self) -> bool:
        from flask import current_app

        env_allowed = bool(current_app.config.get("ALLOW_REGISTRATION", True))
        return env_allowed and bool(self.get("allow_registration", True))

    def maintenance(self) -> bool:
        return bool(self.get("maintenance_mode", False))


settings = SettingsStore()
