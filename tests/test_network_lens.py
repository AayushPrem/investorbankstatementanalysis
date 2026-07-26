"""Tests for reports/network_lens.py (Sprint 4, Step 4.1)."""
from __future__ import annotations

import datetime
from decimal import Decimal
from io import BytesIO

import pdfplumber
import pytest
from openpyxl import load_workbook

from analysis.compliance import ComplianceReport
from analysis.customer_analytics import (
    ChurnEvent,
    ConcentrationPoint,
    CustomerAnalyticsReport,
)
from analysis.financial_analyst import FinancialMetrics, MonthlyStats
from analysis.risk import Flag, RiskReport, Severity
from reports.network_lens import CompanySummary, NetworkLensReport, build_company_summary
from schema.canonical import StatementDocument

_DATE_START = datetime.date(2024, 1, 1)
_DATE_END = datetime.date(2024, 6, 30)


def _metrics(burn: str = "500000", revenue: str = "800000", runway: str | None = "12.0") -> FinancialMetrics:
    return FinancialMetrics(
        period_months=6,
        total_revenue=Decimal(revenue) * 6, total_burn=Decimal(burn) * 6,
        closing_balance=Decimal("6000000"),
        monthly_stats=[MonthlyStats(year_month="2024-01", revenue=Decimal(revenue), burn=Decimal(burn))],
        avg_monthly_revenue=Decimal(revenue), avg_monthly_burn=Decimal(burn),
        runway_months=Decimal(runway) if runway else None,
        mom_revenue_growth_rates=[Decimal("0.05")], avg_mom_growth=Decimal("0.05"),
        revenue_hhi=0.2, top_customer_revenue_pct=0.3,
    )


def _risk_report(score: float = 10.0, n_flags: int = 1) -> RiskReport:
    flags = [
        Flag(detector_name="structuring", severity=Severity.LOW,
             triggering_transaction_ids=["t1"], description="test flag")
        for _ in range(n_flags)
    ]
    return RiskReport(flags=flags, composite_score=score, narrative="test narrative")


def _customer_analytics(active: int = 20, churn: int = 2) -> CustomerAnalyticsReport:
    return CustomerAnalyticsReport(
        monthly_active_customers={"2024-05": active, "2024-06": active},
        monthly_active_trend=0.5,
        churn_events=[
            ChurnEvent(customer_id=f"cust_{i}", last_payment_date=_DATE_START,
                       previous_avg_monthly_spend=Decimal("10000"), months_active_before_churn=4)
            for i in range(churn)
        ],
        new_acquisitions={}, nrr_per_month={"2024-06": 1.05},
        cohort_retention={}, concentration_trajectory=[
            ConcentrationPoint(month="2024-06", top3_share=0.4, top10_share=0.7),
        ],
        payment_regularity_alerts=[],
    )


def _compliance_report(high: int = 0) -> ComplianceReport:
    from jurisdictions import ComplianceException

    exceptions = [
        ComplianceException(
            rule_id="cash_transaction_limit_269st", rule_name="269ST", regulatory_citation="§269ST",
            severity=Severity.HIGH, description="test", investor_risk_framing="test",
            triggering_transaction_ids=["t1"],
        )
        for _ in range(high)
    ]
    return ComplianceReport(exceptions=exceptions, jurisdiction="india")


def _doc() -> StatementDocument:
    return StatementDocument(
        account_id="acc1", statement_period_start=_DATE_START, statement_period_end=_DATE_END,
        source_format="test", transactions=[],
    )


def _summary(name: str, risk: float = 10.0, sector: str | None = None,
             high_compliance: int = 0, detail_report_path: str | None = None) -> CompanySummary:
    return build_company_summary(
        company_name=name, doc=_doc(), metrics=_metrics(),
        risk_report=_risk_report(score=risk), customer_analytics=_customer_analytics(),
        compliance_report=_compliance_report(high=high_compliance),
        sector=sector, detail_report_path=detail_report_path,
    )


class TestBuildCompanySummary:
    def test_flattens_all_fields(self) -> None:
        s = _summary("Acme")
        assert s.company_name == "Acme"
        assert s.active_customers == 20
        assert s.churn_rate == 2 / 20
        assert s.nrr == 1.05
        assert s.top_customer_share == 0.4
        assert s.composite_risk_score == 10.0
        assert s.red_flag_count == 1

    def test_flattens_burn_revenue_and_runway(self) -> None:
        s = _summary("Acme")
        m = _metrics()
        assert s.monthly_burn == m.avg_monthly_burn
        assert s.monthly_revenue == m.avg_monthly_revenue
        assert s.runway_months == pytest.approx(float(m.runway_months))

    def test_compliance_status_clean(self) -> None:
        s = _summary("Acme", high_compliance=0)
        assert s.compliance_status == "clean"

    def test_compliance_status_violations(self) -> None:
        s = _summary("Acme", high_compliance=2)
        assert s.compliance_status == "violations"

    def test_compliance_status_unknown_when_no_report(self) -> None:
        s = build_company_summary(
            company_name="Acme", doc=_doc(), metrics=_metrics(),
            risk_report=_risk_report(), customer_analytics=_customer_analytics(),
        )
        assert s.compliance_status == "unknown"


