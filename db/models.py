"""SQLAlchemy 2.0 models for the data platform (UPGRADE_GUIDE §8.1, ADR-0001).

Portable by construction: ``Uuid``, ``Numeric(18,2)`` and ``JSON`` with a
``JSONB`` variant on PostgreSQL, so unit/API tests can run on SQLite while
production runs on Postgres 16. ``db/migrations/versions/0001_initial.py``
must match this metadata. ``tests/platform/test_models_migration.py``
asserts that.

Money is ``NUMERIC(18,2)`` and stays ``Decimal`` in Python. Never float.
"""
from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, deferred, mapped_column, relationship

# Deterministic constraint names, so Alembic migrations are reviewable and stable.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

JSONType = JSON().with_variant(JSONB(), "postgresql")
# Flag → transaction uuids. uuid[] on Postgres, a JSON list of strings elsewhere.
UuidList = JSON().with_variant(ARRAY(Uuid(as_uuid=False)), "postgresql")
Money = Numeric(18, 2, asdecimal=True)

# Status vocabularies (kept as plain strings + CHECKs; no PG ENUM types to migrate).
JOB_STATUSES = ("queued", "running", "succeeded", "failed")
RUN_STATUSES = ("running", "succeeded", "failed")
DOCUMENT_STATUSES = ("received", "processing", "processed", "failed")
CATEGORY_SOURCES = ("rule", "ml", "llm", "review", "default", "manual")  # review: Phase 4, migration 0002
RAILS = ("UPI", "NEFT", "IMPS", "RTGS", "CHQ", "CASH", "CARD", "OTHER")
OUTPUT_KINDS = (
    "metrics", "customer_analytics", "financial_health", "compliance",
    "reconciliation", "narrative", "categorisation_stats", "validation",
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def _in(col: str, values: tuple[str, ...]) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        UniqueConstraint("sha256"),  # idempotency key
        CheckConstraint(_in("status", DOCUMENT_STATUSES), name="status"),
        CheckConstraint("size_bytes >= 0", name="size_nonneg"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime: Mapped[str | None] = mapped_column(String(100))
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_format: Mapped[str | None] = mapped_column(String(20))
    bank_name: Mapped[str | None] = mapped_column(String(50))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="received", nullable=False)
    # The original bytes, so the worker (another process) can run the pipeline.
    # Deferred: never loaded unless asked for.
    content: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))

    jobs: Mapped[list[Job]] = relationship(back_populates="document")


