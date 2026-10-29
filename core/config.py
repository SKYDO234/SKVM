"""SKVM PANEL configuration.

All secrets come from environment variables / .env (python-dotenv) or a
0600 secret file. Nothing secret is hard-coded. Configuration uses the
SKVM_* namespace only.
"""
from __future__ import annotations
import os
import secrets
from pathlib import Path

try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:
    pass

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(*names: str, default: str = "") -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and v != "":
            return v
    return default


DATA_DIR = Path(_env("SKVM_DATA_DIR", default=str(BASE_DIR / "data")))
DB_PATH = Path(_env("SKVM_DB", default=str(DATA_DIR / "skvm.db")))
SECRET_FILE = Path(_env("SKVM_SECRET_FILE", default=str(DATA_DIR / ".secret_key")))
NODE_TOKEN_FILE = Path(_env("SKVM_NODE_TOKEN_FILE", default=str(DATA_DIR / ".node_token")))

HOSTNAME = _env("SKVM_HOSTNAME", default="skvm-node-1")
BIND = _env("SKVM_BIND", default="0.0.0.0")
PORT = int(_env("SKVM_PORT", default="5555"))
NODE_BIND = _env("SKVM_NODE_BIND", default="127.0.0.1")
NODE_PORT = int(_env("SKVM_NODE_PORT", default="5556"))
NODE_NAME = _env("SKVM_NODE_NAME", default=HOSTNAME)
PANEL_URL = _env("SKVM_PANEL_URL", default=f"http://127.0.0.1:{PORT}")

# Backend: auto | incus | docker
BACKEND = _env("SKVM_BACKEND", "SKVM_VPS_ENGINE", default="auto").lower()
if BACKEND not in ("auto", "incus", "docker"):
    BACKEND = "auto"

DEFAULT_VCPUS = int(_env("SKVM_DEFAULT_VCPUS", default="2"))
DEFAULT_MEMORY_MB = int(_env("SKVM_DEFAULT_MEMORY_MB", default="2048"))
DEFAULT_DISK_GB = int(_env("SKVM_DEFAULT_DISK_GB", default="25"))
MAX_VPS_PER_USER = int(_env("SKVM_MAX_VPS_PER_USER", default="1"))
AUTO_UPDATE = _env("SKVM_AUTO_UPDATE", default="true").lower() not in {"0", "false", "no"}
TIMEZONE = _env("SKVM_TIMEZONE", default="UTC")
ALLOW_REGISTRATION = _env("SKVM_ALLOW_REGISTRATION", default="true").lower() not in {"0", "false", "no"}
COOKIE_SECURE = _env("SKVM_COOKIE_SECURE", default="false").lower() == "true"

# Forwarded-header trust (X-Forwarded-Host/-Proto/-For). Explicit
# SKVM_TRUST_PROXY=true/false always wins. When it is unset, trust is
# enabled automatically in hosted preview environments (GitHub
# Codespaces, Gitpod, VS Code dev containers): those proxies rewrite
# Host to localhost:<port> and send the public *.app.github.dev host in
# X-Forwarded-Host, so without ProxyFix Flask-WTF's CSRF referrer check
# rejects every login POST with "The referrer does not match the host."
_TRUTHY = {"1", "true", "yes", "on"}
_FALSY = {"0", "false", "no", "off"}
PROXY_ENV_MARKERS = (
    "CODESPACES",
    "GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN",
    "GITPOD_WORKSPACE_ID",
    "GITPOD_WORKSPACE_URL",
    "REMOTE_CONTAINERS",
    "CODESANDBOX_HOST",
    "SKVM_CONTAINER",
)


def trust_proxy_enabled() -> bool:
    raw = os.getenv("SKVM_TRUST_PROXY")
    if raw is not None and raw.strip() != "":
        value = raw.strip().lower()
        if value in _TRUTHY:
            return True
        if value in _FALSY:
            return False
    return any((os.getenv(name) or "").strip() for name in PROXY_ENV_MARKERS)


TRUST_PROXY = trust_proxy_enabled()

# First-run default admin (only used when no users exist yet and no
# SKVM_ADMIN_USERNAME / SKVM_ADMIN_PASSWORD override is supplied).
# Change this password immediately after your first login.
DEFAULT_ADMIN_USERNAME = "admin"
DEFAULT_ADMIN_PASSWORD = "admin123"


def _ensure_secret(path: Path) -> str:
    env_secret = os.getenv("SECRET_KEY") or os.getenv("SKVM_SECRET_KEY")
    if env_secret:
        return env_secret
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_urlsafe(48)
        path.write_text(value + "\n", encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except PermissionError:
            pass
        return value
    except Exception:
        # Last resort for read-only filesystems (e.g. ephemeral dev): still
        # never fall back to a known/default secret.
        return secrets.token_urlsafe(48)


SECRET_KEY = _ensure_secret(SECRET_FILE)


def node_token() -> str:
    env_token = os.getenv("SKVM_NODE_TOKEN")
    if env_token:
        return env_token
    return _ensure_secret(NODE_TOKEN_FILE)
