"""SKVM node agent API (optional, token-authenticated).

Exposes node capabilities and VPS actions to a trusted panel caller.
Binds to 127.0.0.1 by default; every route except /health requires the
node token (constant-time comparison). Never expose this port publicly.
"""
from __future__ import annotations
import hmac

from flask import Flask, jsonify, request

from app import app as panel_app  # noqa: F401  (ensures DB schema exists)
from core import backend, db
from core.capabilities import snapshot
from core.config import NODE_BIND, NODE_NAME, NODE_PORT, node_token

app = Flask(__name__)
TOKEN = node_token()


def _auth_ok() -> bool:
    given = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    return hmac.compare_digest(given, TOKEN)


@app.before_request
def guard():
    if request.path == "/health":
        return None
    if not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized"}), 401
    return None


@app.get("/health")
def health():
    return jsonify({"ok": True, "node": NODE_NAME, "service": "skvm-node"})


@app.get("/capabilities")
def capabilities():
    with panel_app.app_context():
        return jsonify(snapshot())


@app.get("/resources")
def resources():
    with panel_app.app_context():
        return jsonify(snapshot())


@app.post("/v1/vps/<int:vps_id>/<op>")
def vps_op(vps_id, op):
    with panel_app.app_context():
        v = db.get_vps(vps_id)
        if not v:
            return jsonify({"ok": False, "error": "not found"}), 404
        try:
            st = backend.do_action(v, op)
            db.update_vps(vps_id, state=("running" if st.get("state") == "running" else "stopped"),
                          ip_address=st.get("ip"))
            return jsonify({"ok": True, **st})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 400


@app.post("/v1/reconcile")
def reconcile():
    from core.provision import reconcile_running
    with panel_app.app_context():
        reconcile_running()
    return jsonify({"ok": True})


if __name__ == "__main__":
    with panel_app.app_context():
        db.init_db()
    app.run(host=NODE_BIND, port=NODE_PORT, debug=False)
