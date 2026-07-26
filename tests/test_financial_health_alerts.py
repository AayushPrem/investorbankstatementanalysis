"""Tests for analysis/financial_health_alerts.py — currently scoped to the
Wave 3.1 aggregator-dominance alert only (no test file existed for this
module before; a full pass over all 13 pre-existing alert types is out of
scope here — see the Wave 3 checkpoint notes)."""
from __future__ import annotations

import datetime
from decimal import Decimal

from analysis.customer_analytics import CustomerAnalyticsAnalyst
from analysis.financial_analyst import FinancialMetrics
from analysis.financial_health_alerts import FinancialHealthAnalyst
from pipeline.customer_identity import ANOMALY_FLAG_AGGREGATOR_SETTLEMENT
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)

_SRC = SourceReference(file_path="test.pdf", page=0, row=0, raw_text="")


def _txn(tid, *, credit=None, customer_id=None, month=1, anomaly_flags=None):
    return CanonicalTransaction(
        transaction_id=tid, date=datetime.date(2024, month, 15), description=tid,
        credit=Decimal(str(credit)), balance=Decimal("0"), source_reference=_SRC,
        category=TransactionCategory.REVENUE, customer_id=customer_id,
        anomaly_flags=anomaly_flags or [],
    )


def _doc(*txns):
    return StatementDocument(
        account_id="test", statement_period_start=datetime.date(2024, 1, 1),
        statement_period_end=datetime.date(2024, 3, 31), source_format="test",
        transactions=list(txns),
    )


def _metrics() -> FinancialMetrics:
    return FinancialMetrics(
        total_revenue=Decimal("1000000"), total_burn=Decimal("500000"),
        avg_monthly_revenue=Decimal("333333"), avg_monthly_burn=Decimal("166666"),
        runway_months=Decimal("12"), period_months=3,
        mom_revenue_growth_rates=[], avg_mom_growth=None,
        revenue_hhi=0.1, top_customer_revenue_pct=0.1, monthly_stats=[],
        closing_balance=Decimal("2000000"),
    )


class TestAggregatorDominatedAlert:
    def test_fires_when_dominated(self) -> None:
        doc = _doc(
            _txn("t1", credit=40000, customer_id="A", month=1),
            _txn("t2", credit=60000, anomaly_flags=[ANOMALY_FLAG_AGGREGATOR_SETTLEMENT], month=1),
        )
        ca = CustomerAnalyticsAnalyst().analyse(doc)
        report = FinancialHealthAnalyst(llm_enabled=False).analyse(_metrics(), ca)
        names = [a.alert_name for a in report.alerts]
        assert "aggregator_dominated_revenue" in names

    def test_does_not_fire_when_not_dominated(self) -> None:
        doc = _doc(
            _txn("t1", credit=95000, customer_id="A", month=1),
            _txn("t2", credit=5000, anomaly_flags=[ANOMALY_FLAG_AGGREGATOR_SETTLEMENT], month=1),
        )
        ca = CustomerAnalyticsAnalyst().analyse(doc)
        report = FinancialHealthAnalyst(llm_enabled=False).analyse(_metrics(), ca)
        names = [a.alert_name for a in report.alerts]
        assert "aggregator_dominated_revenue" not in names

    def test_alert_is_high_severity_with_pct_in_text(self) -> None:
        doc = _doc(
            _txn("t1", credit=30000, customer_id="A", month=1),
            _txn("t2", credit=70000, anomaly_flags=[ANOMALY_FLAG_AGGREGATOR_SETTLEMENT], month=1),
        )
        ca = CustomerAnalyticsAnalyst().analyse(doc)
        report = FinancialHealthAnalyst(llm_enabled=False).analyse(_metrics(), ca)
        alert = next(a for a in report.alerts if a.alert_name == "aggregator_dominated_revenue")
        assert str(alert.severity) == "HIGH"
        assert "70%" in alert.description
