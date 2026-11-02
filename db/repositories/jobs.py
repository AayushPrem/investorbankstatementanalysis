"""Postgres-backed job queue (ADR-0002).

``claim`` selects one claimable job with ``FOR UPDATE SKIP LOCKED`` (Postgres)
and marks it running in the same transaction. A job is claimable if it is
``queued`` and due (``available_at <= now``), or ``running`` with a lease that
expired (``locked_at < now - lease``), which means its worker died. SQLite
(tests) ignores ``FOR UPDATE``, so the no-double-claim guarantee is only
exercised by the ``postgres``-marked tests.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from db.models import Document, Job, utcnow
from db.session import session_scope


@dataclass(frozen=True)
class ClaimedJob:
    id: uuid.UUID
    document_id: uuid.UUID
    attempt: int
    max_attempts: int


class JobQueue:
    """The worker's only view of the queue, so the backend can be swapped later."""

    def __init__(self, factory: sessionmaker[Session], *, lease_s: float, backoff_s: float) -> None:
        self._factory = factory
        self._lease = timedelta(seconds=lease_s)
        self._backoff_s = backoff_s

    def claim(self, worker_id: str, *, now: datetime | None = None) -> ClaimedJob | None:
        now = now or utcnow()
        stale = now - self._lease
        with session_scope(self._factory) as s:
            job = s.scalar(
                select(Job)
                .where(or_(
                    and_(Job.status == "queued", Job.available_at <= now),
                    and_(Job.status == "running", Job.locked_at < stale),
                ))
                .order_by(Job.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if job is None:
                return None
            if job.attempts >= job.max_attempts:
                # A reclaimed job whose worker died on its last attempt.
                job.status = "failed"
                job.last_error = job.last_error or "WorkerLost"
                job.finished_at = now
                job.locked_at = None
                job.locked_by = None
                s.execute(update(Document).where(Document.id == job.document_id).values(status="failed"))
                return None
            job.status = "running"
            job.attempts += 1
            job.locked_at = now
            job.locked_by = worker_id[:100]
            s.execute(update(Document).where(Document.id == job.document_id).values(status="processing"))
            return ClaimedJob(job.id, job.document_id, job.attempts, job.max_attempts)

    def heartbeat(self, job_id: uuid.UUID, worker_id: str) -> None:
        """Extend the lease (called between stages). No-op if we no longer own the job."""
        with session_scope(self._factory) as s:
            s.execute(
                update(Job)
                .where(Job.id == job_id, Job.locked_by == worker_id[:100], Job.status == "running")
                .values(locked_at=utcnow())
            )

    def retry_or_fail(self, job_id: uuid.UUID, error: str, *, worker_id: str, attempt: int,
                      retryable: bool = True, now: datetime | None = None) -> str:
        """After a failed attempt: re-queue with backoff, or fail for good. Returns the new status.

        A no-op (returns ``"not_owner"``) if this worker's lease was lost. A
        zombie worker must never re-queue or fail a job that another worker
        has since reclaimed or completed.
        """
        now = now or utcnow()
        with session_scope(self._factory) as s:
            job = s.get(Job, job_id, with_for_update=True)
            if job is None:
                return "missing"
            if not _is_owner(job, worker_id, attempt):
                return "not_owner"
            job.last_error = error[:500]
            job.locked_at = None
            job.locked_by = None
            if retryable and job.attempts < job.max_attempts:
                job.status = "queued"
                job.available_at = now + timedelta(seconds=self._backoff_s * 2 ** (job.attempts - 1))
                status = "queued"
            else:
                job.status = "failed"
                job.finished_at = now
                s.execute(update(Document).where(Document.id == job.document_id).values(status="failed"))
                status = "failed"
            return status


class LeaseLostError(RuntimeError):
    """The job was reclaimed by another worker; this worker must not write results."""


def _is_owner(job: Job, worker_id: str, attempt: int) -> bool:
    return job.status == "running" and job.locked_by == worker_id[:100] and job.attempts == attempt


def assert_owned(session: Session, job_id: uuid.UUID, worker_id: str, attempt: int) -> Job:
    """Lock the job row and check this worker still holds the lease, else LeaseLostError.

    Call it first in a result transaction, before writing anything.
    """
    job = session.get(Job, job_id, with_for_update=True)
    if job is None or not _is_owner(job, worker_id, attempt):
        raise LeaseLostError(str(job_id))
    return job


def mark_succeeded(session: Session, job_id: uuid.UUID, run_id: str, *,
                   worker_id: str, attempt: int) -> None:
    """Inside the caller's result transaction, so results and status commit together.

    Raises LeaseLostError (and the caller rolls back) if another worker has
    reclaimed the job since this one claimed it.
    """
    job = assert_owned(session, job_id, worker_id, attempt)
    job.status = "succeeded"
    job.run_id = run_id
    job.last_error = None
    job.locked_at = None
    job.locked_by = None
    job.finished_at = utcnow()
    session.execute(update(Document).where(Document.id == job.document_id).values(status="processed"))


def mark_failed(session: Session, job_id: uuid.UUID, run_id: str, error: str, *,
                worker_id: str, attempt: int) -> None:
    """Non-retryable failure (e.g. validation FAILED), in the caller's transaction."""
    job = assert_owned(session, job_id, worker_id, attempt)
    job.status = "failed"
    job.run_id = run_id
    job.last_error = error[:500]
    job.locked_at = None
    job.locked_by = None
    job.finished_at = utcnow()
    session.execute(update(Document).where(Document.id == job.document_id).values(status="failed"))
