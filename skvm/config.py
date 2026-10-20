"""Configuration objects and secret handling for SKVM.

Everything is environment-driven so the panel can be deployed with Docker,
systemd, Kubernetes or plain ``git pull`` without touching code.  A local
``.env`` file is loaded if present (no python-dotenv dependency required).
"""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_IMAGE_DIR = BASE_DIR / "vm-images"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE, ``#`` comments, quoted values)."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _load_dotenv_files() -> None:
    """Read ``.env`` files *before* any configuration value is evaluated.

    The config classes below call the ``_env_*`` helpers in their class bodies,
    which run at import time - so a ``.env`` loaded later (e.g. from
    ``create_app()``) would be ignored for every key except the ones read
    lazily at runtime.  Real environment variables always win: ``_load_dotenv``
    uses ``setdefault``.
    """
    candidates: list[Path] = []
    override = os.environ.get("SKVM_INSTANCE_DIR")
    if override:
        candidates.append(Path(override).expanduser() / ".env")
    candidates += [BASE_DIR / "instance" / ".env", BASE_DIR / ".env", Path.cwd() / ".env"]
    seen: set[str] = set()
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        if path.is_file():
            _load_dotenv(path)


_load_dotenv_files()

# Recomputed after the environment file is in, so SKVM_INSTANCE_DIR can also
# come from ``.env``.
INSTANCE_DIR = Path(os.environ.get("SKVM_INSTANCE_DIR") or (BASE_DIR / "instance"))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


class BaseConfig:
    """Shared configuration for every environment."""

    # -- identity ---------------------------------------------------------- #
    PANEL_NAME = os.environ.get("SKVM_PANEL_NAME", "SKVM")
    PANEL_TAGLINE = os.environ.get("SKVM_PANEL_TAGLINE", "Sovereign Cloud Control")
    PANEL_URL = os.environ.get("SKVM_PANEL_URL", "http://localhost:5000")
    SUPPORT_EMAIL = os.environ.get("SKVM_SUPPORT_EMAIL", "support@example.com")
    BRAND_ACCENT = os.environ.get("SKVM_BRAND_ACCENT", "#7c5cff")

    # -- security ---------------------------------------------------------- #
    SECRET_KEY = os.environ.get("SKVM_SECRET_KEY") or ""
    ENCRYPTION_KEY = os.environ.get("SKVM_ENCRYPTION_KEY") or ""
    SESSION_COOKIE_NAME = "skvm_session"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _env_bool("SKVM_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = _env_int("SKVM_SESSION_LIFETIME", 60 * 60 * 12)  # seconds
    SESSION_REFRESH_EACH_REQUEST = True
    MAX_CONTENT_LENGTH = _env_int("SKVM_MAX_UPLOAD_MB", 512) * 1024 * 1024
    TRUSTED_PROXY_COUNT = _env_int("SKVM_TRUSTED_PROXY_COUNT", 1)
    PASSWORD_MIN_LENGTH = _env_int("SKVM_PASSWORD_MIN_LENGTH", 10)
    LOGIN_MAX_ATTEMPTS = _env_int("SKVM_LOGIN_MAX_ATTEMPTS", 8)
    LOGIN_LOCKOUT_SECONDS = _env_int("SKVM_LOGIN_LOCKOUT_SECONDS", 900)
    CSRF_ENABLED = _env_bool("SKVM_CSRF", True)
    ALLOW_REGISTRATION = _env_bool("SKVM_ALLOW_REGISTRATION", True)
    REQUIRE_EMAIL_VERIFICATION = _env_bool("SKVM_REQUIRE_EMAIL_VERIFICATION", False)
    REQUIRE_ADMIN_APPROVAL = _env_bool("SKVM_REQUIRE_ADMIN_APPROVAL", False)
    BCRYPT_ROUNDS = _env_int("SKVM_BCRYPT_ROUNDS", 12)
    HSTS = _env_bool("SKVM_HSTS", False)
    FORCE_HTTPS = _env_bool("SKVM_FORCE_HTTPS", False)
    ALLOWED_ORIGINS: list[str] = []

    # -- storage ----------------------------------------------------------- #
    DATABASE_URL = os.environ.get("SKVM_DATABASE_URL", "")
    SQLALCHEMY_ECHO = _env_bool("SKVM_SQL_ECHO", False)
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "future": True}

    # -- virtualization backend ------------------------------------------- #
    # auto | docker | demo | node
    BACKEND = os.environ.get("SKVM_BACKEND", "auto").strip().lower()
    DOCKER_HOST = os.environ.get("DOCKER_HOST") or None
    DOCKER_TLS_VERIFY = _env_bool("SKVM_DOCKER_TLS_VERIFY", False)
    DOCKER_CERT_PATH = os.environ.get("SKVM_DOCKER_CERT_PATH") or None
    IMAGE_DIR = Path(os.environ.get("SKVM_IMAGE_DIR", DEFAULT_IMAGE_DIR))

    # -- provisioning defaults -------------------------------------------- #
    DEFAULT_CPU = _env_float("SKVM_DEFAULT_CPU", 2)
    DEFAULT_RAM_MB = _env_int("SKVM_DEFAULT_RAM_MB", 4096)
    DEFAULT_DISK_GB = _env_int("SKVM_DEFAULT_DISK_GB", 50)
    MAX_CPU_PER_SERVER = _env_float("SKVM_MAX_CPU", 32)
    MAX_RAM_PER_SERVER = _env_int("SKVM_MAX_RAM_MB", 131072)
    MAX_DISK_PER_SERVER = _env_int("SKVM_MAX_DISK_GB", 2000)
    DEFAULT_SNAPSHOT_LIMIT = _env_int("SKVM_SNAPSHOT_LIMIT", 5)
    ALLOW_CONSOLE = _env_bool("SKVM_ALLOW_CONSOLE", True)
    CONSOLE_IDLE_TIMEOUT = _env_int("SKVM_CONSOLE_IDLE_TIMEOUT", 900)
    MAX_UPLOAD_FILES = _env_int("SKVM_MAX_UPLOAD_FILES", 25)
    SANDBOX_FS_ROOT = os.environ.get("SKVM_SERVER_ROOT", "/var/lib/skvm/servers")

    # -- platform limits --------------------------------------------------- #
    PER_USER_SERVER_LIMIT = _env_int("SKVM_USER_SERVER_LIMIT", 5)
    PER_USER_CPU_LIMIT = _env_float("SKVM_USER_CPU_LIMIT", 16)
    PER_USER_RAM_LIMIT = _env_int("SKVM_USER_RAM_LIMIT", 32768)
    PER_USER_DISK_LIMIT = _env_int("SKVM_USER_DISK_LIMIT", 500)

    # -- observability ----------------------------------------------------- #
    METRIC_INTERVAL = _env_int("SKVM_METRIC_INTERVAL", 15)      # live poll
    METRIC_RETENTION_DAYS = _env_int("SKVM_METRIC_RETENTION_DAYS", 30)
    ALERT_EVAL_INTERVAL = _env_int("SKVM_ALERT_INTERVAL", 30)
    REAPER_ENABLED = _env_bool("SKVM_REAPER", True)
    ENABLE_PROMETHEUS = _env_bool("SKVM_PROMETHEUS", True)
    PROMETHEUS_TOKEN = os.environ.get("SKVM_PROMETHEUS_TOKEN", "")

    # -- mail -------------------------------------------------------------- #
    SMTP_HOST = os.environ.get("SKVM_SMTP_HOST", "")
    SMTP_PORT = _env_int("SKVM_SMTP_PORT", 587)
    SMTP_USER = os.environ.get("SKVM_SMTP_USER", "")
    SMTP_PASSWORD = os.environ.get("SKVM_SMTP_PASSWORD", "")
    SMTP_TLS = _env_bool("SKVM_SMTP_TLS", True)
    SMTP_FROM = os.environ.get("SKVM_SMTP_FROM", "SKVM <no-reply@example.com>")
    MAIL_MODE = os.environ.get("SKVM_MAIL_MODE", "log")  # log | smtp | console

    # -- rate limiting ----------------------------------------------------- #
    RATELIMIT_STORAGE_URI = os.environ.get("SKVM_RATELIMIT_STORAGE", "memory://")
    RATELIMIT_HEADERS_ENABLED = True
    RATELIMIT_DEFAULT = os.environ.get("SKVM_RATELIMIT_DEFAULT", "600 per minute")

    # -- misc -------------------------------------------------------------- #
    TIMEZONE = os.environ.get("SKVM_TIMEZONE", "UTC")
    DEMO_MODE = _env_bool("SKVM_DEMO", False)
    JSON_SORT_KEYS = False
    TEMPLATES_AUTO_RELOAD = _env_bool("SKVM_TEMPLATE_RELOAD", False)


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    ENV_NAME = "development"
    TEMPLATES_AUTO_RELOAD = True


