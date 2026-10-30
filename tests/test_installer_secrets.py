"""Installers auto-generate SECRET_KEY (idempotently) and apply the
documented first-login admin defaults."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_bash_syntax():
    for name in ("install.sh", "run.sh", "setup.sh"):
        subprocess.run(["bash", "-n", str(ROOT / name)], check=True)


def test_app_default_admin_credentials():
    s = (ROOT / "core" / "config.py").read_text()
    assert 'DEFAULT_ADMIN_USERNAME = "admin"' in s
    assert 'DEFAULT_ADMIN_PASSWORD = "admin123"' in s
    app = (ROOT / "app.py").read_text()
    assert "DEFAULT_ADMIN_PASSWORD" in app
    assert "default_pw_warning" in app


def test_installer_generates_secret_and_uses_default_admin():
    s = (ROOT / "install.sh").read_text()
    assert "secrets.token_urlsafe(48)" in s          # SECRET_KEY generation
    assert "secrets.token_urlsafe(18)" not in s      # admin PW is no longer random
    assert "SECRET_KEY=%s" in s
    assert 'ADMIN_PASS_VALUE="admin123"' in s
    assert 'env_val SECRET_KEY' in s                 # reinstall keeps old key
    assert "FRESH_DB" in s                           # reinstall keeps old admin
    assert "chmod 0640" in s                         # env file locked down
    assert "change it after first login" in s.lower()


def test_runsh_autocreates_env():
    s = (ROOT / "run.sh").read_text()
    assert "auto-create .env" in s
    assert "SECRET_KEY=%s" in s and "SKVM_ADMIN_PASSWORD=%s" in s
    assert "${SKVM_ADMIN_PASSWORD:-admin123}" in s
    assert "chmod 600 .env" in s


def test_env_example_documents_defaults():
    s = (ROOT / ".env.example").read_text()
    assert "SKVM_ADMIN_USERNAME=admin" in s
    assert "SKVM_ADMIN_PASSWORD=admin123" in s
    assert "auto-creates" in s or "auto-generate" in s


def test_new_ui_identity():
    base = (ROOT / "templates" / "base.html").read_text()
    css = (ROOT / "static" / "css" / "skvm.css").read_text()
    assert "Overview" in base
    assert "Launch instance" in base
    assert "default_pw_warning" in base
    assert "NEBULA GLASS" in base
    assert "Sora" in base
    assert "#38bdf8" in css      # electric blue
    assert "#a855f7" in css      # violet accent
    assert "backdrop-filter:blur" in css
    assert "#b8f135" not in css
    assert "#5e6ad2" not in css
    assert "#67e8f9" not in css  # previous Aura identity fully retired


def test_ui_components_and_no_fake_nav():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    base = (root / "templates" / "base.html").read_text()
    dash = (root / "templates" / "dashboard.html").read_text()
    js = (root / "static" / "js" / "skvm.js").read_text()
    # Reusable components exist: toasts, confirm modal, skeletons, sparklines.
    assert "toastStack" in base and "modalRoot" in base
    assert "data-confirm" in dash and "drawSpark" in js
    assert "fleetSearch" in dash and "skeleton" in dash
    # No fake billing nav (no billing backend exists), no inline confirm().
    assert "Billing" not in base
    for tpl in (root / "templates").glob("*.html"):
        assert "return confirm(" not in tpl.read_text(), tpl.name
