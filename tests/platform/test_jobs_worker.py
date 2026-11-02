"""Job queue + worker (§8.4): success, validation failure, retries, lease reclaim, atomic writes."""
from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from config.settings import Settings
from db.ingest import ingest_document
from db.models import (
    AnalysisOutput,
    Categorisation,
    Document,
    DQReportRow,
    Flag,
    Job,
    ReportArtifact,
    Run,
    StageExecution,
    Statement,
    Transaction,
    utcnow,
)
from db.repositories.results import start_run
from pipeline.run import run_analysis
from tests.platform.conftest import csv_statement, make_settings
from worker.runner import Worker

ALL_STAGES = [
    "ingest", "normalise", "validate", "categorise", "financial_analysis", "related_party",
    "customer_identity", "risk", "customer_analytics", "financial_health", "compliance",
    "reconciliation", "narrative",
]


def _count(factory: sessionmaker[Session], model: type, *where: Any) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model).where(*where)) or 0


def _job(factory: sessionmaker[Session], job_id: Any) -> Job:
    with factory() as s:
        job = s.get(Job, job_id)
        assert job is not None
        return job


def test_gold_pdf_end_to_end(factory: sessionmaker[Session], settings: Settings,
                             gold_pdf: tuple[str, bytes]) -> None:
    name, data = gold_pdf
    res = ingest_document(factory, data, name)
    worker = Worker(factory, settings=settings)
    assert worker.run_once() is True
    assert worker.run_once() is False  # queue drained

    job = _job(factory, res.job_id)
    assert (job.status, job.attempts, job.last_error) == ("succeeded", 1, None)
    assert job.locked_by is None and job.run_id is not None
    with factory() as s:
        run = s.get(Run, job.run_id)
        assert run is not None and run.status == "succeeded" and run.statement_id is not None
        assert run.agent_versions["categoriser"] == "3.0.0"  # Phase 4 bump (v3 routing)
        stages = list(s.scalars(select(StageExecution.stage).where(StageExecution.run_id == run.id)
                                .order_by(StageExecution.created_at, StageExecution.id)))
        assert stages == ALL_STAGES + ["render_reports"]
        doc = s.get(Document, res.document_id)
        assert doc is not None and doc.status == "processed"
        n_txn = s.scalar(select(func.count()).select_from(Transaction)
                         .where(Transaction.statement_id == run.statement_id))
        assert n_txn and n_txn > 0
        # one current categorisation per transaction
        assert s.scalar(select(func.count()).select_from(Categorisation)
                        .where(Categorisation.is_current)) == n_txn
        kinds = set(s.scalars(select(AnalysisOutput.kind).where(AnalysisOutput.run_id == run.id)))
        assert {"metrics", "customer_analytics", "narrative", "compliance"} <= kinds
        assert _count(factory, ReportArtifact, ReportArtifact.run_id == run.id) == 5

    # Same transaction count as calling the pipeline directly.
    assert n_txn == len(run_analysis(data, name).doc.transactions)


def test_flags_link_to_transaction_uuids(factory: sessionmaker[Session], settings: Settings,
                                         gold_pdf: tuple[str, bytes]) -> None:
    name, data = gold_pdf
    ingest_document(factory, data, name)
    Worker(factory, settings=settings).run_once()
    with factory() as s:
        txn_ids = {str(i) for i in s.scalars(select(Transaction.id))}
        for flag in s.scalars(select(Flag)):
            assert set(flag.transaction_ids) <= txn_ids


