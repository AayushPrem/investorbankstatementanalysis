"""Persist one statement's pipeline output.

Every function here takes a ``Session`` and never commits. The caller
(``worker.runner``) wraps the whole write in a single transaction, so a
failure part-way leaves no rows from that statement (§8.3).
"""
from __future__ import annotations

import hashlib
import re
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from analysis.categoriser import match_rule
from config.settings import get_settings
from db.models import (
    Account,
    AnalysisOutput,
    Categorisation,
    DQReportRow,
    Flag,
    ReportArtifact,
    Run,
    Statement,
    Transaction,
)
from db.serialize import to_jsonable
from pipeline.validator import DQReport
from pipeline.versioning import AGENT_VERSIONS, JURISDICTION_MODULE_VERSION, schema_version
from schema.canonical import CanonicalTransaction, StatementDocument, TransactionCategory

if TYPE_CHECKING:
    from demo.pipeline_runner import RenderedReports
    from pipeline.run import AnalysisResult

_RAIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("UPI", re.compile(r"\bUPI\b", re.I)),
    ("NEFT", re.compile(r"\bNEFT\b", re.I)),
    ("IMPS", re.compile(r"\bIMPS\b", re.I)),
    ("RTGS", re.compile(r"\bRTGS\b", re.I)),
    ("CHQ", re.compile(r"\b(?:CHQ|CHEQUE|CLG|CLEARING)\b", re.I)),
    ("CASH", re.compile(r"\b(?:CASH|ATM|ATW)\b", re.I)),
    ("CARD", re.compile(r"\b(?:POS|CARD|VISA|MASTERCARD|RUPAY)\b", re.I)),
]

REPORT_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def infer_rail(description: str) -> str:
    """Payment rail from the narration prefix/keywords. OTHER when nothing matches."""
    for rail, pattern in _RAIL_PATTERNS:
        if pattern.search(description):
            return rail
    return "OTHER"


def _row_hash(t: CanonicalTransaction) -> str:
    key = "|".join([
        t.date.isoformat(), t.description.strip(), str(t.debit), str(t.credit), str(t.balance),
    ])
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _account_ref_hash(account_id: str) -> str:
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest()


def model_versions() -> dict[str, Any]:
    s = get_settings()
    return {"llm_model": s.llm_model if s.llm_available else None}


def agent_versions() -> dict[str, Any]:
    return {
        **AGENT_VERSIONS,
        "jurisdiction_module": JURISDICTION_MODULE_VERSION,
        "schema": schema_version(),
    }


def start_run(session: Session, run_id: str, document_id: uuid.UUID, job_id: uuid.UUID | None) -> Run:
    run = Run(
        id=run_id,
        document_id=document_id,
        job_id=job_id,
        agent_versions=agent_versions(),
        model_versions=model_versions(),
        status="running",
    )
    session.add(run)
    return run


def _get_or_create_account(session: Session, doc: StatementDocument, company_name: str | None) -> Account:
    ref_hash = _account_ref_hash(doc.account_id)
    bank = doc.bank_name or "UNKNOWN"
    acct = (
        session.query(Account)
        .filter(Account.external_account_ref_hash == ref_hash, Account.bank_name == bank)
        .one_or_none()
    )
    if acct is None:
        acct = Account(external_account_ref_hash=ref_hash, bank_name=bank, company_name=company_name)
        session.add(acct)
        session.flush()
    elif company_name and not acct.company_name:
        acct.company_name = company_name
    return acct


