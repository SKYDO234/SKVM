"""Background worker: metric sampling, alert evaluation, retention, billing.

Run it inside the web process (single-worker deployments) or standalone::

    python manage.py worker

For multi-worker WSGI setups keep ``SKVM_REAPER=0`` on the web workers and run
exactly one ``manage.py worker`` process to avoid duplicate sampling.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
from datetime import timedelta
from typing import Any

from ..backends import BackendError, get_backend
from ..nodeclient import NodeError
from ..db import db, utcnow
from ..models import (
    ApiToken,
    Backup,
    Node,
    ResetToken,
    Server,
    ServerMetric,
    ServerStatus,
    Session,
    User,
)
from . import alerts, audit, metrics, provisioning
from .settings import settings

log = logging.getLogger("skvm.worker")


class Worker:
    """One cooperative loop with per-task schedules."""

    def __init__(self, app=None) -> None:
        self.app = app
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.last_run: dict[str, float] = {}
        self.stats: dict[str, Any] = {"cycles": 0, "samples": 0, "alerts": 0, "errors": 0, "started_at": None}
        self._history_counts: dict[int, int] = {}

    # -- lifecycle -------------------------------------------------------- #
    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self.run_forever, name="skvm-worker", daemon=True)
        self.thread.start()
        self.stats["started_at"] = utcnow().isoformat()
        log.info("background worker started")

    def stop(self, *_args) -> None:
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, self.stop)
            except (ValueError, OSError):  # pragma: no cover - non-main thread
                pass

    # -- scheduling ------------------------------------------------------- #
    def due(self, task: str, interval: float) -> bool:
        now = time.time()
        if now - self.last_run.get(task, 0.0) >= interval:
            self.last_run[task] = now
            return True
        return False

    def run_forever(self) -> None:
        while not self.stop_event.is_set():
            started = time.time()
            try:
                self.cycle()
            except Exception as exc:  # pragma: no cover - keep the loop alive
                self.stats["errors"] = int(self.stats["errors"]) + 1
                log.exception("worker cycle failed: %s", exc)
            self.stats["cycles"] = int(self.stats["cycles"]) + 1
            elapsed = time.time() - started
            self.stop_event.wait(max(1.0, 5.0 - elapsed))

    def cycle(self) -> None:
        app = self.app
        if app is None:  # pragma: no cover
            from flask import current_app

            app = current_app._get_current_object()

        with app.app_context():
            interval = max(5, int(app.config.get("METRIC_INTERVAL", 15)))
            if self.due("sample", interval):
                self.sample_servers(interval)
            if self.due("alerts", max(10, int(app.config.get("ALERT_EVAL_INTERVAL", 30)))):
                fired = alerts.evaluate_all()
                self.stats["alerts"] = int(self.stats["alerts"]) + len(
                    [f for f in fired if f["kind"] == "firing"]
                )
                db.session.commit()
            if self.due("retention", 3600):
                self.retention()
            if self.due("nodes", 60):
                self.check_nodes()
            if self.due("expiry", 1800):
                provisioning.expire_overdue_servers()
                db.session.commit()

    # -- tasks ------------------------------------------------------------ #
    def sample_servers(self, interval: int) -> None:
        servers = db.session.query(Server).all()
        sampled = 0
        persist_every = max(1, 60 // max(interval, 1))
        for server in servers:
            if server.status not in (ServerStatus.RUNNING.value, ServerStatus.STOPPED.value):
                continue
            try:
                stats = metrics.collect(server, cache_seconds=0.5)
            except Exception as exc:  # pragma: no cover - driver specific
                log.debug("sampling %s failed: %s", server.uid, exc)
                continue
            stats["_interval"] = interval
            counter = self._history_counts.get(server.id, 0) + 1
            if counter >= persist_every:
                counter = 0
                metrics.record_history(server, stats)
            self._history_counts[server.id] = counter
            sampled += 1
        db.session.commit()
        self.stats["samples"] = int(self.stats["samples"]) + sampled

    def retention(self) -> None:
        days = int(settings.get("metrics_retention_days") or 30)
        cutoff = utcnow() - timedelta(days=days)
        removed = (
            db.session.query(ServerMetric)
            .filter(ServerMetric.ts < cutoff)
            .delete(synchronize_session=False)
        )
        expired_sessions = (
            db.session.query(Session)
            .filter(Session.expires_at < utcnow() - timedelta(days=7))
            .delete(synchronize_session=False)
        )
        dead_tokens = (
            db.session.query(ApiToken)
            .filter(ApiToken.revoked_at.isnot(None), ApiToken.revoked_at < utcnow() - timedelta(days=30))
            .delete(synchronize_session=False)
        )
        used_resets = (
            db.session.query(ResetToken)
            .filter(ResetToken.expires_at < utcnow() - timedelta(days=1))
            .delete(synchronize_session=False)
        )
        expired_backups = (
            db.session.query(Backup)
            .filter(Backup.expires_at.isnot(None), Backup.expires_at < utcnow())
            .all()
        )
        for backup in expired_backups:
            try:
                if backup.storage_path and os.path.exists(backup.storage_path):
                    os.remove(backup.storage_path)
            except OSError:  # pragma: no cover
                pass
            db.session.delete(backup)
        db.session.commit()
        log.info(
            "retention: %s metrics, %s sessions, %s tokens, %s reset links, %s backups removed",
            removed, expired_sessions, dead_tokens, used_resets, len(expired_backups),
        )

    def check_nodes(self) -> None:
        nodes = db.session.query(Node).all()
        for node in nodes:
            from ..backends.node_backend import NodeBackend

            try:
                # Construction itself can fail (missing/rotated agent secret) - one
                # unreachable node must never abort the rest of the worker cycle.
                backend = NodeBackend(node)
                data = backend.client.health()
            except (BackendError, NodeError) as exc:
                node.status = "offline"
                node.last_error = str(exc)[:250]
                db.session.commit()
                continue
            node.status = "online"
            node.last_error = ""
            node.version = str(data.get("version", node.version))[:32]
            node.last_seen_at = utcnow()
        db.session.commit()


worker = Worker()


def start_worker(app) -> Worker | None:
    """Start the in-process worker when configuration allows it."""
    if app.config.get("TESTING"):
        return None
    if not app.config.get("REAPER_ENABLED", True):
        log.info("background worker disabled by configuration")
        return None
    worker.app = app
    worker.start()
    return worker


def stop_worker() -> None:
    worker.stop()
    if worker.thread and worker.thread.is_alive():
        worker.thread.join(timeout=5)
