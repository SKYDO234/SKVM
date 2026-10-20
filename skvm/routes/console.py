"""Interactive console + live metric streaming over WebSockets."""

from __future__ import annotations

import json
import logging
import threading
import time

from flask import Blueprint, current_app, render_template, request

from ..backends import BackendError, get_backend
from ..db import db, iso, utcnow
from ..models import Role, Server, ServerStatus
from ..security import current_user, login_required
from ..services import audit, metrics
from ..wsutil import OP_CLOSE, WebSocket, WebSocketError, console_origin_allowed

log = logging.getLogger("skvm.console")

bp = Blueprint("console", __name__, url_prefix="/ws")


def _resolve_console_token(token: str) -> dict | None:
    store = current_app.extensions.get("skvm.console_tokens") or {}
    record = store.get(token)
    if not record:
        return None
    if record["expires"] < utcnow():
        store.pop(token, None)
        return None
    return record


def _authorise_server(uid: str, token: str | None) -> Server | None:
    """Authorise a WebSocket handshake via one-shot token or session cookie."""
    server = db.session.query(Server).filter(Server.uid == uid).one_or_none()
    if server is None:
        return None
    if token:
        record = _resolve_console_token(token)
        if record and record["server_id"] == server.id:
            from ..models import User

            user = db.session.get(User, record["user_id"])
            if user and (server.owner_id == user.id or user.can(Role.OPERATOR)):
                return server
        return None
    user = current_user()
    if user and (server.owner_id == user.id or user.can(Role.OPERATOR)):
        return server
    return None


# --------------------------------------------------------------------------- #
# Console
# --------------------------------------------------------------------------- #
@bp.route("/server/<uid>/console")
def console_socket(uid: str):
    if not console_origin_allowed():
        return "origin rejected", 403
    token = request.args.get("token")
    server = _authorise_server(uid, token)
    if server is None:
        return "unauthorised", 403
    if not current_app.config.get("ALLOW_CONSOLE", True):
        return "console disabled", 403

    try:
        ws = WebSocket.from_request()
    except WebSocketError as exc:
        log.warning("websocket upgrade failed: %s", exc)
        return str(exc), 500
    if ws is None:
        return "expected a websocket upgrade", 400

    user = current_user()
    actor = user.username if user else "token"
    app = current_app._get_current_object()
    thread = threading.Thread(
        target=_run_console,
        args=(app, ws, server.id, actor, token),
        name=f"console-{server.uid}",
        daemon=True,
    )
    thread.start()
    # The response is already fully written to the socket by the upgrade.
    return ""


def _run_console(app, ws: WebSocket, server_id: int, actor: str, token: str | None) -> None:
    with app.app_context():
        server = db.session.get(Server, server_id)
        if server is None:
            ws.close(1008, "server not found")
            return
        if token:
            (app.extensions.get("skvm.console_tokens") or {}).pop(token, None)

        ws.set_timeout(0.5)
        ws.send_json(
            {
                "type": "hello",
                "server": server.public(),
                "message": f"Attached to {server.name} ({server.status})",
                "ts": iso(utcnow()),
            }
        )

        if server.status != ServerStatus.RUNNING.value:
            ws.send_json({"type": "error", "message": f"Server is {server.status} — start it to open a console."})
            session = None
        else:
            session = _open_session(server, ws)

        audit.record("console.attach", target_type="server", target_id=server.uid, target_label=server.name,
                     actor=actor, channel="ws")
        db.session.commit()

        last_ping = time.time()
        try:
            while not ws.closed:
                if session is not None:
                    try:
                        chunk = session.read(timeout=0.05)
                    except Exception as exc:  # pragma: no cover
                        ws.send_json({"type": "error", "message": f"console stream ended: {exc}"})
                        chunk = b""
                    if chunk:
                        try:
                            ws.send_json({"type": "output", "data": chunk.decode("utf-8", "replace")})
                        except WebSocketError:
                            break
                message = ws.read_text(timeout=0.05)
                if message:
                    try:
                        payload = json.loads(message)
                    except ValueError:
                        payload = {"type": "input", "data": message}
                    _handle_client_message(ws, session, payload)
                if time.time() - last_ping > 25:
                    try:
                        ws.send_json({"type": "heartbeat", "ts": iso(utcnow())})
                    except WebSocketError:
                        break
                    last_ping = time.time()
        except WebSocketError:
            pass
        finally:
            if session is not None:
                try:
                    session.close()
                except Exception:  # pragma: no cover
                    pass
            audit.record("console.detach", target_type="server", target_id=server.uid,
                         target_label=server.name, actor=actor, channel="ws")
            try:
                db.session.commit()
            except Exception:  # pragma: no cover
                db.session.rollback()
            ws.close(1000, "closed")


