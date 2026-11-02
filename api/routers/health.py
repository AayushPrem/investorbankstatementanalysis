"""Liveness and readiness (no auth)."""
from __future__ import annotations

from fastapi import APIRouter
from sqlalchemy import text

from api.deps import FactoryDep
from api.errors import APIError

router = APIRouter(tags=["health"])


@router.get("/health")
def health() -> dict[str, str]:
    """The process is up."""
    return {"status": "ok"}


@router.get("/ready")
def ready(factory: FactoryDep) -> dict[str, str]:
    """The database is reachable."""
    try:
        with factory() as s:
            s.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        raise APIError(503, "not_ready", "Database unavailable") from exc
    return {"status": "ready"}