class TestingConfig(BaseConfig):
    TESTING = True
    ENV_NAME = "testing"
    CSRF_ENABLED = False
    RATELIMIT_ENABLED = False
    BACKEND = "demo"
    WTF_CSRF_ENABLED = False


class ProductionConfig(BaseConfig):
    DEBUG = False
    ENV_NAME = "production"
    SESSION_COOKIE_SECURE = _env_bool("SKVM_COOKIE_SECURE", True)


CONFIG_MAP = {
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
    "default": ProductionConfig,
}


# --------------------------------------------------------------------------- #
# Secret material
# --------------------------------------------------------------------------- #
def ensure_instances(instance_dir: Path = INSTANCE_DIR) -> Path:
    instance_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(instance_dir, stat.S_IRWXU)
    except OSError:
        pass
    return instance_dir


def _secret_store(path: Path, length: int = 48) -> str:
    """Return a persistent random secret, generating it on first use."""
    if path.is_file():
        data = path.read_text(encoding="utf-8").strip()
        if data:
            return data
    path.parent.mkdir(parents=True, exist_ok=True)
    value = secrets.token_urlsafe(length)
    path.write_text(value, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return value


def resolve_secret_key(instance_dir: Path = INSTANCE_DIR) -> str:
    env = os.environ.get("SKVM_SECRET_KEY")
    if env:
        return env
    return _secret_store(ensure_instances(instance_dir) / "secret_key")


def resolve_encryption_key(instance_dir: Path = INSTANCE_DIR) -> str:
    """Fernet key protecting VPS root passwords and node API keys at rest."""
    env = os.environ.get("SKVM_ENCRYPTION_KEY")
    if env:
        return env
    path = ensure_instances(instance_dir) / "encryption_key"
    if path.is_file() and path.read_text(encoding="utf-8").strip():
        return path.read_text(encoding="utf-8").strip()
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    path.write_text(key, encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return key


def resolve_database_url(instance_dir: Path = INSTANCE_DIR) -> str:
    url = os.environ.get("SKVM_DATABASE_URL") or BaseConfig.DATABASE_URL
    if url:
        return url
    ensure_instances(instance_dir)
    return f"sqlite:///{instance_dir / 'skvm.db'}"