class Account(Base):
    __tablename__ = "accounts"
    __table_args__ = (UniqueConstraint("external_account_ref_hash", "bank_name"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # sha256 of the bank's account number/ref, never the raw value
    external_account_ref_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    bank_name: Mapped[str] = mapped_column(String(50), nullable=False)
    company_name: Mapped[str | None] = mapped_column(String(255))


class Statement(Base):
    __tablename__ = "statements"
    __table_args__ = (
        UniqueConstraint("document_id"),  # one statement per uploaded document
        CheckConstraint("period_start <= period_end", name="period_order"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    account_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(20), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(20), nullable=False)
    source_format: Mapped[str | None] = mapped_column(String(20))
    opening_balance: Mapped[Decimal | None] = mapped_column(Money)

    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="statement", cascade="all, delete-orphan", order_by="Transaction.txn_seq"
    )


class Transaction(Base):
    __tablename__ = "transactions"
    __table_args__ = (
        CheckConstraint("(debit IS NULL) <> (credit IS NULL)", name="exactly_one_of_debit_credit"),
        CheckConstraint("debit IS NULL OR debit >= 0", name="debit_nonneg"),
        CheckConstraint("credit IS NULL OR credit >= 0", name="credit_nonneg"),
        CheckConstraint(_in("rail", RAILS), name="rail"),
        UniqueConstraint("statement_id", "txn_seq"),
        Index("ix_transactions_statement_id_txn_date", "statement_id", "txn_date"),
        Index("ix_transactions_customer_id", "customer_id"),
        Index("ix_transactions_source_txn_id", "source_txn_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), nullable=False
    )
    txn_seq: Mapped[int] = mapped_column(Integer, nullable=False)
    # The pipeline's deterministic id (CanonicalTransaction.transaction_id)
    source_txn_id: Mapped[str] = mapped_column(String(64), nullable=False)
    txn_date: Mapped[date] = mapped_column(Date, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    debit: Mapped[Decimal | None] = mapped_column(Money)
    credit: Mapped[Decimal | None] = mapped_column(Money)
    balance: Mapped[Decimal] = mapped_column(Money, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), default="INR", nullable=False)
    rail: Mapped[str] = mapped_column(String(8), default="OTHER", nullable=False)
    counterparty: Mapped[str | None] = mapped_column(String(255))
    customer_id: Mapped[str | None] = mapped_column(String(128))
    is_related_party: Mapped[bool | None] = mapped_column(Boolean)
    source_page: Mapped[int] = mapped_column(Integer, nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    row_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    statement: Mapped[Statement] = relationship(back_populates="transactions")
    categorisations: Mapped[list[Categorisation]] = relationship(
        back_populates="transaction", cascade="all, delete-orphan"
    )


class Categorisation(Base):
    """Category history per transaction; exactly one row has ``is_current``."""

    __tablename__ = "categorisations"
    __table_args__ = (
        CheckConstraint(_in("source", CATEGORY_SOURCES), name="source"),
        CheckConstraint("confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence"),
        Index(
            "uq_categorisations_current", "transaction_id", unique=True,
            postgresql_where=text("is_current"), sqlite_where=text("is_current"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    transaction_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("transactions.id", ondelete="CASCADE"), nullable=False
    )
    category: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[str] = mapped_column(String(10), nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    model_version: Mapped[str | None] = mapped_column(String(40))
    rule_id: Mapped[str | None] = mapped_column(String(40))
    is_current: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    transaction: Mapped[Transaction] = relationship(back_populates="categorisations")


class DQReportRow(Base):
    __tablename__ = "dq_reports"
    __table_args__ = (UniqueConstraint("statement_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    statement_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("statements.id", ondelete="CASCADE"), nullable=False
    )
    checks: Mapped[list[dict[str, Any]]] = mapped_column(JSONType, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Run(Base):
    """One pipeline execution. ``id`` is the pipeline's run_id (config.logging)."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(_in("status", RUN_STATUSES), name="status"),
        Index("ix_runs_document_id", "document_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.id", ondelete="SET NULL"))
    statement_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("statements.id", ondelete="SET NULL")
    )
    agent_versions: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    model_versions: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error: Mapped[str | None] = mapped_column(String(200))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StageExecution(Base):
    """The "agent execution log": one row per stage per run."""

    __tablename__ = "stage_executions"
    __table_args__ = (Index("ix_stage_executions_run_id", "run_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    stage: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    error: Mapped[str | None] = mapped_column(String(200))  # exception type only
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Job(Base):
    """Postgres-backed queue row (ADR-0002)."""

    __tablename__ = "jobs"
    __table_args__ = (
        CheckConstraint(_in("status", JOB_STATUSES), name="status"),
        CheckConstraint("attempts >= 0", name="attempts_nonneg"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), default="queued", nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[str | None] = mapped_column(String(36))  # the run that produced the result
    # Upload options for the pipeline: company_name, affiliates, claims (JSON-safe).
    options: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)

    document: Mapped[Document] = relationship(back_populates="jobs")


class Flag(Base):
    __tablename__ = "flags"
    __table_args__ = (
        CheckConstraint("kind IN ('rule', 'ml')", name="kind"),
        Index("ix_flags_run_id_severity", "run_id", "severity"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    detector: Mapped[str] = mapped_column(String(60), nullable=False)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    severity: Mapped[str] = mapped_column(String(10), nullable=False)
    score: Mapped[float | None] = mapped_column(Float)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    transaction_ids: Mapped[list[str]] = mapped_column(UuidList, nullable=False)


class AnalysisOutput(Base):
    __tablename__ = "analysis_outputs"
    __table_args__ = (
        UniqueConstraint("run_id", "kind"),
        CheckConstraint(_in("kind", OUTPUT_KINDS), name="kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload: Mapped[Any] = mapped_column(JSONType, nullable=False)


class ReportArtifact(Base):
    """Rendered lens reports (bytes). Not in §8.1; see CHANGELOG for why it exists."""

    __tablename__ = "report_artifacts"
    __table_args__ = (UniqueConstraint("run_id", "lens", "fmt"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    lens: Mapped[str] = mapped_column(String(20), nullable=False)
    fmt: Mapped[str] = mapped_column(String(10), nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
