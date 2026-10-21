"""Optional FastAPI web app + JSON API around the pipeline.

    pip install "songforge[server]"
    songforge serve --host 0.0.0.0 --port 8000
"""

from .manager import JobManager, RateLimiter, STYLES

__all__ = ["JobManager", "RateLimiter", "STYLES", "create_app", "ServerConfig"]


def __getattr__(name):
    # keep FastAPI optional: importing songforge.serve must not require it
    if name in ("create_app", "ServerConfig", "app"):
        from . import app as _app
        return getattr(_app, name)
    raise AttributeError(name)