class TestNetworkLensReport:
    def test_generates_valid_xlsx_and_pdf(self) -> None:
        summaries = [_summary("Acme", risk=10), _summary("Beta", risk=60)]
        xlsx_bytes, pdf_bytes = NetworkLensReport().generate(summaries)
        assert pdf_bytes.startswith(b"%PDF")
        wb = load_workbook(BytesIO(xlsx_bytes))
        assert wb.sheetnames == [
            "Comparison", "Risk & Compliance Detail", "Risk Distribution",
            "Sector Breakdown", "Company Reports",
        ]

    def test_comparison_sheet_has_one_row_per_company(self) -> None:
        summaries = [_summary("Acme"), _summary("Beta"), _summary("Gamma")]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Comparison"]
        names = [ws.cell(row=r, column=1).value for r in range(2, 5)]
        assert names == ["Acme", "Beta", "Gamma"]

    def test_comparison_sheet_shows_correct_burn_revenue_runway_values(self) -> None:
        """Wave 4.1 — verify the actual rendered cell values, not just row count."""
        from reports.network_lens import _inr

        s = _summary("Acme")
        xlsx_bytes, _ = NetworkLensReport().generate([s])
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Comparison"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        row = {headers[c - 1]: ws.cell(row=2, column=c).value for c in range(1, len(headers) + 1)}
        assert row["Monthly Burn"] == _inr(s.monthly_burn)
        assert row["Monthly Revenue"] == _inr(s.monthly_revenue)
        assert row["Runway (mo)"] == f"{s.runway_months:.1f}"
        assert row["Risk Score"] == f"{s.composite_risk_score:.0f}"

    def test_comparison_sheet_has_autofilter_table(self) -> None:
        summaries = [_summary("Acme"), _summary("Beta")]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Comparison"]
        assert "CompanyComparison" in ws.tables

    def test_risk_distribution_buckets_correctly(self) -> None:
        summaries = [_summary("Low", risk=5), _summary("Med", risk=30), _summary("High", risk=70)]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Risk Distribution"]
        counts = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value for r in range(2, 5)}
        assert counts["0-19 (Low)"] == 1
        assert counts["20-49 (Medium)"] == 1
        assert counts["50+ (High)"] == 1

    def test_sector_breakdown_groups_by_sector(self) -> None:
        summaries = [
            _summary("Acme", sector="SaaS"), _summary("Beta", sector="SaaS"),
            _summary("Gamma", sector="Fintech"),
        ]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Sector Breakdown"]
        rows = {ws.cell(row=r, column=1).value: ws.cell(row=r, column=2).value for r in range(2, 4)}
        assert rows["SaaS"] == 2
        assert rows["Fintech"] == 1

    def test_sector_breakdown_placeholder_when_no_sectors(self) -> None:
        summaries = [_summary("Acme"), _summary("Beta")]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Sector Breakdown"]
        assert "No sector metadata" in str(ws.cell(row=1, column=1).value)

    def test_company_links_sheet_includes_hyperlink(self) -> None:
        summaries = [_summary("Acme", detail_report_path="reports/acme.xlsx")]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Company Reports"]
        assert ws.cell(row=2, column=2).value == "reports/acme.xlsx"

    def test_empty_cohort_does_not_crash(self) -> None:
        xlsx_bytes, pdf_bytes = NetworkLensReport().generate([])
        assert pdf_bytes.startswith(b"%PDF")
        wb = load_workbook(BytesIO(xlsx_bytes))
        assert wb["Comparison"].max_row == 1  # header only

    def test_detail_sheet_contains_narrative_and_flag_rows(self) -> None:
        summaries = [_summary("Acme", risk=60)]  # _summary() gives it 1 HIGH-severity flag
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Risk & Compliance Detail"]

        assert ws["A1"].value == "PORTFOLIO NARRATIVE"
        assert isinstance(ws["A2"].value, str) and len(ws["A2"].value) > 0

        detail_rows = [
            [ws.cell(row=r, column=c).value for c in range(1, 6)]
            for r in range(7, ws.max_row + 1)
        ]
        assert any(row[0] == "Acme" and row[1] == "Risk Flag" for row in detail_rows)

    def test_detail_sheet_contains_compliance_rows(self) -> None:
        summaries = [_summary("Acme", high_compliance=1)]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Risk & Compliance Detail"]

        detail_rows = [
            [ws.cell(row=r, column=c).value for c in range(1, 6)]
            for r in range(7, ws.max_row + 1)
        ]
        assert any(row[1] == "Compliance" and "269ST" in str(row[3]) for row in detail_rows)

    def test_detail_sheet_placeholder_when_no_flags(self) -> None:
        from reports.network_lens import CompanySummary

        clean = CompanySummary(
            company_name="CleanCo", statement_period_start=_DATE_START,
            statement_period_end=_DATE_END, monthly_burn=Decimal("100000"),
            monthly_revenue=Decimal("200000"), runway_months=12.0, revenue_growth=0.05,
            active_customers=10, churn_rate=0.0, nrr=1.0, top_customer_share=0.2,
            composite_risk_score=0.0, red_flag_count=0, compliance_status="clean",
        )
        xlsx_bytes, _ = NetworkLensReport().generate([clean])
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Risk & Compliance Detail"]
        assert "No risk flags or compliance exceptions" in str(ws.cell(row=7, column=1).value)


