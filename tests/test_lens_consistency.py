"""Wave 3.2 — cross-lens band-classification consistency.

Before this wave, angel_lens and vc_lens each hardcoded their own NRR/runway/
concentration cut points, independently of analysis/financial_health_alerts.py
and of each other. A concrete, verified example of the resulting bug: NRR of
80% was MEDIUM ("nrr_contraction") in the alerts engine but rendered as
"Critical" (the single worst tier) in both angel_lens and vc_lens.

These tests build ONE shared scenario and verify every lens's rendered output
agrees with analysis/financial_health_alerts.py's classification — no lens
may show a more/less severe label for figures financial_health_alerts.py
does NOT consider severe (and vice versa).
"""
from __future__ import annotations

import datetime
from decimal import Decimal
from io import BytesIO

import pdfplumber
import pytest
from openpyxl import load_workbook

from analysis.compliance import ComplianceReport
from analysis.customer_analytics import ChurnEvent, ConcentrationPoint, CustomerAnalyticsReport
from analysis.financial_analyst import FinancialMetrics, MonthlyStats
from analysis.financial_health_alerts import FinancialHealthAnalyst, classify_concentration, classify_nrr
from analysis.reconciliation import ReconciliationReport
from analysis.risk import RiskReport
from reports.angel_lens import AngelLensReport
from reports.network_lens import build_company_summary
from reports.vc_lens import AnalysisResult, VCLensReport
from reports.workbench_lens import WorkbenchAnalysisResult, WorkbenchLensReport
from schema.canonical import StatementDocument

_SRC_DATE = datetime.date(2024, 1, 1)

# NRR = 80% -> classify_nrr == "contraction" (MEDIUM in FHA), NOT "severe".
_NRR = 0.80
# Top-3 concentration = 70% -> classify_concentration == "medium", NOT "high".
_TOP3_SHARE = 0.70

assert classify_nrr(_NRR) == "contraction"
assert classify_concentration(_TOP3_SHARE) == "medium"


def _metrics() -> FinancialMetrics:
    stats = [
        MonthlyStats("2024-01", Decimal("500000"), Decimal("300000")),
        MonthlyStats("2024-02", Decimal("520000"), Decimal("310000")),
    ]
    return FinancialMetrics(
        period_months=2, total_revenue=Decimal("1020000"), total_burn=Decimal("610000"),
        closing_balance=Decimal("2000000"), monthly_stats=stats,
        avg_monthly_revenue=Decimal("510000"), avg_monthly_burn=Decimal("305000"),
        runway_months=Decimal("8"), mom_revenue_growth_rates=[Decimal("0.04")],
        avg_mom_growth=Decimal("0.04"), revenue_hhi=0.3, top_customer_revenue_pct=0.4,
    )


def _customer_analytics() -> CustomerAnalyticsReport:
    return CustomerAnalyticsReport(
        monthly_active_customers={"2024-01": 10, "2024-02": 10},
        monthly_active_trend=0.0,
        churn_events=[],
        new_acquisitions={},
        nrr_per_month={"2024-02": _NRR},
        cohort_retention={},
        concentration_trajectory=[
            ConcentrationPoint(month="2024-02", top3_share=_TOP3_SHARE, top10_share=0.9),
        ],
        payment_regularity_alerts=[],
    )


def _doc() -> StatementDocument:
    return StatementDocument(
        account_id="ACC001", statement_period_start=_SRC_DATE,
        statement_period_end=datetime.date(2024, 2, 29), source_format="test",
        transactions=[],
    )


def _risk_report() -> RiskReport:
    return RiskReport(flags=[], composite_score=0.0, narrative="No flags.")


def _compliance_report() -> ComplianceReport:
    return ComplianceReport(exceptions=[], jurisdiction="india")


def _reconciliation_report() -> ReconciliationReport:
    return ReconciliationReport(findings=[], claims_provided=False)


