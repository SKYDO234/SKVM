#!/usr/bin/env python3
"""SKVM control-plane CLI.

    python manage.py run            # dev server (websockets enabled)
    python manage.py serve          # production-ish threaded server
    python manage.py worker         # background sampler + alert evaluator
    python manage.py init-db        # create/patch the schema
    python manage.py seed           # create the owner account
    python manage.py demo-data      # seed simulated servers/metrics
    python manage.py doctor         # environment + hypervisor diagnostics
    python manage.py node-key       # generate a node agent keypair
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from skvm import __version__, create_app  # noqa: E402
from skvm.config import BASE_DIR, INSTANCE_DIR  # noqa: E402


def build_app(args):
    env = args.env or os.environ.get("SKVM_ENV", "development" if args.command in ("run", "demo-data") else "production")
    return create_app(env)


def cmd_run(args) -> int:
    app = build_app(args)
    _banner(app, args)
    app.run(
        host=args.host,
        port=args.port,
        debug=bool(args.debug),
        threaded=True,
        use_reloader=False,
        ssl_context=("adhoc" if args.tls else None),
    )
    return 0


def cmd_serve(args) -> int:
    """Threaded WSGI server that supports upgrades (gunicorn gthread if present)."""
    app = build_app(args)
    try:
        from gunicorn.app.base import BaseApplication  # type: ignore

        class _App(BaseApplication):
            def __init__(self, application, options):
                self.options = options
                self.application = application
                super().__init__()

            def load_config(self):
                for key, value in self.options.items():
                    if key in self.cfg.settings and value is not None:
                        self.cfg.set(key.lower(), value)

            def load(self):
                return self.application

        workers = args.workers or max(2, (os.cpu_count() or 2))
        options = {
            "bind": f"{args.host}:{args.port}",
            "workers": 1 if os.environ.get("SKVM_REAPER", "1") not in ("0", "false") else workers,
            "threads": args.threads or 16,
            "worker_class": "gthread",
            "timeout": 120,
            "graceful_timeout": 30,
            "keepalive": 5,
            "accesslog": "-",
            "loglevel": "info",
            "proc_name": "skvm",
        }
        if os.environ.get("SKVM_TLS_CERT") and os.environ.get("SKVM_TLS_KEY"):
            options["certfile"] = os.environ["SKVM_TLS_CERT"]
            options["keyfile"] = os.environ["SKVM_TLS_KEY"]
        print(f"[skvm] starting gunicorn (gthread × {options['threads']}) on {options['bind']}")
        _App(app, options).run()
        return 0
    except ImportError:
        print("[skvm] gunicorn is not installed — falling back to the built-in threaded server")
        app.run(host=args.host, port=args.port, threaded=True, debug=False, use_reloader=False)
        return 0


def cmd_worker(args) -> int:
    app = build_app(args)
    from skvm.services.reaper import Worker

    runner = Worker(app)
    runner.install_signal_handlers()
    runner.start()
    print(f"[skvm] worker running (pid {os.getpid()}) — Ctrl+C to stop")
    try:
        while runner.thread and runner.thread.is_alive():
            runner.thread.join(timeout=1)
    except KeyboardInterrupt:
        runner.stop()
    print("[skvm] worker stopped")
    return 0


def cmd_init_db(args) -> int:
    app = build_app(args)
    with app.app_context():
        from skvm.db import db

        db.create_all()
        print("[skvm] schema created/verified")
    return 0


def cmd_seed(args) -> int:
    app = build_app(args)
    with app.app_context():
        from skvm.bootstrap import ensure_defaults, seed_owner

        ensure_defaults(app)
        password = args.password or secrets.token_urlsafe(12)
        user = seed_owner(username=args.username, password=password, email=args.email)
        print(f"[skvm] owner ready → {user.username} / {password}")
    return 0


def cmd_demo_data(args) -> int:
    app = build_app(args)
    with app.app_context():
        from skvm.bootstrap import seed_demo_data

        created = seed_demo_data(server_count=args.servers)
        print(f"[skvm] demo data seeded ({created} servers)")
    return 0


def cmd_node_key(args) -> int:
    key_id = f"sk_{secrets.token_hex(6)}"
    secret = secrets.token_urlsafe(32)
    print("SKVM node agent credentials")
    print(f"  key id : {key_id}")
    print(f"  secret : {secret}")
    print("\nStart the agent with:")
    print(f"  SKVM_NODE_KEY={key_id} SKVM_NODE_SECRET={secret} SKVM_NODE_PORT=9105 python3 skvm_node/agent.py")
    return 0


def cmd_doctor(args) -> int:
    app = build_app(args)
    lines: list[str] = []

    def row(label: str, value: str) -> None:
        lines.append(f"  {label:<26} {value}")

    lines.append(f"SKVM {__version__}\n")
    lines.append("Environment")
    row("python", sys.version.split()[0])
    row("instance dir", str(INSTANCE_DIR))
    row("database", app.config["SQLALCHEMY_DATABASE_URL"].split("@")[-1])
    row("secret key", "persistent" if app.config.get("SECRET_KEY") else "MISSING")
    row("encryption key", "ok" if app.config.get("ENCRYPTION_KEY_RESOLVED") else "MISSING")
    row("env", app.config.get("ENV_NAME"))
    row("backend config", app.config.get("BACKEND"))

    lines.append("\nHypervisors")
    from skvm.backends import available_backends, backend_status

    for entry in available_backends():
        state = "✔" if entry["available"] else "✘"
        row(f"{state} {entry['name']}", entry["detail"])

    lines.append("\nDatabase")
    with app.app_context():
        ok, detail = _db_check()
        row("connection", "ok" if ok else f"FAILED ({detail})")
        from skvm.models import AlertRule, Node, Server, User

        row("users", str(_count(User)))
        row("servers", str(_count(Server)))
        row("nodes", str(_count(Node)))
        row("alert rules", str(_count(AlertRule)))

    lines.append("\nOptional features")
    for module, label in (
        ("docker", "docker SDK (container driver)"),
        ("psutil", "psutil (host metrics)"),
        ("qrcode", "qrcode (server-side QR)"),
        ("cryptography", "cryptography (secret encryption)"),
        ("pyotp", "pyotp (two-factor auth)"),
        ("gunicorn", "gunicorn (production server)"),
        ("flask_limiter", "flask-limiter (rate limiting)"),
    ):
        try:
            __import__(module)
            row(f"✔ {label}", "available")
        except Exception:
            row(f"✘ {label}", "not installed")

    print("\n".join(lines))
    return 0


def _db_check():
    from skvm.db import db

    return db.health()


def _count(model) -> int:
    from skvm.db import db

    return db.session.query(model).count()


def _banner(app, args) -> None:
    url = f"{'https' if args.tls else 'http'}://{args.host}:{args.port}"
    print(
        f"""
  ███████╗██╗  ██╗██╗   ██╗███╗   ███╗
  ██╔════╝██║ ██╔╝██║   ██║████╗ ████║   {app.config.get('PANEL_NAME')} {__version__}
  ███████╗█████╔╝ ██║   ██║██╔████╔██║   {app.config.get('PANEL_TAGLINE')}
  ╚════██║██╔═██╗ ╚██╗ ██╔╝██║╚██╔╝██║
  ███████║██║  ██╗ ╚████╔╝ ██║ ╚═╝ ██║   backend   : {app.config.get('BACKEND')}
  ╚══════╝╚═╝  ╚═╝  ╚═══╝  ╚═╝     ╚═╝   environment: {app.config.get('ENV_NAME')}
                                          listening  : {url}