class TestBuildPortfolioNarrative:
    def test_returns_non_empty_string(self) -> None:
        from reports.network_lens import build_portfolio_narrative

        summaries = [_summary("Acme", risk=10), _summary("Beta", risk=70)]
        narrative = build_portfolio_narrative(summaries, llm_enabled=False)
        assert isinstance(narrative, str)
        assert "Beta" in narrative  # high-risk company named by the fallback

    def test_empty_cohort(self) -> None:
        from reports.network_lens import build_portfolio_narrative

        assert build_portfolio_narrative([], llm_enabled=False) == "No companies in this cohort."


class TestCohortDashboardPDF:
    def test_high_risk_section_lists_companies_over_50(self) -> None:
        from reports.network_lens import _build_dashboard_pdf

        summaries = [_summary("SafeCo", risk=10), _summary("RiskyCo", risk=75)]
        pdf_bytes = _build_dashboard_pdf(summaries, narrative="Test narrative.")
        assert pdf_bytes.startswith(b"%PDF")

    def test_detail_page_added_when_flags_present(self) -> None:
        import re

        from reports.network_lens import _build_dashboard_pdf

        summaries = [_summary("Acme", risk=60)]
        pdf_bytes = _build_dashboard_pdf(summaries, narrative="Test narrative.")
        page_count = len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes))
        assert page_count >= 2  # dashboard page + risk/compliance detail page

    def test_percentile_helper(self) -> None:
        from reports.network_lens import _percentile

        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        assert _percentile(values, 0.5) == 30.0
        assert _percentile([], 0.5) == 0.0


class TestAggregatorCaveat:
    """Wave 3.1 — a company whose revenue is aggregator-dominated must be
    flagged in the comparison table and named in the portfolio PDF caveat."""

    def _dominated_summary(self, name: str) -> CompanySummary:
        ca = _customer_analytics()
        ca.aggregator_revenue_pct = 0.72
        return build_company_summary(
            company_name=name, doc=_doc(), metrics=_metrics(),
            risk_report=_risk_report(), customer_analytics=ca,
            compliance_report=_compliance_report(),
        )

    def test_company_summary_carries_aggregator_fields(self) -> None:
        s = self._dominated_summary("AggCo")
        assert s.aggregator_revenue_pct == pytest.approx(0.72)
        assert s.aggregator_dominated is True

    def test_not_dominated_by_default(self) -> None:
        s = _summary("Normal")
        assert s.aggregator_dominated is False
        assert s.aggregator_revenue_pct == 0.0

    def test_comparison_sheet_shows_aggregator_column(self) -> None:
        summaries = [_summary("Clean"), self._dominated_summary("AggCo")]
        xlsx_bytes, _ = NetworkLensReport().generate(summaries)
        wb = load_workbook(BytesIO(xlsx_bytes))
        ws = wb["Comparison"]
        headers = [ws.cell(row=1, column=c).value for c in range(1, ws.max_column + 1)]
        assert "Aggregator-Settled Rev." in headers
        col = headers.index("Aggregator-Settled Rev.") + 1
        agg_row_value = ws.cell(row=3, column=col).value  # AggCo is row 3
        assert "72" in str(agg_row_value)

    def test_dashboard_pdf_names_dominated_companies(self) -> None:
        from reports.network_lens import _build_dashboard_pdf

        summaries = [_summary("Clean"), self._dominated_summary("AggCo")]
        pdf_bytes = _build_dashboard_pdf(summaries, narrative="Test narrative.")
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "AggCo" in text
        assert "aggregator-settled" in text

    def test_dashboard_pdf_no_caveat_when_none_dominated(self) -> None:
        from reports.network_lens import _build_dashboard_pdf

        summaries = [_summary("Clean1"), _summary("Clean2")]
        pdf_bytes = _build_dashboard_pdf(summaries, narrative="Test narrative.")
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            text = "\n".join(p.extract_text() or "" for p in pdf.pages)
        assert "aggregator-settled" not in text
