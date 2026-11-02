"""Job worker: claim → run_analysis → render → persist (ADR-0002).

One job at a time per worker process. Run several processes for parallelism.
The `jobs` table and `SKIP LOCKED` keep them from colliding.

Failure semantics:

* Validation FAILED (deterministic bad data): the statement, its
  transactions and the DQ report are persisted, the run is ``failed`` and the
  job is ``failed`` with no retry. Retrying can't fix the data.
* Any other exception: the run is ``failed`` and the job is re-queued with
  exponential backoff until ``max_attempts``, then ``failed``.
* Worker killed mid-job: nothing was committed for the result (one
  transaction), and the lease expires, so another worker reclaims the job and
  starts a fresh run.

Logs carry ids, counts and exception *types* only (§1.9).
"""
from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from config.logging import new_run_id, safe_exc
from config.settings import Settings, get_settings
from db.ingest import JobOptions
from db.models import Document, Job, Run, StageExecution
from db.repositories.jobs import (
    ClaimedJob,
    JobQueue,
    LeaseLostError,
    assert_owned,
    mark_failed,
    mark_succeeded,
)
from db.repositories.results import persist_analysis, persist_statement, start_run
from db.session import session_scope
from pipeline.run import AnalysisOptions, AnalysisResult, StageEvent, run_analysis
from pipeline.validator import ValidationFailedError

log = logging.getLogger(__name__)

Analyse = Callable[..., AnalysisResult]
Render = Callable[[AnalysisResult], object]


def default_worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def _default_render(analysis: AnalysisResult) -> object:
    from demo.pipeline_runner import render_reports

    return render_reports(analysis)


