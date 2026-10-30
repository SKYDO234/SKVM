"""SKVM PANEL smoke/security tests.

Run:  SKVM_ADMIN_PASSWORD=... venv/bin/python -m pytest tests/ -q
Uses a throwaway SQLite DB in a temp dir. Provisioning success paths use a
mocked backend (no Docker/Incus needed); failure paths assert the panel
fails honestly instead of faking success.
"""
import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="skvm-test-")
os.environ["SKVM_DATA_DIR"] = _TMP
os.environ["SKVM_DB"] = os.path.join(_TMP, "skvm.db")
os.environ["SKVM_SECRET_FILE"] = os.path.join(_TMP, ".secret")
os.environ["SKVM_NODE_TOKEN_FILE"] = os.path.join(_TMP, ".token")
os.environ["SKVM_ADMIN_USERNAME"] = "admin"
os.environ["SKVM_ADMIN_PASSWORD"] = "TestAdminPass123"
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from app import app as flask_app, bootstrap_admin  # noqa: E402
from core import backend, db  # noqa: E402
from core.extensions import socketio  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init():
    with flask_app.app_context():
        db.init_db()
        bootstrap_admin()
        from core.config import node_token
        db.ensure_local_node(node_token())
    flask_app.config["TESTING"] = True
    # CSRF is exercised explicitly in test_csrf_required; keep it ON globally.


@pytest.fixture()
def client():
    return flask_app.test_client()


def _csrf(client):
    with client.session_transaction() as sess:
        # Flask-WTF stores no stable token in session for GET-less flows;
        # fetch a page that renders a form instead.
        pass
    resp = client.get("/login")
    import re
    m = re.search(rb'name="csrf_token" value="([^"]+)"', resp.data)
    assert m, "login form must render a CSRF token"
    return m.group(1).decode()


def _register(client, username="alice", password="AlicePass123"):
    token = _csrf(client)
    return client.post("/register", data={"csrf_token": token, "username": username,
                                          "password": password}, follow_redirects=False)


def _login(client, username, password):
    token = _csrf(client)
    return client.post("/login", data={"csrf_token": token, "username": username,
                                       "password": password}, follow_redirects=False)


def test_health_and_landing(client):
    assert client.get("/healthz").get_json()["service"] == "skvm-panel"
    page = client.get("/")
    assert page.status_code == 200
    assert b"SKVM PANEL" in page.data


def test_register_login_dashboard(client):
    r = _register(client)
    assert r.status_code in (302, 303)
    dash = client.get("/dashboard")
    assert dash.status_code == 200
    assert b"alice" in dash.data


def test_csrf_required(client):
    # No csrf_token -> Flask-WTF must reject.
    r = client.post("/login", data={"username": "admin", "password": "x"})
    assert r.status_code == 400


def test_trust_proxy_auto_detection(monkeypatch):
    from core.config import PROXY_ENV_MARKERS, trust_proxy_enabled
    for marker in PROXY_ENV_MARKERS:
        monkeypatch.delenv(marker, raising=False)
    monkeypatch.delenv("SKVM_TRUST_PROXY", raising=False)
    assert trust_proxy_enabled() is False

    # Codespaces/Gitpod previews auto-enable forwarded-header trust...
    monkeypatch.setenv("CODESPACES", "true")
    assert trust_proxy_enabled() is True
    # ...but an explicit operator setting always wins.
    monkeypatch.setenv("SKVM_TRUST_PROXY", "false")
    assert trust_proxy_enabled() is False
    monkeypatch.setenv("SKVM_TRUST_PROXY", "true")
    monkeypatch.delenv("CODESPACES", raising=False)
    assert trust_proxy_enabled() is True


def test_codespaces_forwarded_login_not_blocked_by_csrf_referrer(monkeypatch):
    # GitHub Codespaces rewrites Host to localhost:5555 and sends the
    # public preview host in X-Forwarded-Host (+ Proto https). Without
    # ProxyFix, Flask-WTF rejects the login POST with
    # "Bad Request: The referrer does not match the host."
    monkeypatch.setenv("CODESPACES", "true")
    monkeypatch.delenv("SKVM_TRUST_PROXY", raising=False)
    from app import create_app
    proxied_app = create_app()
    proxied_app.config["TESTING"] = True
    c = proxied_app.test_client()
    public_host = "skvm-test-5555.app.github.dev"
    headers = {
        "X-Forwarded-For": "127.0.0.1",
        "X-Forwarded-Host": public_host,
        "X-Forwarded-Proto": "https",
        "Referer": f"https://{public_host}/login",
    }
    page = c.get("/login", base_url="https://localhost:5555", headers=headers)
    assert page.status_code == 200
    import re
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    r = c.post("/login", base_url="https://localhost:5555", headers=headers,
               data={"csrf_token": token, "username": "admin",
                     "password": "TestAdminPass123"}, follow_redirects=False)
    assert r.status_code in (302, 303), r.data[:300]
    assert "/dashboard" in r.headers.get("Location", "")


