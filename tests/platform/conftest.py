"""Fixtures for the data-platform tests (db/, api/, worker/).

Default backend: SQLite in memory. Tests marked ``postgres`` need
``BSAA_TEST_DATABASE_URL`` (a throwaway Postgres 16 database). CI provides
one; locally they're skipped.

The whole package is skipped if the ``[platform]`` extra isn't installed.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

pytest.importorskip("sqlalchemy")
pytest.importorskip("fastapi")
pytest.importorskip("alembic")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import Engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from api.deps import hash_api_key  # noqa: E402
from api.main import create_app  # noqa: E402
from config.settings import Settings  # noqa: E402
from db.models import Base  # noqa: E402
from db.session import make_engine, make_sessionmaker  # noqa: E402

API_KEY = "test-key-not-secret"
GOLD_DIR = Path(__file__).resolve().parents[2] / "data" / "gold"

PG_URL = os.environ.get("BSAA_TEST_DATABASE_URL")

# One header + rows in the format tests/test_csv_adapter.py already uses.
CSV_HEADER = "Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance"


def make_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "api_key_hash": hash_api_key(API_KEY),
        "job_backoff_s": 0.0,
        "job_lease_s": 300.0,
        "job_max_attempts": 3,
        "llm_enabled": False,
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[call-arg, arg-type]


def csv_statement(n: int = 30, *, break_at: int | None = None, break_by: str = "500.00",
                  start_balance: int = 100000) -> bytes:
    """A valid running-balance CSV of *n* rows. ``break_at`` (0-based) shifts
    that row's balance by ``break_by`` without changing its amount."""
    from decimal import Decimal

    lines = [CSV_HEADER]
    bal = Decimal(start_balance)
    for i in range(n):
        day = 1 + (i % 28)
        if i % 3 == 0:
            amt = Decimal(5000 + i * 10)
            bal += amt
            debit, credit = "", f"{amt:.2f}"
            desc = f"NEFT CR-ACME PVT LTD-INV-{i}"
        else:
            amt = Decimal(1200 + i * 7)
            bal -= amt
            debit, credit = f"{amt:.2f}", ""
            desc = f"UPI-VENDOR PAYMENT-V{i}"
        shown = bal + (Decimal(break_by) if break_at is not None and i == break_at else 0)
        lines.append(f"{day:02d}/04/24,{desc},REF{i:04d},{debit},{credit},{shown:.2f}")
    return ("\n".join(lines) + "\n").encode("utf-8")


@pytest.fixture
def settings() -> Settings:
    return make_settings()


@pytest.fixture
def engine() -> Iterator[Engine]:
    eng = make_engine("sqlite://")
    Base.metadata.create_all(eng)
    yield eng
    eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(engine)


@pytest.fixture
def client(factory: sessionmaker[Session], settings: Settings) -> TestClient:
    return TestClient(create_app(settings=settings, session_factory=factory))


@pytest.fixture
def headers() -> dict[str, str]:
    return {"X-API-Key": API_KEY}


@pytest.fixture(scope="session")
def gold_pdf() -> tuple[str, bytes]:
    """The smallest gold-set PDF (built by tests/conftest.py if missing)."""
    pdfs = sorted(GOLD_DIR.glob("*.pdf"), key=lambda p: p.stat().st_size)
    if not pdfs:
        pytest.skip("gold set not built")
    return pdfs[0].name, pdfs[0].read_bytes()