def persist_statement(
    session: Session,
    *,
    document_id: uuid.UUID,
    doc: StatementDocument,
    validation_status: str,
    dq_report: DQReport | None,
    company_name: str | None = None,
    opening_balance: Decimal | None = None,
    categoriser_model_version: str | None = None,
) -> tuple[Statement, dict[str, uuid.UUID]]:
    """Insert statement + transactions (+ DQ report). Returns the statement and
    a map from the pipeline's transaction_id to the DB uuid."""
    acct = _get_or_create_account(session, doc, company_name)
    stmt = Statement(
        document_id=document_id,
        account_id=acct.id,
        period_start=doc.statement_period_start,
        period_end=doc.statement_period_end,
        validation_status=validation_status,
        schema_version=schema_version(),
        source_format=doc.source_format,
        opening_balance=opening_balance,
    )
    session.add(stmt)
    session.flush()

    ordered = sorted(doc.transactions, key=lambda t: (t.source_reference.page, t.source_reference.row))
    id_map: dict[str, uuid.UUID] = {}
    for seq, t in enumerate(ordered):
        row = Transaction(
            id=uuid.uuid4(),
            statement_id=stmt.id,
            txn_seq=seq,
            source_txn_id=t.transaction_id,
            txn_date=t.date,
            description=t.description,
            debit=t.debit,
            credit=t.credit,
            balance=t.balance,
            currency=(t.currency or "INR")[:3],
            rail=infer_rail(t.description),
            counterparty=t.counterparty,
            customer_id=t.customer_id,
            is_related_party=t.is_related_party,
            source_page=t.source_reference.page,
            source_row=t.source_reference.row,
            row_hash=_row_hash(t),
        )
        session.add(row)
        id_map[t.transaction_id] = row.id
        if t.category is not None:
            session.add(_categorisation(row.id, t, categoriser_model_version))

    if dq_report is not None:
        session.add(DQReportRow(
            statement_id=stmt.id,
            checks=[_dq_check_with_uuids(c.to_dict(), id_map) for c in dq_report.checks],
            passed=dq_report.passed,
        ))
    session.flush()
    return stmt, id_map


def _dq_check_with_uuids(check: dict[str, Any], id_map: dict[str, uuid.UUID]) -> dict[str, Any]:
    """Keep the pipeline ids and add the matching DB transaction uuids."""
    ids = check.get("sample_ids") or []
    check["sample_transaction_uuids"] = [str(id_map[i]) for i in ids if i in id_map]
    return check


def _categorisation(
    txn_uuid: uuid.UUID, t: CanonicalTransaction, ml_model_version: str | None = None,
) -> Categorisation:
    """One current categorisation row. ``model_version`` is the ML model for
    rows routed through it (ml / llm / review), else the categoriser version."""
    assert t.category is not None
    hit = match_rule(t)
    source = t.category_source
    if source is None:  # categorised outside Categoriser v3: derive as before
        if hit is not None and hit[1] == t.category:
            source = "rule"
        elif t.category == TransactionCategory.OTHER:
            source = "default"
        else:
            source = "llm"
    rule_id = f"rule_{hit[0]}" if source == "rule" and hit is not None else None
    conf = t.category_confidence
    return Categorisation(
        transaction_id=txn_uuid,
        category=str(t.category),
        source=source,
        confidence=None if conf is None else min(max(float(conf), 0.0), 1.0),
        rule_id=rule_id,
        model_version=(
            ml_model_version if ml_model_version and source in ("ml", "llm", "review")
            else AGENT_VERSIONS.get("categoriser")
        ),
        is_current=True,
    )


def persist_analysis(
    session: Session,
    *,
    run: Run,
    analysis: AnalysisResult,
    reports: RenderedReports | None,
    id_map: dict[str, uuid.UUID],
) -> None:
    """Flags, analysis outputs and rendered reports for a successful run."""
    for f in analysis.risk_report.flags:
        session.add(Flag(
            run_id=run.id,
            detector=f.detector_name,
            kind="rule",
            severity=str(f.severity),
            score=None,
            description=f.description,
            evidence=to_jsonable(f.evidence),
            transaction_ids=[str(id_map[i]) for i in f.triggering_transaction_ids if i in id_map],
        ))

    outputs: dict[str, Any] = {
        "metrics": analysis.metrics,
        "customer_analytics": analysis.customer_analytics,
        "financial_health": analysis.health_report,
        "compliance": analysis.compliance_report,
        "reconciliation": analysis.reconciliation_report,
        "narrative": {"text": analysis.narrative, "company_name": analysis.company_name},
        "categorisation_stats": analysis.categorisation_stats,
        "validation": analysis.validation_report,
    }
    for kind, payload in outputs.items():
        session.add(AnalysisOutput(run_id=run.id, kind=kind, payload=to_jsonable(payload)))

    if reports is not None:
        for lens, fmt, content in (
            ("angel", "pdf", reports.angel_pdf),
            ("vc", "xlsx", reports.vc_xlsx),
            ("vc", "pdf", reports.vc_pdf),
            ("workbench", "xlsx", reports.workbench_xlsx),
            ("workbench", "pdf", reports.workbench_pdf),
        ):
            session.add(ReportArtifact(
                run_id=run.id, lens=lens, fmt=fmt,
                media_type=REPORT_MEDIA_TYPES[fmt], content=content,
            ))

