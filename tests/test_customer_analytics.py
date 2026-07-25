"""Tests for analysis/customer_analytics.py — CustomerAnalyticsAnalyst."""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from analysis.customer_analytics import (
    CustomerAnalyticsAnalyst,
    CustomerAnalyticsReport,
    ChurnEvent,
    ConcentrationPoint,
    RegularityAlert,
    _add_months,
    _linear_slope,
    _detect_churn,
    _compute_nrr,
    _compute_cohort_retention,
    _compute_concentration,
    _detect_regularity_alerts,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SRC = SourceReference(file_path="test.pdf", page=0, row=0, raw_text="")
_BASE_DATE = datetime.date(2024, 1, 15)


def _date(month: int, day: int = 15) -> datetime.date:
    return datetime.date(2024, month, day)


def _txn(
    tid: str,
    *,
    credit: int | str | None = None,
    debit: int | str | None = None,
    category: TransactionCategory | None = TransactionCategory.REVENUE,
    customer_id: str | None = None,
    month: int = 1,
    day: int = 15,
) -> CanonicalTransaction:
    d = datetime.date(2024, month, day)
    if debit is not None:
        return CanonicalTransaction(
            transaction_id=tid, date=d, description=tid, debit=Decimal(str(debit)),
            balance=Decimal("0"), source_reference=_SRC, category=category,
            customer_id=customer_id,
        )
    return CanonicalTransaction(
        transaction_id=tid, date=d, description=tid, credit=Decimal(str(credit or 0)),
        balance=Decimal("0"), source_reference=_SRC, category=category,
        customer_id=customer_id,
    )


def _rev(amount: int | str, cid: str, tid: str, *, month: int = 1, day: int = 15) -> CanonicalTransaction:
    return _txn(tid, credit=amount, category=TransactionCategory.REVENUE,
                customer_id=cid, month=month, day=day)


def _doc(*txns: CanonicalTransaction) -> StatementDocument:
    return StatementDocument(
        account_id="test",
        statement_period_start=datetime.date(2024, 1, 1),
        statement_period_end=datetime.date(2024, 12, 31),
        source_format="test",
        transactions=list(txns),
    )


# ---------------------------------------------------------------------------
# TestAddMonths
# ---------------------------------------------------------------------------

class TestAddMonths:
    def test_same_month(self) -> None:
        assert _add_months("2024-01", 0) == "2024-01"

    def test_advance_one(self) -> None:
        assert _add_months("2024-01", 1) == "2024-02"

    def test_year_rollover(self) -> None:
        assert _add_months("2024-12", 1) == "2025-01"

    def test_multiple_years(self) -> None:
        assert _add_months("2024-01", 24) == "2026-01"

    def test_mid_year(self) -> None:
        assert _add_months("2024-06", 3) == "2024-09"


# ---------------------------------------------------------------------------
# TestLinearSlope
# ---------------------------------------------------------------------------

class TestLinearSlope:
    def test_positive_slope(self) -> None:
        slope = _linear_slope([0.0, 1.0, 2.0], [1.0, 2.0, 3.0])
        assert slope == pytest.approx(1.0)

    def test_negative_slope(self) -> None:
        slope = _linear_slope([0.0, 1.0, 2.0], [3.0, 2.0, 1.0])
        assert slope == pytest.approx(-1.0)

    def test_flat(self) -> None:
        slope = _linear_slope([0.0, 1.0, 2.0], [5.0, 5.0, 5.0])
        assert slope == pytest.approx(0.0)

    def test_single_point_returns_zero(self) -> None:
        assert _linear_slope([0.0], [3.0]) == 0.0

    def test_empty_returns_zero(self) -> None:
        assert _linear_slope([], []) == 0.0


# ---------------------------------------------------------------------------
# TestMonthlyActiveCustomers
# ---------------------------------------------------------------------------

class TestMonthlyActiveCustomers:
    def test_single_customer_single_month(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_customers == {"2024-01": 1}

    def test_two_customers_same_month(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1), _rev(50000, "B", "t2", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_customers["2024-01"] == 2

    def test_customer_active_in_multiple_months(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
            _rev(50000, "B", "t3", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_customers["2024-01"] == 1
        assert r.monthly_active_customers["2024-02"] == 2

    def test_non_revenue_ignored(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _txn("t2", debit=10000, category=TransactionCategory.VENDOR_PAYMENT, month=1),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_customers == {"2024-01": 1}

    def test_revenue_without_customer_id_ignored(self) -> None:
        doc = _doc(
            _txn("t1", credit=50000, category=TransactionCategory.REVENUE,
                 customer_id=None, month=1),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_customers == {}

    def test_empty_doc_returns_empty_report(self) -> None:
        r = CustomerAnalyticsAnalyst().analyse(_doc())
        assert isinstance(r, CustomerAnalyticsReport)
        assert r.monthly_active_customers == {}
        assert r.churn_events == []
        assert r.payment_regularity_alerts == []


# ---------------------------------------------------------------------------
# TestMonthlyActiveTrend
# ---------------------------------------------------------------------------

class TestMonthlyActiveTrend:
    def test_growing_customer_base_positive_slope(self) -> None:
        # Jan:1, Feb:2, Mar:3
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2), _rev(50000, "B", "t3", month=2),
            _rev(50000, "A", "t4", month=3), _rev(50000, "B", "t5", month=3),
            _rev(50000, "C", "t6", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_trend > 0

    def test_shrinking_customer_base_negative_slope(self) -> None:
        # Jan:3, Feb:2, Mar:1
        doc = _doc(
            _rev(50000, "A", "t1", month=1), _rev(50000, "B", "t2", month=1),
            _rev(50000, "C", "t3", month=1),
            _rev(50000, "A", "t4", month=2), _rev(50000, "B", "t5", month=2),
            _rev(50000, "A", "t6", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_trend < 0

    def test_single_month_trend_is_zero(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_trend == pytest.approx(0.0)

    def test_flat_trend_is_zero(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
            _rev(50000, "A", "t3", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.monthly_active_trend == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# TestNewAcquisitions
# ---------------------------------------------------------------------------

class TestNewAcquisitions:
    def test_first_payment_month_is_acquisition(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert "A" in r.new_acquisitions["2024-01"]
        assert "A" not in r.new_acquisitions.get("2024-02", [])

    def test_two_new_customers_same_month(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1), _rev(50000, "B", "t2", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert set(r.new_acquisitions["2024-01"]) == {"A", "B"}

    def test_staggered_acquisitions(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "B", "t2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert "A" in r.new_acquisitions["2024-01"]
        assert "B" in r.new_acquisitions["2024-02"]
        assert r.new_acquisitions["2024-01"] == ["A"]
        assert r.new_acquisitions["2024-02"] == ["B"]


# ---------------------------------------------------------------------------
# TestChurnDetection
# ---------------------------------------------------------------------------

class TestChurnDetection:
    def test_customer_silent_for_3_months_is_churned(self) -> None:
        # A pays only Jan; statement covers Jan-Apr via B's payments.
        # A has 3 months of silence (Feb, Mar, Apr) → churned.
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "B", "t2", month=2),
            _rev(50000, "B", "t3", month=3),
            _rev(50000, "B", "t4", month=4),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        churned = {e.customer_id for e in r.churn_events}
        assert "A" in churned
        assert "B" not in churned

    def test_customer_paying_until_end_not_churned(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
            _rev(50000, "A", "t3", month=3),
            _rev(50000, "A", "t4", month=4),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.churn_events == []

    def test_churn_requires_4_months_of_data(self) -> None:
        # Only 3 months of data — can't confirm churn
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "B", "t2", month=2),
            _rev(50000, "B", "t3", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.churn_events == []

    def test_churn_event_has_correct_avg_spend(self) -> None:
        # A pays Jan=100K, Feb=200K → avg=150K; then silent Mar, Apr, May
        doc = _doc(
            _rev(100000, "A", "t1", month=1),
            _rev(200000, "A", "t2", month=2),
            _rev(50000, "B", "t3", month=3),
            _rev(50000, "B", "t4", month=4),
            _rev(50000, "B", "t5", month=5),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        a_events = [e for e in r.churn_events if e.customer_id == "A"]
        assert len(a_events) == 1
        assert a_events[0].previous_avg_monthly_spend == Decimal("150000")

    def test_churn_event_months_active_count(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
            _rev(50000, "B", "t3", month=3),
            _rev(50000, "B", "t4", month=4),
            _rev(50000, "B", "t5", month=5),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        a_events = [e for e in r.churn_events if e.customer_id == "A"]
        assert a_events[0].months_active_before_churn == 2

    def test_churn_event_last_payment_date(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1, day=5),
            _rev(50000, "A", "t2", month=1, day=20),
            _rev(50000, "B", "t3", month=2),
            _rev(50000, "B", "t4", month=3),
            _rev(50000, "B", "t5", month=4),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        a_events = [e for e in r.churn_events if e.customer_id == "A"]
        assert a_events[0].last_payment_date == datetime.date(2024, 1, 20)

    def test_multiple_churning_customers(self) -> None:
        txns = []
        for i in range(5):
            txns.append(_rev(50000, f"churn_{i}", f"tc{i}a", month=1))
            txns.append(_rev(50000, f"churn_{i}", f"tc{i}b", month=2))
        for i in range(5):
            for m in range(1, 7):
                txns.append(_rev(50000, f"stable_{i}", f"ts{i}m{m}", month=m))
        doc = _doc(*txns)
        r = CustomerAnalyticsAnalyst().analyse(doc)
        churned = {e.customer_id for e in r.churn_events}
        assert churned == {f"churn_{i}" for i in range(5)}

    def test_boundary_customer_last_active_3_months_before_end(self) -> None:
        # Statement has months 1-6; customer last active in month 3 → exactly 3 months silence
        txns = [
            _rev(50000, "A", "tA1", month=1),
            _rev(50000, "A", "tA2", month=2),
            _rev(50000, "A", "tA3", month=3),
            _rev(50000, "B", "tB4", month=4),
            _rev(50000, "B", "tB5", month=5),
            _rev(50000, "B", "tB6", month=6),
        ]
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        churned = {e.customer_id for e in r.churn_events}
        assert "A" in churned
        assert "B" not in churned

    def test_boundary_customer_last_active_2_months_before_end_not_churned(self) -> None:
        # Statement has months 1-5; customer last active in month 3 → only 2 months silence
        txns = [
            _rev(50000, "A", "tA1", month=1),
            _rev(50000, "A", "tA2", month=2),
            _rev(50000, "A", "tA3", month=3),
            _rev(50000, "B", "tB4", month=4),
            _rev(50000, "B", "tB5", month=5),
        ]
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        churned = {e.customer_id for e in r.churn_events}
        assert "A" not in churned


# ---------------------------------------------------------------------------
# TestNRR
# ---------------------------------------------------------------------------

class TestNRR:
    def test_nrr_expansion_above_1(self) -> None:
        # Jan: A=100K, B=100K; Feb: A=120K, B=100K → monthly=1.10, annualised=1.10^12
        doc = _doc(
            _rev(100000, "A", "tA1", month=1), _rev(100000, "B", "tB1", month=1),
            _rev(120000, "A", "tA2", month=2), _rev(100000, "B", "tB2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.nrr_per_month.get("2024-02") == pytest.approx(1.10 ** 12, rel=1e-4)

    def test_nrr_contraction_below_1(self) -> None:
        # Jan: A=100K, B=100K; Feb: A=80K, B=80K → monthly=0.80, annualised=0.80^12
        doc = _doc(
            _rev(100000, "A", "tA1", month=1), _rev(100000, "B", "tB1", month=1),
            _rev(80000, "A", "tA2", month=2), _rev(80000, "B", "tB2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.nrr_per_month.get("2024-02") == pytest.approx(0.80 ** 12, rel=1e-4)

    def test_nrr_churn_reduces_below_1(self) -> None:
        # Jan: A=100K, B=100K; Feb: only A=100K (B churns) → monthly=0.50, annualised=0.50^12
        doc = _doc(
            _rev(100000, "A", "tA1", month=1), _rev(100000, "B", "tB1", month=1),
            _rev(100000, "A", "tA2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.nrr_per_month.get("2024-02") == pytest.approx(0.50 ** 12, rel=1e-4)

    def test_nrr_new_customer_not_counted(self) -> None:
        # Jan: A=100K; Feb: A=100K + new customer B=50K
        # NRR only counts cohort from Jan (only A) → monthly=1.0, annualised=1.0
        doc = _doc(
            _rev(100000, "A", "tA1", month=1),
            _rev(100000, "A", "tA2", month=2), _rev(50000, "B", "tB2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.nrr_per_month.get("2024-02") == pytest.approx(1.0)

    def test_nrr_per_month_has_one_entry_per_month_pair(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
            _rev(50000, "A", "t3", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        # NRR for Feb (Jan→Feb) and Mar (Feb→Mar)
        assert "2024-02" in r.nrr_per_month
        assert "2024-03" in r.nrr_per_month
        assert "2024-01" not in r.nrr_per_month

    def test_nrr_single_month_empty(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.nrr_per_month == {}


# ---------------------------------------------------------------------------
# TestCohortRetention
# ---------------------------------------------------------------------------

class TestCohortRetention:
    def test_cohort_retention_100_pct_at_offset_0(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1), _rev(50000, "B", "t2", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        cohort = r.cohort_retention.get("2024-01", {})
        assert cohort.get(0) == pytest.approx(1.0)

    def test_cohort_retention_partial_at_offset_1(self) -> None:
        # Cohort Jan: A, B; Feb: only A → 50% retention at offset 1
        doc = _doc(
            _rev(50000, "A", "t1", month=1), _rev(50000, "B", "t2", month=1),
            _rev(50000, "A", "t3", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.cohort_retention["2024-01"][1] == pytest.approx(0.5)

    def test_cohort_retention_full_retention(self) -> None:
        # Cohort Jan: A, B; both pay Feb, Mar → 100% at offsets 0, 1, 2
        doc = _doc(
            _rev(50000, "A", "t1", month=1), _rev(50000, "B", "t2", month=1),
            _rev(50000, "A", "t3", month=2), _rev(50000, "B", "t4", month=2),
            _rev(50000, "A", "t5", month=3), _rev(50000, "B", "t6", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        cohort = r.cohort_retention["2024-01"]
        assert cohort[0] == pytest.approx(1.0)
        assert cohort[1] == pytest.approx(1.0)
        assert cohort[2] == pytest.approx(1.0)

    def test_two_cohorts_tracked_independently(self) -> None:
        # Jan cohort: A; Feb cohort: B (new)
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2), _rev(50000, "B", "t3", month=2),
            _rev(50000, "A", "t4", month=3), _rev(50000, "B", "t5", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        # Jan cohort (A): 100% at offsets 0, 1, 2
        assert r.cohort_retention["2024-01"][2] == pytest.approx(1.0)
        # Feb cohort (B): 100% at offsets 0, 1
        assert r.cohort_retention["2024-02"][0] == pytest.approx(1.0)
        assert r.cohort_retention["2024-02"][1] == pytest.approx(1.0)

    def test_cohort_retention_stops_at_statement_end(self) -> None:
        # Only 2 months of data; Jan cohort only has offsets 0 and 1
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        cohort = r.cohort_retention["2024-01"]
        assert 0 in cohort
        assert 1 in cohort
        assert 2 not in cohort  # month 3 not in data

    def test_cohort_single_customer(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.cohort_retention["2024-01"][0] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# TestConcentrationTrajectory
# ---------------------------------------------------------------------------

class TestConcentrationTrajectory:
    def test_one_point_per_month(self) -> None:
        doc = _doc(
            _rev(80000, "A", "t1", month=1), _rev(20000, "B", "t2", month=1),
            _rev(80000, "A", "t3", month=2), _rev(20000, "B", "t4", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert len(r.concentration_trajectory) == 2

    def test_months_sorted_chronologically(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
            _rev(50000, "A", "t3", month=3),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        months = [p.month for p in r.concentration_trajectory]
        assert months == sorted(months)

    def test_single_customer_is_100_pct(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=1))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.concentration_trajectory[0].top3_share == pytest.approx(1.0)
        assert r.concentration_trajectory[0].top10_share == pytest.approx(1.0)

    def test_top3_share_correct(self) -> None:
        # 4 customers in Jan: A=40, B=30, C=20, D=10 → top3 = 90%
        doc = _doc(
            _rev(40000, "A", "t1", month=1),
            _rev(30000, "B", "t2", month=1),
            _rev(20000, "C", "t3", month=1),
            _rev(10000, "D", "t4", month=1),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.concentration_trajectory[0].top3_share == pytest.approx(0.90)

    def test_top10_share_with_fewer_than_10_customers(self) -> None:
        # Only 4 customers → top10 = 100%
        doc = _doc(
            _rev(40000, "A", "t1", month=1),
            _rev(30000, "B", "t2", month=1),
            _rev(20000, "C", "t3", month=1),
            _rev(10000, "D", "t4", month=1),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.concentration_trajectory[0].top10_share == pytest.approx(1.0)

    def test_top10_share_with_more_than_10_customers(self) -> None:
        # 11 customers each contributing equally → top10 = 10/11
        txns = [_rev(10000, f"C{i}", f"t{i}", month=1) for i in range(11)]
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        expected = 10 / 11
        assert r.concentration_trajectory[0].top10_share == pytest.approx(expected, rel=1e-6)

    def test_concentration_month_field(self) -> None:
        doc = _doc(_rev(50000, "A", "t1", month=3))
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert r.concentration_trajectory[0].month == "2024-03"

    def test_concentration_changes_across_months(self) -> None:
        # Jan: A=80%, B=10%, C=5%, D=5% → top3=95%
        # Feb: A=40%, B=30%, C=20%, D=10% → top3=90%
        doc = _doc(
            _rev(80000, "A", "t1", month=1), _rev(10000, "B", "t2", month=1),
            _rev(5000, "C", "t3", month=1), _rev(5000, "D", "t4", month=1),
            _rev(40000, "A", "t5", month=2), _rev(30000, "B", "t6", month=2),
            _rev(20000, "C", "t7", month=2), _rev(10000, "D", "t8", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        jan_top3 = next(p for p in r.concentration_trajectory if p.month == "2024-01").top3_share
        feb_top3 = next(p for p in r.concentration_trajectory if p.month == "2024-02").top3_share
        assert jan_top3 > feb_top3  # more concentrated in Jan


# ---------------------------------------------------------------------------
# TestPaymentRegularityAlerts
# ---------------------------------------------------------------------------

class TestPaymentRegularityAlerts:
    def _regular_customer(self, cid: str, months: list[int]) -> list[CanonicalTransaction]:
        return [_rev(50000, cid, f"{cid}_m{m}", month=m) for m in months]

    def test_no_alert_for_regular_monthly_customer(self) -> None:
        # Customer pays every month for 7 months, with a consistent gap
        txns = self._regular_customer("A", list(range(1, 8)))
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        alerts = [a for a in r.payment_regularity_alerts if a.customer_id == "A"]
        assert alerts == []

    def test_alert_when_recent_gap_exceeds_2x_median(self) -> None:
        # Customer pays Jan-Jul (months 1-7, monthly cadence) then skips 3 months
        # to pay in month 10 — gap of ~92 days vs median ~31 days
        base = self._regular_customer("A", [1, 2, 3, 4, 5, 6, 7])
        late = _rev(50000, "A", "A_m10", month=10)
        r = CustomerAnalyticsAnalyst().analyse(_doc(*base, late))
        alerts = [a for a in r.payment_regularity_alerts if a.customer_id == "A"]
        assert len(alerts) == 1
        assert alerts[0].actual_gap_days > alerts[0].expected_gap_days * 2

    def test_fewer_than_6_active_months_no_alert(self) -> None:
        # Only 5 distinct months — below the threshold
        txns = self._regular_customer("A", [1, 2, 3, 4, 5])
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        alerts = [a for a in r.payment_regularity_alerts if a.customer_id == "A"]
        assert alerts == []

    def test_alert_last_payment_date_is_correct(self) -> None:
        base = self._regular_customer("A", [1, 2, 3, 4, 5, 6, 7])
        late = _rev(50000, "A", "A_m10", month=10, day=5)
        r = CustomerAnalyticsAnalyst().analyse(_doc(*base, late))
        alerts = [a for a in r.payment_regularity_alerts if a.customer_id == "A"]
        assert len(alerts) == 1
        # last payment date should be in month 10
        assert alerts[0].last_payment_date.month == 10

    def test_exactly_6_active_months_eligible(self) -> None:
        # Exactly 6 months activity, then a very long gap in month 9
        base = self._regular_customer("A", [1, 2, 3, 4, 5, 6])
        late = _rev(50000, "A", "A_m9", month=9)
        r = CustomerAnalyticsAnalyst().analyse(_doc(*base, late))
        alerts = [a for a in r.payment_regularity_alerts if a.customer_id == "A"]
        assert len(alerts) == 1

    def test_multiple_customers_independent_alerts(self) -> None:
        # A: regular; B: irregular
        txns_a = self._regular_customer("A", [1, 2, 3, 4, 5, 6, 7])
        txns_b = self._regular_customer("B", [1, 2, 3, 4, 5, 6, 7])
        late_b = _rev(50000, "B", "B_m11", month=11)
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns_a, *txns_b, late_b))
        alerted = {a.customer_id for a in r.payment_regularity_alerts}
        assert "B" in alerted
        assert "A" not in alerted


# ---------------------------------------------------------------------------
# TestIntegration
# ---------------------------------------------------------------------------

class TestIntegration:
    def test_full_pipeline_returns_all_fields(self) -> None:
        txns = [
            _rev(100000, "A", "tA1", month=1), _rev(100000, "B", "tB1", month=1),
            _rev(110000, "A", "tA2", month=2), _rev(90000, "B", "tB2", month=2),
            _rev(120000, "A", "tA3", month=3), _rev(100000, "B", "tB3", month=3),
        ]
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        assert isinstance(r.monthly_active_customers, dict)
        assert isinstance(r.monthly_active_trend, float)
        assert isinstance(r.churn_events, list)
        assert isinstance(r.new_acquisitions, dict)
        assert isinstance(r.nrr_per_month, dict)
        assert isinstance(r.cohort_retention, dict)
        assert isinstance(r.concentration_trajectory, list)
        assert isinstance(r.payment_regularity_alerts, list)

    def test_mixed_categories_only_revenue_counted(self) -> None:
        txns = [
            _rev(100000, "A", "tA1", month=1),
            _txn("td1", debit=50000, category=TransactionCategory.VENDOR_PAYMENT,
                 customer_id="X", month=1),
            _txn("tl1", credit=200000, category=TransactionCategory.LOAN_IN,
                 customer_id="Y", month=1),
        ]
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        assert r.monthly_active_customers == {"2024-01": 1}

    def test_customer_with_multiple_payments_per_month(self) -> None:
        # A has 3 payments in Jan — all aggregated to one monthly bucket
        txns = [
            _rev(30000, "A", "tA1", month=1, day=5),
            _rev(30000, "A", "tA2", month=1, day=15),
            _rev(40000, "A", "tA3", month=1, day=25),
            _rev(50000, "B", "tB1", month=1),
        ]
        r = CustomerAnalyticsAnalyst().analyse(_doc(*txns))
        assert r.monthly_active_customers["2024-01"] == 2

    def test_report_types_are_correct(self) -> None:
        doc = _doc(
            _rev(50000, "A", "t1", month=1),
            _rev(50000, "A", "t2", month=2),
        )
        r = CustomerAnalyticsAnalyst().analyse(doc)
        assert isinstance(r.monthly_active_trend, float)
        for v in r.monthly_active_customers.values():
            assert isinstance(v, int)
        for pt in r.concentration_trajectory:
            assert isinstance(pt.top3_share, float)
            assert isinstance(pt.top10_share, float)
