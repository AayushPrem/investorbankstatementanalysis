"""Testable pipeline logic for the demo — no Streamlit dependency.

Sprint 1+2+3 pipeline:
  PDF → adapter → normaliser → validator → categoriser
      → related-party tagger → customer identity resolver
      → risk analyst → customer analytics analyst
      → compliance analyst → reconciliation analyst
      → angel lens PDF + VC lens XLSX/PDF + workbench lens XLSX/PDF
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import workbench_config
from adapters.digital_pdf import DigitalPDFAdapter
from analysis.categoriser import Categoriser
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
from jurisdictions.india import IndiaJurisdictionModule
from pipeline.customer_identity import CustomerIdentityResolver
from pipeline.normaliser import Normaliser
from pipeline.related_party import Affiliate, RelatedPartyTagger
from pipeline.validator import ValidationFailedError, Validator
from reports.angel_lens import AngelLensReport
from reports.vc_lens import AnalysisResult, VCLensReport
from reports.workbench_lens import WorkbenchAnalysisResult, WorkbenchLensReport
from schema.canonical import StatementDocument, ValidationStatus


@dataclass
class DemoResult:
    doc: StatementDocument
    metrics: FinancialMetrics
    risk_report: RiskReport
    customer_analytics: CustomerAnalyticsReport
    health_report: FinancialHealthReport
    compliance_report: ComplianceReport
    reconciliation_report: ReconciliationReport
    narrative: str
    angel_report_bytes: bytes
    vc_xlsx_bytes: bytes
    vc_pdf_bytes: bytes
    workbench_xlsx_bytes: bytes
    workbench_pdf_bytes: bytes
    # backward-compat alias
    report_bytes: bytes = field(init=False)

    def __post_init__(self) -> None:
        self.report_bytes = self.angel_report_bytes


def run_pipeline_on_bytes(
    pdf_bytes: bytes,
    filename: str,
    affiliates: list[Affiliate] | None = None,
    company_name: str | None = None,
    claims: CompanyClaims | None = None,
) -> DemoResult:
    """Run the full Sprint 1+2+3 pipeline on raw PDF bytes."""
    file_hash = hashlib.sha1(pdf_bytes).hexdigest()[:8]

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pdf_path = tmp_path / f"{file_hash}_{filename}"
        pdf_path.write_bytes(pdf_bytes)

        # ── Sprint 1 stages ──────────────────────────────────────────────
        raw = DigitalPDFAdapter().extract(pdf_path)
        doc = Normaliser().normalise(raw)
        validation_report = Validator().validate(doc)
        if validation_report.status == ValidationStatus.FAILED:
            raise ValidationFailedError(validation_report)
        doc, _ = Categoriser(llm_enabled=True).categorise(doc)
        metrics = FinancialAnalyst().analyse(doc)

        # ── Sprint 2 stages ──────────────────────────────────────────────
        doc = RelatedPartyTagger(llm_enabled=False).tag(doc, affiliates or [])
        doc = CustomerIdentityResolver(embedding_enabled=False).resolve(doc)
        risk_report = RiskAnalyst().analyse(doc)
        customer_analytics = CustomerAnalyticsAnalyst().analyse(doc)
        health_report = FinancialHealthAnalyst().analyse(metrics, customer_analytics)

        # ── Sprint 3 stages ──────────────────────────────────────────────
        compliance_report = ComplianceAnalyst().analyse(doc, IndiaJurisdictionModule())
        reconciliation_report = ReconciliationAnalyst().analyse(
            doc, claims, customer_analytics, metrics
        )

        # ── Reports ──────────────────────────────────────────────────────
        cname = company_name or f"Account {doc.account_id}"

        active_counts = list(customer_analytics.monthly_active_customers.values())
        nrr_vals = list(customer_analytics.nrr_per_month.values())
        narrative = generate_report_narrative(ReportNarrativeInput(
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
        ))

        angel_path = tmp_path / "angel_report.pdf"
        AngelLensReport().generate(
            doc, metrics, angel_path,
            company_name=cname,
            risk_report=risk_report,
            customer_analytics=customer_analytics,
            health_report=health_report,
            narrative=narrative,
        )
        angel_bytes = angel_path.read_bytes()

        analysis_result = AnalysisResult(
            doc=doc, metrics=metrics, risk_report=risk_report,
            customer_analytics=customer_analytics, company_name=cname,
            health_report=health_report, narrative=narrative,
        )
        vc_xlsx, vc_pdf = VCLensReport().generate(analysis_result)

        workbench_risk_report = workbench_config.apply(doc, risk_report)
        workbench_result = WorkbenchAnalysisResult(
            doc=doc, metrics=metrics, risk_report=workbench_risk_report,
            customer_analytics=customer_analytics,
            compliance_report=compliance_report,
            reconciliation_report=reconciliation_report,
            company_name=cname,
            narrative=narrative,
        )
        wb_xlsx, wb_pdf = WorkbenchLensReport().generate(workbench_result)

    return DemoResult(
        doc=doc,
        metrics=metrics,
        risk_report=risk_report,
        customer_analytics=customer_analytics,
        health_report=health_report,
        compliance_report=compliance_report,
        reconciliation_report=reconciliation_report,
        narrative=narrative,
        angel_report_bytes=angel_bytes,
        vc_xlsx_bytes=vc_xlsx,
        vc_pdf_bytes=vc_pdf,
        workbench_xlsx_bytes=wb_xlsx,
        workbench_pdf_bytes=wb_pdf,
    )