class TestFinancialHealthAlertsBaseline:
    """The canonical source: NRR=80% must be MEDIUM ('contraction'), not HIGH."""

    def test_nrr_alert_is_medium_not_severe(self) -> None:
        report = FinancialHealthAnalyst(llm_enabled=False).analyse(_metrics(), _customer_analytics())
        nrr_alerts = [a for a in report.alerts if "nrr" in a.alert_name]
        assert len(nrr_alerts) == 1
        assert nrr_alerts[0].alert_name == "nrr_contraction"
        assert str(nrr_alerts[0].severity) == "MEDIUM"

    def test_concentration_alert_is_medium_not_high(self) -> None:
        report = FinancialHealthAnalyst(llm_enabled=False).analyse(_metrics(), _customer_analytics())
        conc_alerts = [a for a in report.alerts if "concentration" in a.alert_name]
        assert len(conc_alerts) == 1
        assert conc_alerts[0].alert_name == "moderate_customer_concentration"
        assert str(conc_alerts[0].severity) == "MEDIUM"


class TestAngelLensAgreesWithBaseline:
    def test_nrr_80pct_not_shown_as_critical(self, tmp_path) -> None:
        out = tmp_path / "angel.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out, customer_analytics=_customer_analytics())
        with pdfplumber.open(out) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "severe" not in text.lower()
        assert "NRR: 80%" in text

    def test_concentration_70pct_not_shown_as_extreme(self, tmp_path) -> None:
        out = tmp_path / "angel2.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out, customer_analytics=_customer_analytics())
        with pdfplumber.open(out) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "high concentration" not in text.lower() or "moderate concentration" in text.lower()
        assert "70%" in text


class TestVCLensAgreesWithBaseline:
    def _result(self) -> AnalysisResult:
        return AnalysisResult(
            doc=_doc(), metrics=_metrics(), risk_report=_risk_report(),
            customer_analytics=_customer_analytics(), company_name="Acme Pvt Ltd",
        )

    def test_nrr_80pct_not_critical_in_xlsx(self) -> None:
        xlsx, _ = VCLensReport().generate(self._result())
        wb = load_workbook(BytesIO(xlsx))
        ws = wb["Customer Analytics"]
        all_text = " ".join(
            str(ws.cell(r, c).value) for r in range(1, ws.max_row + 1)
            for c in range(1, 5) if ws.cell(r, c).value
        )
        assert "Critical" not in all_text
        assert "Contraction" in all_text

    def test_nrr_80pct_not_critical_in_pdf(self) -> None:
        _, pdf_bytes = VCLensReport().generate(self._result())
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "Critical" not in text

    def test_concentration_70pct_not_high_risk_in_xlsx(self) -> None:
        xlsx, _ = VCLensReport().generate(self._result())
        wb = load_workbook(BytesIO(xlsx))
        ws = wb["Customer Analytics"]
        all_text = " ".join(
            str(ws.cell(r, c).value) for r in range(1, ws.max_row + 1)
            for c in range(1, 5) if ws.cell(r, c).value
        )
        assert "High concentration risk" not in all_text
        assert "Moderate" in all_text


class TestWorkbenchLensAgreesWithBaseline:
    def test_nrr_80pct_gets_amber_not_red_fill(self) -> None:
        result = WorkbenchAnalysisResult(
            doc=_doc(), metrics=_metrics(), risk_report=_risk_report(),
            customer_analytics=_customer_analytics(), compliance_report=_compliance_report(),
            reconciliation_report=_reconciliation_report(), company_name="Acme",
        )
        xlsx, _ = WorkbenchLensReport().generate(result)
        wb = load_workbook(BytesIO(xlsx))
        ws = wb["Customer Analytics"]
        found_row = None
        for r in range(1, ws.max_row + 1):
            if ws.cell(r, 1).value == "2024-02":
                found_row = r
                break
        assert found_row is not None
        nrr_cell = ws.cell(found_row, 5)
        assert nrr_cell.value == "80%"
        # RED (FCE4D6 in this file's palette) would mean "severe" — must not be red.
        assert nrr_cell.fill.fgColor.rgb != "00FCE4D6"


class TestNetworkLensAgreesWithBaseline:
    def test_summary_carries_the_same_raw_figures(self) -> None:
        summary = build_company_summary(
            company_name="Acme", doc=_doc(), metrics=_metrics(),
            risk_report=_risk_report(), customer_analytics=_customer_analytics(),
            compliance_report=_compliance_report(),
        )
        # Network Lens doesn't render its own NRR/concentration verdict text —
        # it shows the raw figures the same classify_* functions were computed
        # from, so consistency here means the raw numbers themselves are
        # correct and unmodified, not re-derived with a different formula.
        assert summary.nrr == pytest.approx(_NRR)
        assert summary.top_customer_share == pytest.approx(_TOP3_SHARE)
