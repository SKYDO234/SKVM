"""Installer/launcher must auto-generate SECRET_KEY + admin credentials
(idempotently) so users never invent or hand-edit secrets."""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_bash_syntax():
    for name in ("install.sh", "run.sh", "setup.sh"):
        subprocess.run(["bash", "-n", str(ROOT / name)], check=True)


def test_installer_generates_and_preserves_secrets():
    s = (ROOT / "install.sh").read_text()
    assert "secrets.token_urlsafe(48)" in s          # SECRET_KEY generation
    assert "secrets.token_urlsafe(18)" in s          # admin password generation
    assert "SECRET_KEY=%s" in s and "SKVM_ADMIN_PASSWORD=%s" in s
    assert 'env_val SECRET_KEY' in s                 # reinstall keeps old key
    assert "FRESH_DB" in s                           # reinstall keeps old admin
    assert "chmod 0640" in s                         # env file locked down


def test_runsh_autocreates_env():
    s = (ROOT / "run.sh").read_text()
    assert "auto-create .env" in s
    assert "SECRET_KEY=%s" in s and "SKVM_ADMIN_PASSWORD=%s" in s
    assert "chmod 600 .env" in s


def test_env_example_documents_autogeneration():
    s = (ROOT / ".env.example").read_text()
    assert "auto-creates" in s or "auto-generate" in s
