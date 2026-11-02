"""The single canonical pipeline entry point: :func:`run_analysis`.

Before Phase 1 there were two orchestrators. ``pipeline/orchestrator.py`` ran
Sprint 1 stages only. ``demo/pipeline_runner.run_pipeline_on_bytes`` was the
real end-to-end flow, but it was hardwired to the digital-PDF adapter and also
rendered PDFs. This module is that flow minus report rendering, with the
adapter chosen from the input. Callers:

* ``demo/pipeline_runner.run_pipeline_on_bytes``: ``run_analysis`` + PDF/XLSX lenses
* ``pipeline/batch.py``: through ``run_pipeline_on_bytes`` (the UI needs the report
  bytes), or ``run_analysis`` directly with ``BatchOrchestrator(render_reports=False)``

Stage order and per-stage flags are identical to the pre-Phase-1 runner:

    adapter → Normaliser → Validator (FAILED raises) → Categoriser(llm)
    → FinancialAnalyst → RelatedPartyTagger(llm=False)
    → CustomerIdentityResolver(embeddings=False) → RiskAnalyst
    → CustomerAnalyticsAnalyst → FinancialHealthAnalyst
    → ComplianceAnalyst(India) → ReconciliationAnalyst → report narrative
"""
from __future__ import annotations

import hashlib
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from adapters.base import RawStatement
from analysis.categoriser import CategorisationStats, Categoriser
from analysis.compliance import ComplianceAnalyst, ComplianceReport
from analysis.customer_analytics import CustomerAnalyticsAnalyst, CustomerAnalyticsReport
from analysis.financial_analyst import FinancialAnalyst, FinancialMetrics
from analysis.financial_health_alerts import FinancialHealthAnalyst, FinancialHealthReport
from analysis.reconciliation import CompanyClaims, ReconciliationAnalyst, ReconciliationReport
from analysis.report_narrative import (
    ReportNarrativeInput,
    generate_report_narrative,
    severity_counts,
)
from analysis.risk import RiskAnalyst, RiskReport
from config.logging import run_context, stage_timer
from config.settings import get_settings
from jurisdictions.india import IndiaJurisdictionModule
from pipeline.customer_identity import CustomerIdentityResolver
from pipeline.normaliser import Normaliser
from pipeline.related_party import Affiliate, RelatedPartyTagger
from pipeline.validator import ValidationFailedError, ValidationReport, Validator
from schema.canonical import StatementDocument, ValidationStatus

log = logging.getLogger(__name__)

ADAPTER_DIGITAL_PDF = "digital_pdf"
ADAPTER_SCANNED_PDF = "scanned_pdf"
ADAPTER_CSV = "csv"
ADAPTER_EXCEL = "excel"
ADAPTER_IMAGE = "image"
ADAPTERS = (ADAPTER_DIGITAL_PDF, ADAPTER_SCANNED_PDF, ADAPTER_CSV, ADAPTER_EXCEL, ADAPTER_IMAGE)

_CSV_SUFFIXES = frozenset({".csv"})
_EXCEL_SUFFIXES = frozenset({".xlsx", ".xlsm", ".xls"})
_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".tif", ".tiff"})


class _Adapter(Protocol):
    def extract(self, path: str | Path) -> RawStatement: ...


@dataclass
class AnalysisOptions:
    """Per-run knobs. ``None`` means "use config/settings.py"."""

    affiliates: list[Affiliate] | None = None
    company_name: str | None = None
    claims: CompanyClaims | None = None
    # LLM for categoriser fallback + narratives/polish. It is still gated on a
    # key being present, exactly as before.
    llm_enabled: bool | None = None
    # LLM fuzzy related-party matching. Off in the demo path since Sprint 2.
    related_party_llm: bool = False
    # MiniLM customer-identity embeddings. None → settings.embeddings_enabled (default False).
    embeddings_enabled: bool | None = None
    # Force an adapter (one of ADAPTERS). None → auto-select from filename/content.
    adapter: str | None = None


@dataclass
class AnalysisResult:
    """Everything the pipeline computes for one statement (no rendered reports)."""

    doc: StatementDocument
    validation_report: ValidationReport
    categorisation_stats: CategorisationStats
    metrics: FinancialMetrics
    risk_report: RiskReport
    customer_analytics: CustomerAnalyticsReport
    health_report: FinancialHealthReport
    compliance_report: ComplianceReport
    reconciliation_report: ReconciliationReport
    narrative: str
    company_name: str
    adapter_name: str
    run_id: str
    stage_names: list[str] = field(default_factory=list)


