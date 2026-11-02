"""Engine and session factory. The URL comes from ``DATABASE_URL`` (config/settings.py)."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from config.settings import get_settings


class DatabaseNotConfiguredError(RuntimeError):
    """DATABASE_URL is not set."""


def make_engine(url: str | None = None, **kwargs: Any) -> Engine:
    """Create an engine. Postgres in production; SQLite is supported for tests."""
    url = url or get_settings().database_url
    if not url:
        raise DatabaseNotConfiguredError("DATABASE_URL is not set")
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
        if ":memory:" in url or url in ("sqlite://", "sqlite+pysqlite://"):
            kwargs.setdefault("poolclass", StaticPool)
        engine = create_engine(url, connect_args=connect_args, **kwargs)

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

        return engine
    kwargs.setdefault("pool_pre_ping", True)
    return create_engine(url, **kwargs)


def make_sessionmaker(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """One unit of work: commit on success, roll back on any exception."""
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