class Worker:
    def __init__(
        self,
        factory: sessionmaker[Session],
        *,
        worker_id: str | None = None,
        settings: Settings | None = None,
        analyse: Analyse = run_analysis,
        render: Render | None = _default_render,
    ) -> None:
        self._factory = factory
        self._settings = settings or get_settings()
        self.worker_id = worker_id or default_worker_id()
        self._analyse = analyse
        self._render = render
        self.queue = JobQueue(
            factory, lease_s=self._settings.job_lease_s, backoff_s=self._settings.job_backoff_s
        )

    # ── loop ──────────────────────────────────────────────────────────────

    def run_once(self) -> bool:
        """Claim and process at most one job. True if a job was claimed."""
        claimed = self.queue.claim(self.worker_id)
        if claimed is None:
            return False
        self.process(claimed)
        return True

    def run_forever(self, stop: Callable[[], bool] = lambda: False) -> None:
        log.info("worker started", extra={"worker_id": self.worker_id})
        while not stop():
            try:
                worked = self.run_once()
            except Exception as exc:  # noqa: BLE001 — keep the loop alive (DB blips)
                log.warning("worker loop error", extra={"error": safe_exc(exc)})
                worked = False
            if not worked:
                time.sleep(self._settings.worker_poll_s)

    # ── one job ───────────────────────────────────────────────────────────

    def process(self, claimed: ClaimedJob) -> None:
        with session_scope(self._factory) as s:
            doc_row = s.get(Document, claimed.document_id)
            job_row = s.get(Job, claimed.id)
            assert doc_row is not None and job_row is not None
            data, filename = doc_row.content, doc_row.filename
            opts = JobOptions.model_validate(job_row.options or {})

        run_id = new_run_id()
        with session_scope(self._factory) as s:
            # A reclaimed job: earlier attempts' runs were left "running" by a dead worker.
            s.execute(
                update(Run)
                .where(Run.job_id == claimed.id, Run.status == "running")
                .values(status="failed", error="WorkerLost", finished_at=datetime.now(UTC))
            )
            start_run(s, run_id, claimed.document_id, claimed.id)
        log.info("job claimed", extra={"job_id": str(claimed.id), "attempt": claimed.attempt})

        observer = self._observer(run_id, claimed)
        try:
            analysis = self._analyse(
                data, filename,
                options=AnalysisOptions(
                    affiliates=opts.to_affiliates(),
                    company_name=opts.company_name,
                    claims=opts.to_claims(),
                    stage_observer=observer,
                    run_id=run_id,
                ),
            )
            reports = self._render_timed(analysis, observer)
        except ValidationFailedError as exc:
            self._persist_validation_failure(run_id, claimed, exc, opts)
            return
        except Exception as exc:  # noqa: BLE001 — recorded on the job, then retried
            self._fail_run(run_id, safe_exc(exc))
            status = self.queue.retry_or_fail(
                claimed.id, safe_exc(exc), worker_id=self.worker_id, attempt=claimed.attempt
            )
            log.warning("job attempt failed",
                        extra={"job_id": str(claimed.id), "error": safe_exc(exc), "next": status})
            return

        try:
            with session_scope(self._factory) as s:
                # Lock + ownership check first: a worker whose lease was taken writes nothing.
                assert_owned(s, claimed.id, self.worker_id, claimed.attempt)
                stmt, id_map = persist_statement(
                    s,
                    document_id=claimed.document_id,
                    doc=analysis.doc,
                    validation_status=str(analysis.validation_report.status),
                    dq_report=analysis.dq_report,
                    company_name=analysis.company_name,
                    categoriser_model_version=analysis.categorisation_stats.model_version,
                )
                run = s.get(Run, run_id)
                assert run is not None
                run.statement_id = stmt.id
                persist_analysis(s, run=run, analysis=analysis, reports=reports,  # type: ignore[arg-type]
                                 id_map=id_map)
                run.status = "succeeded"
                run.finished_at = datetime.now(UTC)
                mark_succeeded(s, claimed.id, run_id, worker_id=self.worker_id, attempt=claimed.attempt)
        except LeaseLostError:
            self._fail_run(run_id, "LeaseLost")
            log.warning("lease lost; results discarded", extra={"job_id": str(claimed.id)})
            return
        except Exception as exc:  # noqa: BLE001 — the result transaction rolled back
            self._fail_run(run_id, safe_exc(exc))
            self.queue.retry_or_fail(
                claimed.id, safe_exc(exc), worker_id=self.worker_id, attempt=claimed.attempt
            )
            log.warning("persist failed", extra={"job_id": str(claimed.id), "error": safe_exc(exc)})
            return
        log.info("job succeeded", extra={"job_id": str(claimed.id), "txns": len(id_map)})

    # ── helpers ───────────────────────────────────────────────────────────

    def _observer(self, run_id: str, claimed: ClaimedJob) -> Callable[[StageEvent], None]:
        def observe(ev: StageEvent) -> None:
            with session_scope(self._factory) as s:
                s.add(StageExecution(
                    run_id=run_id, stage=ev.stage, status=ev.status, attempt=claimed.attempt,
                    duration_ms=ev.duration_ms, error=ev.error,
                ))
            self.queue.heartbeat(claimed.id, self.worker_id)
        return observe

    def _render_timed(self, analysis: AnalysisResult,
                      observer: Callable[[StageEvent], None]) -> object | None:
        if self._render is None:
            return None
        start = time.perf_counter()
        try:
            reports = self._render(analysis)
        except Exception as exc:
            observer(StageEvent("render_reports", "failed", _ms(start), safe_exc(exc)))
            raise
        observer(StageEvent("render_reports", "succeeded", _ms(start)))
        return reports

    def _fail_run(self, run_id: str, error: str) -> None:
        with session_scope(self._factory) as s:
            run = s.get(Run, run_id)
            if run is not None:
                run.status = "failed"
                run.error = error[:200]
                run.finished_at = datetime.now(UTC)

    def _persist_validation_failure(self, run_id: str, claimed: ClaimedJob,
                                    exc: ValidationFailedError, opts: JobOptions) -> None:
        codes = sorted({i.code for i in exc.report.issues if i.severity == "ERROR"})
        error = "ValidationFailed: " + ",".join(codes)
        try:
            with session_scope(self._factory) as s:
                assert_owned(s, claimed.id, self.worker_id, claimed.attempt)
                run = s.get(Run, run_id)
                assert run is not None
                if exc.doc is not None:
                    stmt, _ = persist_statement(
                        s,
                        document_id=claimed.document_id,
                        doc=exc.doc,
                        validation_status="failed",
                        dq_report=exc.dq_report,
                        company_name=opts.company_name,
                    )
                    run.statement_id = stmt.id
                run.status = "failed"
                run.error = error[:200]
                run.finished_at = datetime.now(UTC)
                mark_failed(s, claimed.id, run_id, error, worker_id=self.worker_id,
                            attempt=claimed.attempt)
        except LeaseLostError:
            self._fail_run(run_id, "LeaseLost")
            return
        log.info("job failed validation", extra={"job_id": str(claimed.id), "codes": len(codes)})


def _ms(start: float) -> int:
    return int(round((time.perf_counter() - start) * 1000))

