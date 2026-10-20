"""Lightweight SQLAlchemy 2.0 integration for Flask.

We intentionally avoid a Flask-SQLAlchemy dependency: the panel only needs an
engine, a request-scoped session and a couple of helpers, and keeping that here
makes the data layer transparent and easy to test.
"""

from __future__ import annotations

import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from flask import current_app, g, has_app_context
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

log = logging.getLogger("skvm.db")

UTC = timezone.utc


def utcnow() -> datetime:
    """Timezone-aware UTC now — the only clock the panel uses."""
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    """ISO-8601 with an explicit ``Z``; naive values are assumed UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_dt(raw: Any) -> datetime | None:
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
    try:
        text_value = str(raw).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text_value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def human_delta(value: datetime | str | None) -> str:
    """``"3h 12m"`` style relative rendering used by the front-end.

    Templates sometimes hold what ``public()`` serialised - an already formatted
    string - rather than a datetime. Saying that back is better than a 500 on an
    admin page, so non-datetime input is passed through instead of raising.
    """
    if value is None:
        return "never"
    if isinstance(value, str):
        return value or "never"
    if not isinstance(value, datetime):
        return "unknown"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = utcnow() - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        seconds = -seconds
        suffix = " from now"
    else:
        suffix = " ago"
    for limit, unit, div in (
        (60, "s", 1),
        (3600, "m", 60),
        (86400, "h", 3600),
        (2592000, "d", 86400),
        (31536000, "mo", 2592000),
    ):
        if seconds < limit:
            return f"{seconds // div}{unit}{suffix}" if unit == "s" else f"{seconds // div}{unit}{suffix}"
    return f"{seconds // 31536000}y{suffix}"


class Base(DeclarativeBase):
    """Declarative base shared by every model."""

    def to_dict(self, exclude: tuple[str, ...] = ()) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for column in inspect(self).mapper.column_attrs:
            key = column.key
            if key in exclude or key.startswith("_"):
                continue
            value = getattr(self, key)
            out[key] = iso(value) if isinstance(value, datetime) else value
        return out

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        ident = getattr(self, "id", None)
        return f"<{type(self).__name__} id={ident}>"


class Database:
    """Owns the engine + session factory for one application instance."""

    def __init__(self) -> None:
        self.engine: Engine | None = None
        self._factory: sessionmaker[Session] | None = None

    # -- lifecycle ------------------------------------------------------- #
    def init_app(self, app) -> None:
        url = app.config["SQLALCHEMY_DATABASE_URL"]
        options: dict[str, Any] = {
            "echo": app.config.get("SQLALCHEMY_ECHO", False),
            "future": True,
            "pool_pre_ping": True,
        }
        if url.startswith("sqlite"):
            options["connect_args"] = {"check_same_thread": False, "timeout": 30}
        else:
            options.update(
                pool_size=int(app.config.get("SQL_POOL_SIZE", 10)),
                max_overflow=int(app.config.get("SQL_MAX_OVERFLOW", 20)),
                pool_recycle=1800,
            )
        self.engine = create_engine(url, **options)
        if url.startswith("sqlite"):
            _install_sqlite_pragmas(self.engine)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)
        app.extensions["skvm.db"] = self
        app.teardown_appcontext(self._teardown)

    def _teardown(self, exc: BaseException | None = None) -> None:
        session = g.pop("_skvm_session", None)
        if session is None:
            return
        if exc is None:
            session.close()
        else:  # pragma: no cover - defensive
            session.rollback()
            session.close()

    # -- sessions -------------------------------------------------------- #
    @property
    def session(self) -> Session:
        """Request-scoped session (also usable outside a request via g)."""
        if not has_app_context():
            raise RuntimeError("db.session requires an application context")
        session = getattr(g, "_skvm_session", None)
        if session is None:
            if self._factory is None:
                raise RuntimeError("Database.init_app() was never called")
            session = self._factory()
            g._skvm_session = session
        return session

    def new_session(self) -> Session:
        """Detached session for background workers / threads."""
        if self._factory is None:
            raise RuntimeError("Database.init_app() was never called")
        return self._factory()

    @contextlib.contextmanager
    def tx(self) -> Iterator[Session]:
        """Transaction scope that commits on success and rolls back on error."""
        session = self.session
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise

    @contextlib.contextmanager
    def worker_tx(self) -> Iterator[Session]:
        """Same as :meth:`tx` but for background threads."""
        session = self.new_session()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # -- schema ---------------------------------------------------------- #
    def create_all(self) -> None:
        from . import models  # noqa: F401  (ensures models are imported)

        assert self.engine is not None
        Base.metadata.create_all(self.engine)

    def drop_all(self) -> None:
        assert self.engine is not None
        Base.metadata.drop_all(self.engine)

    def health(self) -> tuple[bool, str]:
        try:
            with self.engine.connect() as conn:  # type: ignore[union-attr]
                conn.execute(text("SELECT 1"))
            return True, "ok"
        except Exception as exc:  # pragma: no cover - depends on environment
            return False, str(exc)


def _install_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record):  # pragma: no cover - driver hook
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


db = Database()


def purge_old_rows(model, column: str, days: int) -> int:
    """Delete rows older than ``days`` — used by the retention reaper."""
    cutoff = utcnow() - timedelta(days=days)
    col = getattr(model, column)
    with db.worker_tx() as session:
        deleted = session.query(model).filter(col < cutoff).delete(synchronize_session=False)
    return int(deleted or 0)