"""
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="manage.py", description="SKVM control plane")
    parser.add_argument("--env", default=None, help="development|production|testing")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p, host: str = "127.0.0.1", port: int = 5000):
        p.add_argument("--host", default=host)
        p.add_argument("--port", type=int, default=port)
        p.add_argument("--tls", action="store_true", help="serve HTTPS with an ad-hoc certificate")
        p.add_argument("--debug", action="store_true")

    run_p = sub.add_parser("run", help="development server")
    add_common(run_p)

    serve_p = sub.add_parser("serve", help="production threaded server")
    add_common(serve_p, host="0.0.0.0", port=8000)
    serve_p.add_argument("--workers", type=int, default=None)
    serve_p.add_argument("--threads", type=int, default=16)

    worker_p = sub.add_parser("worker", help="background worker")
    add_common(worker_p)

    sub.add_parser("init-db", help="create the schema")

    seed_p = sub.add_parser("seed", help="create an owner account")
    seed_p.add_argument("--username", default="admin")
    seed_p.add_argument("--password", default=None)
    seed_p.add_argument("--email", default=None)

    demo_p = sub.add_parser("demo-data", help="seed simulated infrastructure")
    demo_p.add_argument("--servers", type=int, default=5)

    sub.add_parser("node-key", help="generate node agent credentials")
    sub.add_parser("doctor", help="diagnostics")

    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "serve": cmd_serve,
        "worker": cmd_worker,
        "init-db": cmd_init_db,
        "seed": cmd_seed,
        "demo-data": cmd_demo_data,
        "node-key": cmd_node_key,
        "doctor": cmd_doctor,
    }
    if not hasattr(args, "host"):
        args.host, args.port, args.tls, args.debug = "127.0.0.1", 5000, False, False
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