def test_admin_rbac(client):
    assert client.get("/admin").status_code in (302, 303)  # anon -> login
    _register(client, "bob", "BobPass12345")
    assert client.get("/admin").status_code == 403  # user -> forbidden
    c2 = flask_app.test_client()
    _login(c2, "admin", "TestAdminPass123")
    assert c2.get("/admin").status_code == 200  # admin -> ok


def test_create_vps_without_backend_fails_honestly(client, monkeypatch):
    _register(client, "carol", "CarolPass123")
    monkeypatch.setattr(backend, "resolve_backend",
                        lambda requested=None: (_ for _ in ()).throw(Exception("No provisioning backend is available")))
    with flask_app.app_context():
        uid = db.get_user_by_username("carol").id
        before = db.count_vps(uid)
    token = _csrf(client)
    plan_id = _plan_id()
    r = client.post("/vps/create", data={"csrf_token": token, "name": "carol-1", "plan_id": plan_id,
                                         "os": "ubuntu-22.04", "engine": "container",
                                         "vcpus": 1, "memory": 1024, "disk": 15})
    assert r.status_code in (302, 303)
    with flask_app.app_context():
        assert db.count_vps(db.get_user_by_username("carol").id) == before  # no fake VPS row


def _plan_id(name="Starter"):
    with flask_app.app_context():
        return next(p.id for p in db.list_plans() if p.name == name)


def test_plan_limits_enforced(client, monkeypatch):
    _register(client, "dave", "DavePass1234")
    monkeypatch.setattr(backend, "resolve_backend", lambda requested=None: "docker")
    token = _csrf(client)
    # Starter allows 1 vCPU; asking for 8 must be rejected server-side.
    client.post("/vps/create", data={"csrf_token": token, "name": "dave-big", "plan_id": _plan_id(),
                                     "os": "ubuntu-22.04", "engine": "container",
                                     "vcpus": 8, "memory": 1024, "disk": 15})
    with flask_app.app_context():
        assert db.get_vps_by_name("dave-big") is None


def test_provisioning_pipeline_with_mocked_backend(client, monkeypatch):
    _register(client, "erin", "ErinPass1234")
    monkeypatch.setattr(backend, "resolve_backend", lambda requested=None: "docker")
    token = _csrf(client)
    client.post("/vps/create", data={"csrf_token": token, "name": "erin-1", "plan_id": _plan_id(),
                                     "os": "debian-12", "engine": "container",
                                     "vcpus": 1, "memory": 1024, "disk": 15})
    with flask_app.app_context():
        v = db.get_vps_by_name("erin-1")
        assert v is not None and v.state == "creating"
        job = db.list_jobs_for_vps(v.id)[0]
        monkeypatch.setattr(backend, "create", lambda vv: {"container_id": "fake123", "disk_quota_enforced": True})
        import core.provision as prov
        monkeypatch.setattr(prov, "_guest_prep", lambda vv, pw: None)
        monkeypatch.setattr(backend, "status", lambda vv: {"state": "running", "ip": "172.17.0.2"})
        prov.run_job(job.id)
        v2 = db.get_vps(v.id)
        assert v2.state == "running" and v2.ip_address == "172.17.0.2"
        assert v2.root_password_enc  # encrypted root password captured
        from core.crypto import decrypt_text
        assert decrypt_text(v2.root_password_enc)


def test_admin_resize_updates_allocation(client, monkeypatch):
    _register(client, "frank", "FrankPass123")
    with flask_app.app_context():
        owner = db.get_user_by_username("frank")
        node_id = db.list_nodes()[0].id
        vps_id = db.create_vps(owner.id, node_id, "frank-1", "ubuntu-24.04",
                               "container", 1, 1024, 15, "frank-1",
                               backend="docker", plan_id=_plan_id())
        db.update_vps(vps_id, state="running")

    monkeypatch.setattr(backend, "resize", lambda v, cpu, mem, disk: {})
    admin = flask_app.test_client()
    _login(admin, "admin", "TestAdminPass123")
    page = admin.get("/admin/vps")
    import re
    token = re.search(rb'name="csrf_token" value="([^"]+)"', page.data).group(1).decode()
    r = admin.post(f"/admin/vps/{vps_id}/resources",
                   data={"csrf_token": token, "vcpus": 3, "memory": 3072, "disk": 45})
    assert r.status_code in (302, 303)
    with flask_app.app_context():
        v = db.get_vps(vps_id)
        assert (v.vcpus, v.memory_mb, v.disk_gb) == (3, 3072, 45)


def test_console_requires_auth_and_ownership(client):
    # Anonymous socket must be rejected.
    anon = socketio.test_client(flask_app, namespace="/console",
                                query_string="vps_id=1")
    assert not anon.is_connected("/console")


def test_passwords_are_hashed():
    with flask_app.app_context():
        u = db.get_user_by_username("admin")
        assert u.password_hash != "TestAdminPass123"
        assert u.password_hash.startswith(("scrypt:", "pbkdf2:"))
