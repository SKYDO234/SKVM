#!/usr/bin/env python3
"""SQLite has to survive the panel talking to itself.

A hosted box runs one process with a web thread pool *and* a background sampler, all
against one sqlite file. That combination has a specific failure: a transaction that
starts as a read and then writes has to *upgrade* its lock, and while another writer
holds it sqlite answers ``database is locked`` immediately - the ``busy_timeout`` is
ignored for upgrades, so the panel 500s at whoever happened to be logging in. This
script drives exactly that interleaving (read, pause, write, commit) from several
threads and fails on the first lock error.

    python .scripts/check_db_contention.py

The fix it guards lives in ``skvm/db.py``: WAL + ``BEGIN IMMEDIATE`` so every
transaction takes its write lock up front and queues on ``busy_timeout`` instead.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

THREADS = 6
ROUNDS = 60


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skvm-contention-"))
    os.environ.update(
        {
            "SKVM_INSTANCE_DIR": str(tmp),
            "SKVM_DATABASE_URL": f"sqlite:///{tmp / 't.db'}",
            "SKVM_SECRET_KEY": "y" * 48,
            "SKVM_ENV": "testing",
            "SKVM_FORCE_HTTPS": "0",
            "SKVM_BACKEND": "demo",
        }
    )
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from sqlalchemy import func, select

    from skvm.app import create_app
    from skvm.db import db
    from skvm.models import AuditLog, Server

    app = create_app("testing")
    with app.app_context():
        db.create_all()
        db.session.add(Server(uid="srv-contention", name="contention", owner_id=1, status="running"))
        db.session.commit()

    lock_errors: list[str] = []
    other_errors: list[str] = []
    started = threading.Barrier(THREADS)

    def hammer(index: int) -> None:
        started.wait(timeout=30)
        for round_no in range(ROUNDS):
            try:
                with app.app_context():
                    # Read first: this is what opens a deferred, read-only snapshot.
                    db.session.execute(select(func.count()).select_from(Server)).scalar_one()
                    time.sleep(0.002)
                    # Then write from the same transaction - the upgrade that used to fail.
                    db.session.add(
                        AuditLog(
                            actor=f"thread-{index}",
                            action="contention",
                            target_type="check",
                            target_label=f"r{round_no}",
                            meta={"round": round_no},
                        )
                    )
                    db.session.commit()
            except Exception as exc:  # noqa: BLE001 - the point of the check
                message = str(exc)
                if "locked" in message or "database is locked" in message.lower():
                    lock_errors.append(f"thread {index} round {round_no}: {message[:120]}")
                else:
                    other_errors.append(f"thread {index} round {round_no}: {type(exc).__name__}: {message[:120]}")

    threads = [threading.Thread(target=hammer, args=(i,)) for i in range(THREADS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=180)

    with app.app_context():
        rows = db.session.execute(select(func.count()).select_from(AuditLog)).scalar_one()

    if lock_errors:
        for line in lock_errors[:5]:
            print(f"::error::sqlite lock contention: {line}", flush=True)
        print(
            f"contention check: {len(lock_errors)}/{THREADS * ROUNDS} transactions lost the lock - "
            "write transactions must be opened IMMEDIATE",
            file=sys.stderr,
            flush=True,
        )
        return 1
    if other_errors:
        for line in other_errors[:5]:
            print(f"::error::unexpected error: {line}", flush=True)
        return 1

    print(
        f"contention check ok: {THREADS * ROUNDS} read-then-write transactions from {THREADS} threads, "
        f"{rows} rows landed, no lock errors",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
