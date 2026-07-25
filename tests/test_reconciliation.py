"""Tests for analysis/reconciliation.py."""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from analysis.customer_analytics import CustomerAnalyticsAnalyst, CustomerAnalyticsReport, ChurnEvent
from analysis.financial_analyst import FinancialAnalyst, FinancialMetrics, MonthlyStats
from analysis.reconciliation import (
    CompanyClaims,
    MismatchDirection,
    ReconciliationAnalyst,
    ReconciliationReport,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)

_SRC = SourceReference(file_path="t.pdf", page=0, row=0, raw_text="")


def _txn(tid, *, date=None, debit=None, credit=None, cat=None, cid=None, desc="TXN"):
    d = date or datetime.date(2024, 1, 15)
    kwargs = dict(
        transaction_id=tid, date=d, description=desc,
        balance=Decimal("0"), source_reference=_SRC, category=cat, customer_id=cid,
    )
    if debit is not None:
        kwargs["debit"] = Decimal(str(debit))
    else:
        kwargs["credit"] = Decimal(str(credit or 0))
    return CanonicalTransaction(**kwargs)


def _empty_ca() -> CustomerAnalyticsReport:
    return CustomerAnalyticsReport(
        monthly_active_customers={},
        monthly_active_trend=0.0,
        churn_events=[],
        new_acquisitions={},
        nrr_per_month={},
        cohort_retention={},
        concentration_trajectory=[],
        payment_regularity_alerts=[],
    )


def _metrics(revenue: float = 1_000_000, burn: float = 500_000,
             period_months: int = 3) -> FinancialMetrics:
    return FinancialMetrics(
        total_revenue=Decimal(str(revenue)),
        total_burn=Decimal(str(burn)),
        avg_monthly_revenue=Decimal(str(revenue / period_months)),
        avg_monthly_burn=Decimal(str(burn / period_months)),
        runway_months=Decimal("6"),
        period_months=period_months,
        mom_revenue_growth_rates=[],
        avg_mom_growth=None,
        revenue_hhi=0.1,
        top_customer_revenue_pct=0.1,
        monthly_stats=[],
        closing_balance=Decimal("500000"),
    )


def _doc(*txns):
    return StatementDocument(
        account_id="test",
        statement_period_start=datetime.date(2024, 1, 1),
        statement_period_end=datetime.date(2024, 3, 31),
        source_format="test",
        transactions=list(txns),
    )


class TestNoClaims:
    def test_no_claims_returns_empty_report(self):
        analyst = ReconciliationAnalyst(llm_enabled=False)
        report = analyst.analyse(_doc(), None, _empty_ca(), _metrics())
        assert isinstance(report, ReconciliationReport)
        assert not report.claims_provided
        assert report.findings == []


class TestRevenueMismatch:
    def test_revenue_within_tolerance_no_finding(self):
        claims = CompanyClaims(declared_revenue_total=Decimal("1050000"))  # 5% over
        report = ReconciliationAnalyst(llm_enabled=False).analyse(
            _doc(), claims, _empty_ca(), _metrics(revenue=1_000_000)
        )
        assert not any(f.check_name == "Total Revenue" for f in report.findings)

    def test_revenue_over_reported_finding(self):
        claims = CompanyClaims(declared_revenue_total=Decimal("1500000"))  # 50% over
        report = ReconciliationAnalyst(llm_enabled=False).analyse(
            _doc(), claims, _empty_ca(), _metrics(revenue=1_000_000)
        )
        rev_findings = [f for f in report.findings if f.check_name == "Total Revenue"]
        assert len(rev_findings) == 1
        assert rev_findings[0].direction == MismatchDirection.OVER_REPORTED
        assert rev_findings[0].severity == "HIGH"

    def test_revenue_under_reported_finding(self):
        claims = CompanyClaims(declared_revenue_total=Decimal("500000"))  # 50% under
        report = ReconciliationAnalyst(llm_enabled=False).analyse(
            _doc(), claims, _empty_ca(), _metrics(revenue=1_000_000)
        )
        rev_findings = [f for f in report.findings if f.check_name == "Total Revenue"]
        assert len(rev_findings) == 1
        assert rev_findings[0].direction == MismatchDirection.UNDER_REPORTED


class TestCustomerCountMismatch:
    def test_customer_count_within_tolerance_no_finding(self):
        ca = _empty_ca()
        ca.monthly_active_customers = {"2024-01": 45}
        claims = CompanyClaims(declared_customer_count=48)  # ~7% over
        report = ReconciliationAnalyst(llm_enabled=False).analyse(_doc(), claims, ca, _metrics())
        assert not any(f.check_name == "Customer Count" for f in report.findings)

    def test_customer_count_over_reported(self):
        ca = _empty_ca()
        ca.monthly_active_customers = {"2024-01": 20}
        claims = CompanyClaims(declared_customer_count=45)  # 125% over
        report = ReconciliationAnalyst(llm_enabled=False).analyse(_doc(), claims, ca, _metrics())
        cc = [f for f in report.findings if f.check_name == "Customer Count"]
        assert len(cc) == 1
        assert cc[0].direction == MismatchDirection.OVER_REPORTED


class TestNeverLostCustomer:
    def test_no_churn_no_finding(self):
        ca = _empty_ca()  # no churn events
        claims = CompanyClaims(declared_never_lost_customer=True)
        report = ReconciliationAnalyst(llm_enabled=False).analyse(_doc(), claims, ca, _metrics())
        assert not any("Customer" in f.check_name and "Never" in f.check_name for f in report.findings)

    def test_churn_contradicts_claim(self):
        ca = _empty_ca()
        ca.churn_events = [
            ChurnEvent(customer_id="A", last_payment_date=datetime.date(2024, 1, 15),
                       previous_avg_monthly_spend=Decimal("50000"), months_active_before_churn=3)
        ]
        claims = CompanyClaims(declared_never_lost_customer=True)
        report = ReconciliationAnalyst(llm_enabled=False).analyse(_doc(), claims, ca, _metrics())
        findings = [f for f in report.findings if "Never" in f.check_name or "never" in f.check_name.lower() or "Customer" in f.check_name and "Lost" in f.check_name]
        never_lost = [f for f in report.findings if "never" in f.check_name.lower() or "Never Lost" in f.check_name]
        # Either naming convention
        all_checks = [f.check_name for f in report.findings]
        assert any("Customer" in c for c in all_checks)
        high_findings = [f for f in report.findings if f.severity == "HIGH"]
        assert high_findings  # should be HIGH severity


class TestReconciliationReport:
    def test_returns_report_type(self):
        claims = CompanyClaims(declared_revenue_total=Decimal("1000000"))
        report = ReconciliationAnalyst(llm_enabled=False).analyse(
            _doc(), claims, _empty_ca(), _metrics()
        )
        assert isinstance(report, ReconciliationReport)
        assert report.claims_provided is True

    def test_nrr_pct_auto_detection(self):
        """declared_nrr=110 should be auto-detected as 110% → 1.10."""
        ca = _empty_ca()
        ca.nrr_per_month = {"2024-02": 0.50}  # 50% actual
        claims = CompanyClaims(declared_nrr=110)  # 110% claimed
        report = ReconciliationAnalyst(llm_enabled=False).analyse(_doc(), claims, ca, _metrics())
        nrr_findings = [f for f in report.findings if f.check_name == "Net Revenue Retention"]
        assert len(nrr_findings) == 1
        assert nrr_findings[0].direction == MismatchDirection.OVER_REPORTED
