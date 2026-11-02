"""Idempotent ingestion (§8.3): same bytes twice → one document, one job."""
from __future__ import annotations

import hashlib
from unittest.mock import patch

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from db.ingest import AffiliateIn, JobOptions, ingest_document
from db.models import Document, Job


def _count(factory: sessionmaker[Session], model: type) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(model)) or 0


def test_same_bytes_twice_yields_one_document(factory: sessionmaker[Session]) -> None:
    data = b"%PDF-1.4 fake statement"
    first = ingest_document(factory, data, "a.pdf")
    second = ingest_document(factory, data, "renamed.pdf")
    assert first.duplicate is False and second.duplicate is True
    assert second.document_id == first.document_id
    assert second.job_id == first.job_id
    assert first.sha256 == hashlib.sha256(data).hexdigest()
    assert _count(factory, Document) == 1
    assert _count(factory, Job) == 1


def test_different_bytes_are_different_documents(factory: sessionmaker[Session]) -> None:
    a = ingest_document(factory, b"one", "a.pdf")
    b = ingest_document(factory, b"two", "a.pdf")
    assert a.document_id != b.document_id
    assert _count(factory, Document) == 2


def test_options_are_stored_on_the_job(factory: sessionmaker[Session]) -> None:
    opts = JobOptions(company_name="Acme", affiliates=[AffiliateIn(name="Mehta Enterprises")])
    res = ingest_document(factory, b"bytes", "a.pdf", options=opts)
    with factory() as s:
        job = s.get(Job, res.job_id)
        assert job is not None
        restored = JobOptions.model_validate(job.options)
    assert restored.company_name == "Acme"
    aff = restored.to_affiliates()
    assert aff[0].name == "Mehta Enterprises" and aff[0].relationship == "user_tagged"


def test_lost_race_returns_the_winner(factory: sessionmaker[Session]) -> None:
    """Simulate a concurrent upload that commits between our check and our insert."""
    data = b"race"
    winner = ingest_document(factory, data, "a.pdf")
    import db.ingest as ingest_mod

    real_existing = ingest_mod._existing
    calls = {"n": 0}

    def first_call_misses(session, sha):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        return None if calls["n"] == 1 else real_existing(session, sha)

    with patch.object(ingest_mod, "_existing", side_effect=first_call_misses):
        loser = ingest_document(factory, data, "b.pdf")
    assert loser.duplicate is True
    assert loser.document_id == winner.document_id
    assert _count(factory, Document) == 1
