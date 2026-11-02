"""Shared FastAPI dependencies: settings, DB sessions, API-key auth."""
from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session, sessionmaker

from api.errors import APIError
from config.settings import Settings


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def get_factory(request: Request) -> sessionmaker[Session]:
    factory: sessionmaker[Session] | None = request.app.state.session_factory
    if factory is None:
        from db.session import make_engine, make_sessionmaker

        factory = make_sessionmaker(make_engine(request.app.state.settings.database_url))
        request.app.state.session_factory = factory
    return factory


def get_session(factory: Annotated[sessionmaker[Session], Depends(get_factory)]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def require_api_key(
    settings: Annotated[Settings, Depends(get_settings_dep)],
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """``X-API-Key`` must hash to ``API_KEY_HASH``. Fails closed if no hash is configured."""
    expected = settings.api_key_hash.get_secret_value().strip().lower() if settings.api_key_hash else ""
    if not expected:
        raise APIError(503, "auth_not_configured", "API key auth is not configured on this server")
    if not x_api_key or not hmac.compare_digest(hash_api_key(x_api_key), expected):
        raise APIError(401, "unauthorized", "Missing or invalid X-API-Key")


SessionDep = Annotated[Session, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
FactoryDep = Annotated[sessionmaker[Session], Depends(get_factory)]