# ── Adapter selection ────────────────────────────────────────────────────────

def _pdf_has_text(path: Path) -> bool:
    """True if any page has extractable text (→ digital), False if none (→ scanned).

    If the probe itself fails (corrupt or empty bytes), it returns True so the
    digital adapter runs and raises its usual AdapterError, keeping existing
    error behaviour.
    """
    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            if not pdf.pages:
                return True
            return any((p.extract_text() or "").strip() for p in pdf.pages)
    except Exception:  # noqa: BLE001 — probe only; the adapter reports the real error
        return True


def select_adapter_name(path: Path) -> str:
    """Choose an adapter from the file suffix (and, for PDFs, a text probe)."""
    suffix = path.suffix.lower()
    if suffix in _CSV_SUFFIXES:
        return ADAPTER_CSV
    if suffix in _EXCEL_SUFFIXES:
        return ADAPTER_EXCEL
    if suffix in _IMAGE_SUFFIXES:
        return ADAPTER_IMAGE
    return ADAPTER_DIGITAL_PDF if _pdf_has_text(path) else ADAPTER_SCANNED_PDF


def make_adapter(name: str) -> _Adapter:
    """Instantiate an adapter by name. OCR adapters are imported lazily (optional ``[ocr]`` extra)."""
    if name == ADAPTER_DIGITAL_PDF:
        from adapters.digital_pdf import DigitalPDFAdapter

        return DigitalPDFAdapter()
    if name == ADAPTER_SCANNED_PDF:
        from adapters.scanned_pdf import ScannedPDFAdapter

        return ScannedPDFAdapter()
    if name == ADAPTER_CSV:
        from adapters.csv import CSVAdapter

        return CSVAdapter()
    if name == ADAPTER_EXCEL:
        from adapters.excel import ExcelAdapter

        return ExcelAdapter()
    if name == ADAPTER_IMAGE:
        from adapters.image import ImageAdapter

        return ImageAdapter()
    raise ValueError(f"unknown adapter {name!r}; expected one of {ADAPTERS}")


# ── Pipeline ─────────────────────────────────────────────────────────────────

