"""Read endpoints for one run: transactions, categorisation, DQ, flags, summary, report, executions."""
from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Query
from fastapi.responses import Response
from sqlalchemy import Select, func, select
from sqlalchemy.orm import Session

from api.deps import SessionDep
from api.errors import APIError, not_found
from api.schemas import (
    CategorisationSummary,
    DQResponse,
    FlagOut,
    StageExecutionOut,
    SummaryResponse,
    TransactionOut,
    TransactionPage,
)
from db.models import (
    AnalysisOutput,
    Categorisation,
    DQReportRow,
    Flag,
    ReportArtifact,
    Run,
    StageExecution,
    Transaction,
)

router = APIRouter(prefix="/v1/runs", tags=["runs"])

# Rows routed for human review: no rule fired, so they fell back to OTHER ("default")
# or were categorised by the LLM. Phase 4 adds low-confidence ML rows here.
REVIEW_SOURCES = ("default", "llm")
SUMMARY_KINDS = ("metrics", "customer_analytics", "financial_health", "narrative",
                 "categorisation_stats")


def _run(session: Session, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise not_found("run", run_id)
    return run


def _statement_id(run: Run) -> uuid.UUID:
    if run.statement_id is None:
        raise APIError(409, "no_statement", "This run has no persisted statement",
                       {"run_status": run.status, "error": run.error})
    return run.statement_id


def _txn_query(statement_id: uuid.UUID) -> Select[Transaction, Categorisation]:
    return (
        select(Transaction, Categorisation)
        .outerjoin(
            Categorisation,
            (Categorisation.transaction_id == Transaction.id) & Categorisation.is_current,
        )
        .where(Transaction.statement_id == statement_id)
    )


def txn_out(t: Transaction, c: Categorisation | None) -> TransactionOut:
    return TransactionOut(
        id=t.id, txn_seq=t.txn_seq, source_txn_id=t.source_txn_id, txn_date=t.txn_date,
        description=t.description, debit=t.debit, credit=t.credit, balance=t.balance,
        currency=t.currency, rail=t.rail, counterparty=t.counterparty, customer_id=t.customer_id,
        is_related_party=t.is_related_party,
        category=c.category if c else None, category_source=c.source if c else None,
    )


@router.get("/{run_id}/transactions", response_model=TransactionPage)
def list_transactions(
    run_id: str,
    session: SessionDep,
    category: str | None = None,
    source: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TransactionPage:
    q = _txn_query(_statement_id(_run(session, run_id)))
    if category:
        q = q.where(Categorisation.category == category)
    if source:
        q = q.where(Categorisation.source == source)
    if date_from:
        q = q.where(Transaction.txn_date >= date_from)
    if date_to:
        q = q.where(Transaction.txn_date <= date_to)
    total = session.scalar(select(func.count()).select_from(q.subquery())) or 0
    rows = session.execute(q.order_by(Transaction.txn_seq).limit(limit).offset(offset)).all()
    return TransactionPage(total=total, limit=limit, offset=offset,
                           items=[txn_out(t, c) for t, c in rows])


@router.get("/{run_id}/categorisation", response_model=CategorisationSummary)
def categorisation(
    run_id: str,
    session: SessionDep,
    review_limit: Annotated[int, Query(ge=0, le=500)] = 100,
) -> CategorisationSummary:
    sid = _statement_id(_run(session, run_id))
    base = _txn_query(sid).subquery()
    total = session.scalar(select(func.count()).select_from(base)) or 0
    by_source: dict[Any, int] = dict(session.execute(
        select(base.c.source, func.count()).group_by(base.c.source)
    ).all())
    by_category: dict[Any, int] = dict(session.execute(
        select(base.c.category, func.count()).group_by(base.c.category)
    ).all())
    review = session.execute(
        _txn_query(sid).where(Categorisation.source.in_(REVIEW_SOURCES))
        .order_by(Transaction.txn_seq).limit(review_limit)
    ).all()
    return CategorisationSummary(
        total=total,
        by_source={str(k): v for k, v in by_source.items()},
        by_category={str(k): v for k, v in by_category.items()},
        review_queue=[txn_out(t, c) for t, c in review],
    )


@router.get("/{run_id}/dq", response_model=DQResponse)
def dq_report(run_id: str, session: SessionDep) -> DQResponse:
    run = _run(session, run_id)
    sid = _statement_id(run)
    row = session.scalar(select(DQReportRow).where(DQReportRow.statement_id == sid))
    if row is None:
        raise not_found("dq report", run_id)
    return DQResponse(run_id=run.id, statement_id=sid, passed=row.passed, checks=row.checks)


@router.get("/{run_id}/flags", response_model=list[FlagOut])
def flags(run_id: str, session: SessionDep, severity: str | None = None) -> list[Flag]:
    _run(session, run_id)
    q = select(Flag).where(Flag.run_id == run_id)
    if severity:
        q = q.where(Flag.severity == severity.upper())
    return list(session.scalars(q.order_by(Flag.severity, Flag.detector)))


@router.get("/{run_id}/summary", response_model=SummaryResponse)
def summary(run_id: str, session: SessionDep) -> SummaryResponse:
    run = _run(session, run_id)
    rows = session.scalars(
        select(AnalysisOutput).where(AnalysisOutput.run_id == run_id,
                                     AnalysisOutput.kind.in_(SUMMARY_KINDS))
    )
    outputs: dict[str, Any] = {r.kind: r.payload for r in rows}
    return SummaryResponse(run_id=run.id, status=run.status, outputs=outputs)


@router.get("/{run_id}/report")
def report(
    run_id: str,
    session: SessionDep,
    lens: Literal["angel", "vc", "workbench"],
    format: Literal["pdf", "xlsx"] | None = None,  # noqa: A002 — public query param name
) -> Response:
    _run(session, run_id)
    fmt = format or ("pdf" if lens == "angel" else "xlsx")
    art = session.scalar(
        select(ReportArtifact).where(
            ReportArtifact.run_id == run_id, ReportArtifact.lens == lens, ReportArtifact.fmt == fmt
        )
    )
    if art is None:
        raise not_found("report", f"{run_id}:{lens}:{fmt}")
    return Response(
        content=art.content,
        media_type=art.media_type,
        headers={"Content-Disposition": f'attachment; filename="{lens}_{run_id}.{fmt}"'},
    )


@router.get("/{run_id}/executions", response_model=list[StageExecutionOut])
def executions(run_id: str, session: SessionDep) -> list[StageExecution]:
    _run(session, run_id)
    return list(session.scalars(
        select(StageExecution).where(StageExecution.run_id == run_id)
        .order_by(StageExecution.created_at, StageExecution.id)
    ))
