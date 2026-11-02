"""Schema: the Alembic migration matches the models, and constraints hold."""
from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from db.models import Account, Base, Categorisation, Document, Statement, Transaction
from db.session import make_engine

ROOT = Path(__file__).resolve().parents[2]


def _alembic_cfg() -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "db" / "migrations"))
    cfg.attributes["configure_logger"] = False
    return cfg


def upgrade_and_diff(engine: Engine) -> list[object]:
    cfg = _alembic_cfg()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return list(compare_metadata(ctx, Base.metadata))


def test_migration_matches_models_on_sqlite(tmp_path: Path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'm.db'}")
    assert upgrade_and_diff(engine) == []
    tables = set(inspect(engine).get_table_names())
    assert {
        "documents", "accounts", "statements", "transactions", "categorisations", "dq_reports",
        "flags", "runs", "stage_executions", "jobs", "analysis_outputs", "report_artifacts",
    } <= tables
    cfg = _alembic_cfg()
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
    engine.dispose()


def _statement(s: Session) -> Statement:
    doc = Document(sha256=uuid.uuid4().hex * 2, filename="a.pdf", size_bytes=1, content=b"x")
    acct = Account(external_account_ref_hash="h" * 64, bank_name="HDFC")
    s.add_all([doc, acct])
    s.flush()
    st = Statement(document_id=doc.id, account_id=acct.id, period_start=dt.date(2024, 4, 1),
                   period_end=dt.date(2024, 4, 30), validation_status="passed", schema_version="v1")
    s.add(st)
    s.flush()
    return st


def _txn(st: Statement, seq: int, **kw: object) -> Transaction:
    base: dict[str, object] = dict(
        statement_id=st.id, txn_seq=seq, source_txn_id=f"t{seq}", txn_date=dt.date(2024, 4, 2),
        description="x", balance=Decimal("10.00"), source_page=1, source_row=seq, row_hash="r" * 64,
        credit=Decimal("1.00"),
    )
    base.update(kw)
    return Transaction(**base)


@pytest.mark.parametrize("kw", [
    {"debit": Decimal("1.00"), "credit": Decimal("1.00")},  # both
    {"debit": None, "credit": None},                         # neither
    {"credit": Decimal("-1.00")},                            # negative
    {"rail": "SWIFT"},                                       # unknown rail
])
def test_transaction_check_constraints(factory: sessionmaker[Session], kw: dict[str, object]) -> None:
    with factory() as s:
        st = _statement(s)
        s.add(_txn(st, 0, **kw))
        with pytest.raises(IntegrityError):
            s.flush()


def test_unique_statement_seq_and_period_check(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        st = _statement(s)
        s.add_all([_txn(st, 0), _txn(st, 0)])
        with pytest.raises(IntegrityError):
            s.flush()
    with factory() as s:
        st = _statement(s)
        st.period_start = dt.date(2024, 5, 1)
        with pytest.raises(IntegrityError):
            s.flush()


def test_unique_sha256(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        s.add_all([
            Document(sha256="a" * 64, filename="a", size_bytes=1, content=b"1"),
            Document(sha256="a" * 64, filename="b", size_bytes=1, content=b"1"),
        ])
        with pytest.raises(IntegrityError):
            s.flush()


def test_only_one_current_categorisation(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        st = _statement(s)
        t = _txn(st, 0)
        s.add(t)
        s.flush()
        s.add(Categorisation(transaction_id=t.id, category="OTHER", source="rule", is_current=False))
        s.add(Categorisation(transaction_id=t.id, category="OTHER", source="rule", is_current=True))
        s.flush()  # history + one current is fine
        s.add(Categorisation(transaction_id=t.id, category="REVENUE", source="manual", is_current=True))
        with pytest.raises(IntegrityError):
            s.flush()


def test_money_round_trips_as_decimal(factory: sessionmaker[Session]) -> None:
    with factory() as s:
        st = _statement(s)
        s.add(_txn(st, 0, credit=Decimal("123456789.12"), balance=Decimal("9999999999.99")))
        s.commit()
    with factory() as s:
        t = s.query(Transaction).one()
        assert t.credit == Decimal("123456789.12") and isinstance(t.credit, Decimal)
        assert t.balance == Decimal("9999999999.99")