def run_analysis(
    data: bytes,
    filename: str,
    *,
    options: AnalysisOptions | None = None,
) -> AnalysisResult:
    """Run every analysis stage on one statement's raw bytes.

    Raises ``AdapterError`` for unreadable or unsupported input and
    ``ValidationFailedError`` when the validator returns FAILED. No later
    stage runs in either case.
    """
    opts = options or AnalysisOptions()
    settings = get_settings()
    llm_enabled = settings.llm_enabled if opts.llm_enabled is None else opts.llm_enabled
    embeddings = (
        settings.embeddings_enabled if opts.embeddings_enabled is None else opts.embeddings_enabled
    )
    file_hash = hashlib.sha1(data).hexdigest()[:8]
    stages: list[str] = []

    with run_context() as run_id:
        log.info("analysis start", extra={"input_hash": file_hash, "input_bytes": len(data)})
        # Adapters take a path, so the bytes go to a temp file (same naming as
        # the pre-Phase-1 runner). It is removed as soon as extraction is done.
        with tempfile.TemporaryDirectory() as tmp:
            # Basename only: a caller-supplied "../x.pdf" must not escape the temp dir.
            # Plain names are unchanged, so transaction IDs (which hash the stem) are too.
            in_path = Path(tmp) / f"{file_hash}_{Path(filename).name}"
            in_path.write_bytes(data)
            adapter_name = opts.adapter or select_adapter_name(in_path)
            with stage_timer("ingest", adapter=adapter_name) as ctx:
                raw = make_adapter(adapter_name).extract(in_path)
                ctx["rows"] = len(raw.rows)
        stages.append("ingest")

        with stage_timer("normalise") as ctx:
            doc = Normaliser().normalise(raw)
            ctx["txns"] = len(doc.transactions)
        stages.append("normalise")

        with stage_timer("validate") as ctx:
            validation_report = Validator().validate(doc)
            ctx["status"] = str(validation_report.status)
            ctx["issues"] = len(validation_report.issues)
        stages.append("validate")
        if validation_report.status == ValidationStatus.FAILED:
            raise ValidationFailedError(validation_report)

        with stage_timer("categorise") as ctx:
            doc, cat_stats = Categoriser(llm_enabled=llm_enabled).categorise(doc)
            ctx["rule"] = cat_stats.rule_matched
            ctx["llm"] = cat_stats.llm_matched
            ctx["other"] = cat_stats.fallback_other
        stages.append("categorise")

        with stage_timer("financial_analysis"):
            metrics = FinancialAnalyst().analyse(doc)
        stages.append("financial_analysis")

        with stage_timer("related_party"):
            doc = RelatedPartyTagger(llm_enabled=opts.related_party_llm).tag(
                doc, opts.affiliates or []
            )
        stages.append("related_party")

        with stage_timer("customer_identity"):
            doc = CustomerIdentityResolver(embedding_enabled=embeddings).resolve(doc)
        stages.append("customer_identity")

        with stage_timer("risk") as ctx:
            risk_report = RiskAnalyst(llm_enabled=llm_enabled).analyse(doc)
            ctx["flags"] = len(risk_report.flags)
        stages.append("risk")

        with stage_timer("customer_analytics"):
            customer_analytics = CustomerAnalyticsAnalyst().analyse(doc)
        stages.append("customer_analytics")

        with stage_timer("financial_health") as ctx:
            health_report = FinancialHealthAnalyst(llm_enabled=llm_enabled).analyse(
                metrics, customer_analytics
            )
            ctx["alerts"] = len(health_report.alerts)
        stages.append("financial_health")

        with stage_timer("compliance") as ctx:
            compliance_report = ComplianceAnalyst(llm_enabled=llm_enabled).analyse(
                doc, IndiaJurisdictionModule()
            )
            ctx["exceptions"] = len(compliance_report.exceptions)
        stages.append("compliance")

        with stage_timer("reconciliation") as ctx:
            reconciliation_report = ReconciliationAnalyst(llm_enabled=llm_enabled).analyse(
                doc, opts.claims, customer_analytics, metrics
            )
            ctx["findings"] = len(reconciliation_report.findings)
        stages.append("reconciliation")

        cname = opts.company_name or f"Account {doc.account_id}"
        with stage_timer("narrative"):
            narrative = _narrative(
                cname, metrics, risk_report, customer_analytics, health_report,
                compliance_report, reconciliation_report, llm_enabled,
            )
        stages.append("narrative")

        log.info("analysis done", extra={"txns": len(doc.transactions), "stages": len(stages)})

    return AnalysisResult(
        doc=doc,
        validation_report=validation_report,
        categorisation_stats=cat_stats,
        metrics=metrics,
        risk_report=risk_report,
        customer_analytics=customer_analytics,
        health_report=health_report,
        compliance_report=compliance_report,
        reconciliation_report=reconciliation_report,
        narrative=narrative,
        company_name=cname,
        adapter_name=adapter_name,
        run_id=run_id,
        stage_names=stages,
    )


def _narrative(
    cname: str,
    metrics: FinancialMetrics,
    risk_report: RiskReport,
    customer_analytics: CustomerAnalyticsReport,
    health_report: FinancialHealthReport,
    compliance_report: ComplianceReport,
    reconciliation_report: ReconciliationReport,
    llm_enabled: bool,
) -> str:
    active_counts = list(customer_analytics.monthly_active_customers.values())
    nrr_vals = list(customer_analytics.nrr_per_month.values())
    return generate_report_narrative(ReportNarrativeInput(
        company_name=cname,
        total_revenue=float(metrics.total_revenue),
        avg_monthly_burn=float(metrics.avg_monthly_burn),
        runway_months=float(metrics.runway_months) if metrics.runway_months is not None else None,
        avg_mom_growth=float(metrics.avg_mom_growth) if metrics.avg_mom_growth is not None else None,
        active_customers=active_counts[-1] if active_counts else None,
        latest_nrr=nrr_vals[-1] if nrr_vals else None,
        risk_flag_counts=severity_counts(risk_report.flags),
        health_alert_counts=severity_counts(health_report.alerts),
        compliance_exception_counts=severity_counts(compliance_report.exceptions),
        reconciliation_finding_counts=(
            severity_counts(reconciliation_report.findings)
            if reconciliation_report.claims_provided else None
        ),
        top_risk_descriptions=[f.description for f in risk_report.flags[:5]],
        top_health_descriptions=[a.description for a in health_report.alerts[:5]],
    ), llm_enabled=llm_enabled)
