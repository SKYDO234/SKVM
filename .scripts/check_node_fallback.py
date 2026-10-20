#!/usr/bin/env python3
"""A broken driver must degrade, never 500 the panel.

Builds a throw-away database, points the servers at a hypervisor node that has no
usable agent secret, and asks the app for its pages plus one power action.  That
state is not hypothetical: ``bootstrap.ensure_defaults`` registers the panel host as
a node before any agent exists, and rotating the encryption key invalidates every
secret stored against a node - which is how a freshly hosted box ends up here.

Expected: every page renders (200) and the action answers 400/502 naming the reason.
Anything else - in particular a 500 - is a regression: the panel has to stay
usable enough to explain *why* a server is unreachable.

    python .scripts/check_node_fallback.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="skvm-node-fallback-"))
    os.environ["SKVM_INSTANCE_DIR"] = str(tmp)
    os.environ["SKVM_DATABASE_URL"] = f"sqlite:///{tmp / 'test.db'}"
    os.environ["SKVM_SECRET_KEY"] = "x" * 48
    # Seed through the demo driver: it is the only one that can create rows in a
    # sandbox with no hypervisor, which keeps this check about rendering, not about
    # provisioning. The driver is swapped below, after the rows exist.
    os.environ["SKVM_BACKEND"] = "demo"
    os.environ["SKVM_ENV"] = "testing"
    # The test client talks plain http to itself; a hosted box sets this in .env
    # and would answer every request with a 308 instead of a page.
    os.environ["SKVM_FORCE_HTTPS"] = "0"
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    from skvm.app import create_app
    from skvm.bootstrap import seed_demo_data, seed_owner
    from skvm.db import db, utcnow
    from skvm.models import Node, Server

    app = create_app("testing")
    if not app.config.get("TESTING"):
        print("::error::expected the testing config (no rate limits, csrf off) to be active", flush=True)
        return 1

    password = "fallback-test-password"
    with app.app_context():
        db.create_all()
        # Owner first, with a known password: ensure_defaults() would generate one.
        owner = seed_owner(password=password)
        if seed_demo_data(owner=owner, server_count=2, with_history=False) < 1:
            if db.session.query(Server).count() == 0:
                print("::error::could not seed servers to point at a broken node", flush=True)
                return 1

        node = Node(
            name="far-away",
            endpoint="https://10.0.0.9:8443",
            region="test",
            status="online",
            last_seen_at=utcnow(),
            priority=1,
        )
        db.session.add(node)
        db.session.commit()
        for server in db.session.query(Server).all():
            server.node_id = node.id
            server.driver = "node"
        db.session.commit()
        first = db.session.query(Server).order_by(Server.id).first()
        uid, node_id = first.uid, node.id

    # Now take the demo crutch away: every lookup has to go through the node.
    app.config["BACKEND"] = "node"

    failures: list[str] = []
    client = app.test_client()
    logged_in = client.post("/login", json={"username": "admin", "password": password})
    if logged_in.status_code not in (200, 302):
        print(f"::error::login failed in the test app ({logged_in.status_code})", flush=True)
        return 1

    for path in (
        "/dashboard",
        "/servers/",
        f"/servers/{uid}",
        f"/servers/{uid}/console",
        "/admin/nodes",
        "/admin/servers",
        "/metrics",
    ):
        status = client.get(path).status_code
        if status != 200:
            failures.append(f"GET {path} -> {status} (wanted 200)")

    response = client.post(f"/servers/{uid}/api/v1/power", json={"action": "restart"})
    body = response.get_data(as_text=True).lower()
    if response.status_code not in (400, 502):
        failures.append(
            f"POST /servers/{uid}/api/v1/power -> {response.status_code} (wanted a handled 400/502, not a 500)"
        )
    elif "unusable" not in body and "secret" not in body and "node" not in body:
        failures.append(f"the power failure does not explain itself: {body[:160]}")

    with app.app_context():
        from skvm.backends import UnavailableBackend, connect_node, get_backend

        resolved = get_backend(db.session.query(Server).order_by(Server.id).first())
        if not isinstance(resolved, UnavailableBackend):
            failures.append(f"get_backend() returned {type(resolved).__name__}, not the stand-in driver")
        if connect_node(db.session.get(Node, node_id)) is None:  # pragma: no cover - defensive
            failures.append("connect_node() returned None")

    if failures:
        for line in failures:
            print(f"::error::{line}", flush=True)
        print(f"node fallback check: {len(failures)} problem(s)", file=sys.stderr, flush=True)
        return 1

    print(
        "node fallback check ok: pages render, the action answers with a handled reason, "
        "the driver resolves to UnavailableBackend",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
