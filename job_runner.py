"""SKVM provisioning worker — persistent, restart-safe job pump.

Runs as its own systemd service (skvm-jobs.service) so provisioning
survives panel restarts. Unfinished jobs are recovered idempotently:
backends adopt/repair an existing instance instead of duplicating it.
A periodic reconciliation re-syncs VPS state with the real backend.
"""
from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor

POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="skvm-provision")
INFLIGHT = set()
_last_reconcile = 0.0


def pump(app):
    from core import db
    from core.provision import run_job
    with app.app_context():
        jobs = [j for j in db.list_active_jobs(20) if j.status == "queued"]
        ids = [j.id for j in jobs]
    for jid in ids:
        if jid in INFLIGHT:
            continue
        INFLIGHT.add(jid)

        def _run(jid=jid):
            with app.app_context():
                run_job(jid)

        fut = POOL.submit(_run)
        fut.add_done_callback(lambda _f, jid=jid: INFLIGHT.discard(jid))


def watchdog_once(app=None):
    """One reconciliation pass (also used by scripts/skvm-watchdog.sh)."""
    from core.provision import reconcile_running
    if app is None:
        from app import app as flask_app
        app = flask_app
    with app.app_context():
        reconcile_running()


def main():
    from app import app as flask_app, bootstrap_admin
    from core import db
    from core.config import node_token
    with flask_app.app_context():
        db.init_db()
        db.reset_stale_jobs()
        bootstrap_admin()
        db.ensure_local_node(node_token())
    print("[SKVM jobs] provisioning worker started", flush=True)
    global _last_reconcile
    while True:
        try:
            pump(flask_app)
            if time.time() - _last_reconcile > 60:
                watchdog_once(flask_app)
                _last_reconcile = time.time()
        except Exception as exc:
            print(f"[SKVM jobs] {exc}", flush=True)
        time.sleep(2)


if __name__ == "__main__":
    main()
