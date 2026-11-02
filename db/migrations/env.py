"""Alembic environment. URL: ``-x url=...`` > ``DATABASE_URL`` (settings)."""
from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import Connection

from config.settings import get_settings
from db.models import Base
from db.session import make_engine

config = context.config
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    url = context.get_x_argument(as_dictionary=True).get("url") or get_settings().database_url
    if not url:
        raise RuntimeError("Set DATABASE_URL (or pass -x url=...) to run migrations")
    return url


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=connection.dialect.name == "sqlite",
        compare_type=True,
    )


def run_migrations_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # tests pass an open connection
        _configure(connection)
        with context.begin_transaction():
            context.run_migrations()
        return
    engine = make_engine(_url())
    with engine.connect() as conn:
        _configure(conn)
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
