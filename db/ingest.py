"""Idempotent ingestion (UPGRADE_GUIDE §8.3).

``sha256(file bytes)`` is computed before anything else. If a document with
that hash exists, the existing document and its latest job are returned with
``duplicate=True``, and nothing is reprocessed. Otherwise the document and a
queued job are inserted in **one** transaction. Two concurrent uploads of the
same bytes race on ``UNIQUE(documents.sha256)``: the loser catches the
IntegrityError and returns the winner's rows as a duplicate.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Annotated, Any

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from analysis.reconciliation import CompanyClaims
from config.settings import get_settings
from db.models import Document, Job
from db.session import session_scope
from pipeline.related_party import Affiliate

log = logging.getLogger(__name__)

DEFAULT_AFFILIATE_RELATIONSHIP = "user_tagged"


class AffiliateIn(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    relationship: str = Field(default=DEFAULT_AFFILIATE_RELATIONSHIP, max_length=64)
    aliases: list[Annotated[str, Field(min_length=1, max_length=255)]] = Field(
        default_factory=list, max_length=20
    )


class ClaimsIn(BaseModel):
    """Mirror of ``analysis.reconciliation.CompanyClaims`` (all optional)."""

    declared_revenue_total: Decimal | None = None
    declared_customer_count: int | None = None
    declared_nrr: float | None = None
    declared_never_lost_customer: bool | None = None
    declared_headcount: int | None = None
    declared_avg_monthly_salary: Decimal | None = None
    declared_loan_count: int | None = None


class JobOptions(BaseModel):
    """Per-upload pipeline options, stored as JSON on the job row."""

    company_name: str | None = Field(default=None, max_length=255)
    affiliates: list[AffiliateIn] = Field(default_factory=list, max_length=200)
    claims: ClaimsIn | None = None

    def to_affiliates(self) -> list[Affiliate]:
        return [Affiliate(**a.model_dump()) for a in self.affiliates]

    def to_claims(self) -> CompanyClaims | None:
        return CompanyClaims(**self.claims.model_dump()) if self.claims else None


@dataclass(frozen=True)
class IngestResult:
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    sha256: str
    duplicate: bool


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _existing(session: Session, sha: str) -> IngestResult | None:
    doc = session.scalar(select(Document).where(Document.sha256 == sha))
    if doc is None:
        return None
    job_id = session.scalar(
        select(Job.id).where(Job.document_id == doc.id).order_by(Job.created_at.desc()).limit(1)
    )
    return IngestResult(document_id=doc.id, job_id=job_id, sha256=sha, duplicate=True)


def ingest_document(
    factory: sessionmaker[Session],
    data: bytes,
    filename: str,
    *,
    mime: str | None = None,
    options: JobOptions | None = None,
    max_attempts: int | None = None,
) -> IngestResult:
    """Store *data* once and enqueue one job for it. Idempotent on content hash."""
    sha = sha256_hex(data)  # first, before anything else
    with session_scope(factory) as s:
        found = _existing(s, sha)
    if found is not None:
        log.info("duplicate upload", extra={"document_sha256": sha[:16]})
        return found

    opts: dict[str, Any] = (options or JobOptions()).model_dump(mode="json")
    try:
        with session_scope(factory) as s:
            doc = Document(
                sha256=sha,
                filename=filename[:255],
                mime=mime,
                size_bytes=len(data),
                status="received",
                content=data,
            )
            s.add(doc)
            s.flush()
            job = Job(
                document_id=doc.id,
                status="queued",
                max_attempts=max_attempts or get_settings().job_max_attempts,
                options=opts,
            )
            s.add(job)
            s.flush()
            result = IngestResult(document_id=doc.id, job_id=job.id, sha256=sha, duplicate=False)
    except IntegrityError:
        # Lost a race with a concurrent upload of the same bytes.
        with session_scope(factory) as s:
            found = _existing(s, sha)
        if found is None:
            raise
        return found
    log.info("document ingested", extra={"document_sha256": sha[:16], "size_bytes": len(data)})
    return result
