"""Postgres-only behaviour: migration on a real server, SKIP LOCKED, JSONB/uuid[]/NUMERIC.

Needs BSAA_TEST_DATABASE_URL pointing at a throwaway database (CI provides a
postgres:16 service). Every table is dropped at the end of each test.
"""
from __future__ import annotations

import threading
from collections.abc import Iterator
from decimal import Decimal

import pytest
from alembic import command
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from db.ingest import ingest_document
from db.models import Flag, Job, Transaction
from db.repositories.jobs import JobQueue
from db.session import make_engine, make_sessionmaker
from tests.platform.conftest import PG_URL, csv_statement, make_settings
from tests.platform.test_models_migration import _alembic_cfg, upgrade_and_diff
from worker.runner import Worker

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(not PG_URL, reason="BSAA_TEST_DATABASE_URL not set (Postgres tests)"),
]


def _reset(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


@pytest.fixture
def pg_engine() -> Iterator[Engine]:
    assert PG_URL
    engine = make_engine(PG_URL)
    _reset(engine)
    yield engine
    _reset(engine)
    engine.dispose()


@pytest.fixture
def pg_factory(pg_engine: Engine) -> sessionmaker[Session]:
    assert upgrade_and_diff(pg_engine) == []  # also: migration == models on Postgres
    return make_sessionmaker(pg_engine)


def test_migration_round_trip_on_postgres(pg_engine: Engine) -> None:
    assert upgrade_and_diff(pg_engine) == []
    with pg_engine.connect() as conn:
        types = dict(conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name IN ('transactions', 'flags', 'analysis_outputs')"
        )).all())
    assert types["balance"] == "numeric"
    assert types["transaction_ids"] == "ARRAY"
    assert types["payload"] == "jsonb"
    cfg = _alembic_cfg()
    with pg_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")


def test_skip_locked_never_double_claims(pg_factory: sessionmaker[Session]) -> None:
    for i in range(20):
        ingest_document(pg_factory, f"doc {i}".encode(), f"{i}.csv")
    queue = JobQueue(pg_factory, lease_s=300, backoff_s=0)
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def grab(n: int) -> None:
        barrier.wait()
        while (job := queue.claim(f"w{n}")) is not None:
            with lock:
                claimed.append(str(job.id))

    threads = [threading.Thread(target=grab, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(claimed) == 20
    assert len(set(claimed)) == 20  # no job claimed twice
    with pg_factory() as s:
        assert all(j.attempts == 1 for j in s.scalars(select(Job)))


def test_locked_row_is_skipped_not_waited_on(pg_factory: sessionmaker[Session]) -> None:
    ingest_document(pg_factory, b"only one", "a.csv")
    queue = JobQueue(pg_factory, lease_s=300, backoff_s=0)
    holder = pg_factory()
    try:
        holder.execute(select(Job).with_for_update())  # another tx holds the row lock
        assert queue.claim("w") is None  # returns immediately instead of blocking
    finally:
        holder.rollback()
        holder.close()
    assert queue.claim("w") is not None


def test_end_to_end_on_postgres(pg_factory: sessionmaker[Session],
                                gold_pdf: tuple[str, bytes]) -> None:
    settings = make_settings()
    name, data = gold_pdf
    res = ingest_document(pg_factory, data, name)
    assert ingest_document(pg_factory, data, name).duplicate is True
    Worker(pg_factory, settings=settings).run_once()
    with pg_factory() as s:
        job = s.get(Job, res.job_id)
        assert job is not None and job.status == "succeeded"
        bal = s.scalar(select(Transaction.balance).limit(1))
        assert isinstance(bal, Decimal)
        for f in s.scalars(select(Flag)):
            assert isinstance(f.transaction_ids, list)


def test_validation_failure_on_postgres(pg_factory: sessionmaker[Session]) -> None:
    res = ingest_document(pg_factory, csv_statement(30, break_at=10), "broken.csv")
    Worker(pg_factory, settings=make_settings(), render=None).run_once()
    with pg_factory() as s:
        job = s.get(Job, res.job_id)
        assert job is not None and job.status == "failed"
        assert job.last_error == "ValidationFailed: BALANCE_BREAK"
