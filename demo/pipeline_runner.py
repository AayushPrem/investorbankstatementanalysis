"""Testable pipeline logic for the demo — no Streamlit dependency.

The analysis itself is :func:`pipeline.run.run_analysis`, the single canonical
entry point. This module adds report rendering (angel lens PDF, VC lens
XLSX/PDF, workbench lens XLSX/PDF) and packages everything as a DemoResult.

  bytes → run_analysis (adapter → … → narrative)
        → angel lens PDF + VC lens XLSX/PDF + workbench lens XLSX/PDF
"""
from __future__ import annotations

import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import workbench_config

# Re-exported: tests monkeypatch methods on these classes via this module
# (e.g. pipeline_runner.Validator.validate), and run_analysis uses the same classes.
from analysis.categoriser import Categoriser  # noqa: F401
from analysis.compliance import ComplianceReport
from analysis.customer_analytics import CustomerAnalyticsReport
from analysis.financial_analyst import FinancialMetrics
from analysis.financial_health_alerts import FinancialHealthReport
from analysis.reconciliation import CompanyClaims, ReconciliationReport
from analysis.risk import RiskReport
from pipeline.related_party import Affiliate
from pipeline.run import AnalysisOptions, AnalysisResult, run_analysis
from pipeline.validator import ValidationFailedError, Validator  # noqa: F401
from reports.angel_lens import AngelLensReport
from reports.vc_lens import AnalysisResult as VCAnalysisResult
from reports.vc_lens import VCLensReport
from reports.workbench_lens import WorkbenchAnalysisResult, WorkbenchLensReport
from schema.canonical import StatementDocument


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


@dataclass
class RenderedReports:
    """Every lens rendered for one analysis (bytes only)."""

    angel_pdf: bytes
    vc_xlsx: bytes
    vc_pdf: bytes
    workbench_xlsx: bytes
    workbench_pdf: bytes


def render_reports(analysis: AnalysisResult) -> RenderedReports:
    """Render the Angel/VC/Workbench lenses for a finished analysis.

    Split out of ``run_pipeline_on_bytes`` in Phase 2 so the job worker can
    render (and store) the same reports. The rendering itself is unchanged.
    """
    doc, metrics = analysis.doc, analysis.metrics
    risk_report, customer_analytics = analysis.risk_report, analysis.customer_analytics
    health_report, narrative, cname = analysis.health_report, analysis.narrative, analysis.company_name

    with tempfile.TemporaryDirectory() as tmp:
        angel_path = Path(tmp) / "angel_report.pdf"
        AngelLensReport().generate(
            doc, metrics, angel_path,
            company_name=cname,
            risk_report=risk_report,
            customer_analytics=customer_analytics,
            health_report=health_report,
            narrative=narrative,
        )
        angel_bytes = angel_path.read_bytes()

    vc_result = VCAnalysisResult(
        doc=doc, metrics=metrics, risk_report=risk_report,
        customer_analytics=customer_analytics, company_name=cname,
        health_report=health_report, narrative=narrative,
    )
    vc_xlsx, vc_pdf = VCLensReport().generate(vc_result)

    workbench_risk_report = workbench_config.apply(doc, risk_report)
    workbench_result = WorkbenchAnalysisResult(
        doc=doc, metrics=metrics, risk_report=workbench_risk_report,
        customer_analytics=customer_analytics,
        compliance_report=analysis.compliance_report,
        reconciliation_report=analysis.reconciliation_report,
        company_name=cname,
        narrative=narrative,
    )
    wb_xlsx, wb_pdf = WorkbenchLensReport().generate(workbench_result)

    return RenderedReports(
        angel_pdf=angel_bytes,
        vc_xlsx=vc_xlsx,
        vc_pdf=vc_pdf,
        workbench_xlsx=wb_xlsx,
        workbench_pdf=wb_pdf,
    )


def run_pipeline_on_bytes(
    pdf_bytes: bytes,
    filename: str,
    affiliates: list[Affiliate] | None = None,
    company_name: str | None = None,
    claims: CompanyClaims | None = None,
) -> DemoResult:
    """Run the full pipeline on raw statement bytes and render all report lenses."""
    analysis = run_analysis(
        pdf_bytes, filename,
        options=AnalysisOptions(affiliates=affiliates, company_name=company_name, claims=claims),
    )
    reports = render_reports(analysis)
    return DemoResult(
        doc=analysis.doc,
        metrics=analysis.metrics,
        risk_report=analysis.risk_report,
        customer_analytics=analysis.customer_analytics,
        health_report=analysis.health_report,
        compliance_report=analysis.compliance_report,
        reconciliation_report=analysis.reconciliation_report,
        narrative=analysis.narrative,
        angel_report_bytes=reports.angel_pdf,
        vc_xlsx_bytes=reports.vc_xlsx,
        vc_pdf_bytes=reports.vc_pdf,
        workbench_xlsx_bytes=reports.workbench_xlsx,
        workbench_pdf_bytes=reports.workbench_pdf,
    )