def _open_session(server: Server, ws: WebSocket):
    backend = get_backend(server)
    try:
        session = backend.console(server.container_id, "/bin/bash")
    except BackendError as exc:
        ws.send_json({"type": "warning", "message": f"bash unavailable ({exc}); falling back to sh"})
        try:
            session = backend.console(server.container_id, "/bin/sh")
        except BackendError as inner:
            ws.send_json({"type": "error", "message": f"Cannot open a console: {inner}"})
            return None
    try:
        session.resize(32, 110)
    except Exception:  # pragma: no cover
        pass
    ws.send_json({"type": "ready", "shell": "/bin/bash"})
    return session


def _handle_client_message(ws: WebSocket, session, payload: dict) -> None:
    kind = payload.get("type")
    if kind == "input":
        if session is None:
            ws.send_json({"type": "error", "message": "Console is not attached"})
            return
        try:
            session.write(str(payload.get("data", "")).encode())
        except Exception as exc:  # pragma: no cover
            ws.send_json({"type": "error", "message": f"write failed: {exc}"})
    elif kind == "resize":
        if session is None:
            return
        try:
            session.resize(int(payload.get("rows", 32)), int(payload.get("cols", 110)))
        except Exception:  # pragma: no cover
            pass
    elif kind == "ping":
        ws.send_json({"type": "pong", "ts": iso(utcnow())})
    elif kind == "close":
        ws.close(1000, "client closed")


# --------------------------------------------------------------------------- #
# Live metrics stream
# --------------------------------------------------------------------------- #
@bp.route("/server/<uid>/metrics")
def metrics_socket(uid: str):
    if not console_origin_allowed():
        return "origin rejected", 403
    token = request.args.get("token")
    server = _authorise_server(uid, token)
    if server is None:
        return "unauthorised", 403

    try:
        ws = WebSocket.from_request()
    except WebSocketError as exc:
        return str(exc), 500
    if ws is None:
        return "expected a websocket upgrade", 400

    app = current_app._get_current_object()
    try:
        interval = float(request.args.get("interval") or 2.0)
    except ValueError:
        interval = 2.0
    thread = threading.Thread(
        target=_run_metrics,
        args=(app, ws, server.id, interval),
        name=f"metrics-{server.uid}",
        daemon=True,
    )
    thread.start()
    return ""


def _run_metrics(app, ws: WebSocket, server_id: int, interval: float) -> None:
    interval = max(1.0, min(10.0, interval))
    with app.app_context():
        import time as _time

        deadline = _time.time() + 3600  # streams are capped at one hour per connection
        try:
            while not ws.closed and _time.time() < deadline:
                server = db.session.get(Server, server_id)
                if server is None:
                    ws.send_json({"type": "error", "message": "server no longer exists"})
                    break
                db.session.expire_all()
                stats = metrics.collect(server, cache_seconds=1.5)
                ws.send_json(
                    {
                        "type": "metrics",
                        "server": server.uid,
                        "status": server.status,
                        "stats": stats,
                        "series": metrics.live_series(server, points=60),
                        "ts": iso(utcnow()),
                    }
                )
                message = ws.read_text(timeout=interval)
                if message:
                    try:
                        payload = json.loads(message)
                    except ValueError:
                        payload = {}
                    if payload.get("type") == "close":
                        break
                    if payload.get("type") == "interval":
                        interval = max(1.0, min(10.0, float(payload.get("value", interval))))
        except (WebSocketError, ValueError):
            pass
        finally:
            ws.close(1000, "stream ended")
