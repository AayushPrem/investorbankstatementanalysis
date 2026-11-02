"""Pydantic response/request models for the public API."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from schema.canonical import TransactionCategory


class UploadResponse(BaseModel):
    document_id: uuid.UUID
    job_id: uuid.UUID | None
    sha256: str
    duplicate: bool


class JobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None
    run_id: str | None
    created_at: datetime
    finished_at: datetime | None


class TransactionOut(BaseModel):
    id: uuid.UUID
    txn_seq: int
    source_txn_id: str
    txn_date: date
    description: str
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal
    currency: str
    rail: str
    counterparty: str | None
    customer_id: str | None
    is_related_party: bool | None
    category: str | None
    category_source: str | None


class TransactionPage(BaseModel):
    total: int
    limit: int
    offset: int
    items: list[TransactionOut]


class CategorisationSummary(BaseModel):
    total: int
    by_source: dict[str, int]
    by_category: dict[str, int]
    review_queue: list[TransactionOut]


class CategoryOverride(BaseModel):
    category: TransactionCategory


class DQResponse(BaseModel):
    run_id: str
    statement_id: uuid.UUID
    passed: bool
    checks: list[dict[str, Any]]


class FlagOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    detector: str
    kind: str
    severity: str
    score: float | None
    description: str
    evidence: dict[str, Any]
    transaction_ids: list[str]


class SummaryResponse(BaseModel):
    run_id: str
    status: str
    outputs: dict[str, Any]


class StageExecutionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    stage: str
    status: str
    attempt: int
    duration_ms: int
    error: str | None
    created_at: datetime