def test_validation_failure_fails_job_without_retry_and_keeps_dq(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    res = ingest_document(factory, csv_statement(30, break_at=10, break_by="500.00"), "broken.csv")
    worker = Worker(factory, settings=settings)
    worker.run_once()
    job = _job(factory, res.job_id)
    assert job.status == "failed" and job.attempts == 1
    assert job.last_error == "ValidationFailed: BALANCE_BREAK"
    assert worker.run_once() is False  # not retried
    with factory() as s:
        run = s.get(Run, job.run_id)
        assert run is not None and run.status == "failed" and run.statement_id is not None
        stmt = s.get(Statement, run.statement_id)
        assert stmt is not None and stmt.validation_status == "failed"
        dq = s.scalar(select(DQReportRow).where(DQReportRow.statement_id == run.statement_id))
        assert dq is not None and dq.passed is False
        rb = next(c for c in dq.checks if c["name"] == "running_balance")
        first_break = s.get(Transaction, uuid.UUID(rb["sample_transaction_uuids"][0]))
        assert first_break is not None and first_break.txn_seq == 10
        # analysis never ran, so nothing past validate was stored
        assert _count(factory, AnalysisOutput) == 0


def test_small_break_passes_gate_but_dq_reports_exact_row(
    factory: sessionmaker[Session], settings: Settings
) -> None:
    res = ingest_document(factory, csv_statement(30, break_at=7, break_by="0.50"), "small.csv")
    Worker(factory, settings=settings).run_once()
    job = _job(factory, res.job_id)
    assert job.status == "succeeded"
    with factory() as s:
        run = s.get(Run, job.run_id)
        assert run is not None
        dq = s.scalar(select(DQReportRow).where(DQReportRow.statement_id == run.statement_id))
        assert dq is not None and dq.passed is False
        rb = next(c for c in dq.checks if c["name"] == "running_balance")
        seqs = sorted(
            s.scalar(select(Transaction.txn_seq).where(Transaction.source_txn_id == tid))
            for tid in rb["sample_ids"]
        )
        assert seqs == [7, 8]  # the corrupted row and the one after it


class _Flaky:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    def __call__(self, data: bytes, filename: str, **kw: Any) -> Any:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient: secret narration text must not be stored")
        return run_analysis(data, filename, **kw)


def test_transient_failure_is_retried_with_backoff(factory: sessionmaker[Session]) -> None:
    settings = make_settings(job_backoff_s=60.0)
    res = ingest_document(factory, csv_statement(20), "a.csv")
    flaky = _Flaky(failures=1)
    worker = Worker(factory, settings=settings, analyse=flaky, render=None)
    worker.run_once()
    job = _job(factory, res.job_id)
    assert job.status == "queued" and job.attempts == 1
    assert job.last_error == "RuntimeError"  # exception type only, never the message
    assert worker.run_once() is False  # backoff: not due yet
    later = utcnow() + timedelta(seconds=61)
    claimed = worker.queue.claim(worker.worker_id, now=later)
    assert claimed is not None and claimed.attempt == 2
    worker.process(claimed)
    job = _job(factory, res.job_id)
    assert job.status == "succeeded" and job.attempts == 2
    with factory() as s:
        statuses = sorted(r.status for r in s.scalars(select(Run)))
    assert statuses == ["failed", "succeeded"]


def test_gives_up_after_max_attempts(factory: sessionmaker[Session], settings: Settings) -> None:
    res = ingest_document(factory, csv_statement(20), "a.csv")
    worker = Worker(factory, settings=settings, analyse=_Flaky(failures=99), render=None)
    while worker.run_once():
        pass
    job = _job(factory, res.job_id)
    assert (job.status, job.attempts, job.last_error) == ("failed", 3, "RuntimeError")
    with factory() as s:
        doc = s.get(Document, res.document_id)
        assert doc is not None and doc.status == "failed"


def test_killed_worker_job_is_reclaimed_and_completed(factory: sessionmaker[Session],
                                                      settings: Settings) -> None:
    """Worker A claims and dies. After the lease, B reclaims and finishes.
    If A wakes up later, its stale attempt must not write anything."""
    res = ingest_document(factory, csv_statement(20), "a.csv")
    a = Worker(factory, settings=settings, worker_id="A", render=None)
    b = Worker(factory, settings=settings, worker_id="B", render=None)
    a_claim = a.queue.claim("A")
    assert a_claim is not None
    with factory() as s:  # A got as far as opening its run, then was killed
        start_run(s, "run-of-dead-A", a_claim.document_id, a_claim.id)
        s.commit()

    assert b.run_once() is False  # lease still valid → nothing to do
    after_lease = utcnow() + timedelta(seconds=settings.job_lease_s + 1)
    b_claim = b.queue.claim("B", now=after_lease)
    assert b_claim is not None and b_claim.attempt == 2
    b.process(b_claim)
    assert _job(factory, res.job_id).status == "succeeded"
    n_txn = _count(factory, Transaction)

    with factory() as s:  # B closed out A's orphaned run when it reclaimed the job
        runs = {r.error: r.status for r in s.scalars(select(Run))}
    assert runs == {"WorkerLost": "failed", None: "succeeded"}
    with factory() as s:
        dead = s.get(Run, "run-of-dead-A")
        assert dead is not None and (dead.status, dead.error) == ("failed", "WorkerLost")

    a.process(a_claim)  # zombie A finishes its stale attempt
    job = _job(factory, res.job_id)
    assert job.status == "succeeded" and job.attempts == 2
    assert _count(factory, Statement) == 1
    assert _count(factory, Transaction) == n_txn


def test_worker_lost_on_last_attempt_fails_job(factory: sessionmaker[Session]) -> None:
    settings = make_settings(job_max_attempts=1)
    res = ingest_document(factory, csv_statement(20), "a.csv", max_attempts=1)
    w = Worker(factory, settings=settings, render=None)
    assert w.queue.claim("dead") is not None
    after_lease = utcnow() + timedelta(seconds=settings.job_lease_s + 1)
    assert w.queue.claim("B", now=after_lease) is None
    job = _job(factory, res.job_id)
    assert (job.status, job.last_error) == ("failed", "WorkerLost")


def test_result_write_is_atomic(factory: sessionmaker[Session], settings: Settings) -> None:
    """A failure after the statement rows are written must leave none of them."""
    res = ingest_document(factory, csv_statement(20), "a.csv")
    worker = Worker(factory, settings=settings, render=None)
    with patch("worker.runner.persist_analysis", side_effect=RuntimeError("disk full")):
        worker.run_once()
    for model in (Statement, Transaction, Categorisation, DQReportRow, Flag, AnalysisOutput):
        assert _count(factory, model) == 0, model.__name__
    job = _job(factory, res.job_id)
    assert job.status == "queued" and job.last_error == "RuntimeError"
    # and the retry succeeds cleanly
    worker.run_once()
    assert _job(factory, res.job_id).status == "succeeded"
    assert _count(factory, Statement) == 1


@pytest.mark.parametrize("stage_error", [ValueError, KeyError])
def test_stage_failure_is_logged_in_executions(factory: sessionmaker[Session], settings: Settings,
                                               stage_error: type[Exception]) -> None:
    ingest_document(factory, csv_statement(20), "a.csv")
    with patch("pipeline.run.FinancialAnalyst.analyse", side_effect=stage_error("boom")):
        Worker(factory, settings=settings, render=None).run_once()
    with factory() as s:
        failed = list(s.scalars(select(StageExecution).where(StageExecution.status == "failed")))
    assert [(e.stage, e.error) for e in failed] == [("financial_analysis", stage_error.__name__)]
