"""Tests for the FinancialAnalyst (Step 1.9).

Strategy:
  - Unit tests use hand-crafted StatementDocuments with known category/amount data
    so expected values can be computed by hand.
  - Integration tests run the full pipeline (generate → adapter → normaliser →
    categoriser → analyst) and assert structural properties (ranges, signs).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from analysis.categoriser import Categoriser
from analysis.financial_analyst import FinancialAnalyst, FinancialMetrics, MonthlyStats
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
    ValidationStatus,
)

# ─── helpers ─────────────────────────────────────────────────────────────────

def _ref(page: int = 0, row: int = 1) -> SourceReference:
    return SourceReference(file_path="test.pdf", page=page, row=row, raw_text="")


def _credit_txn(
    txn_id: str,
    d: date,
    amount: str,
    category: TransactionCategory,
    balance: str = "100000",
    customer_id: str | None = None,
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=txn_id,
        date=d,
        description="TEST",
        credit=Decimal(amount),
        balance=Decimal(balance),
        source_reference=_ref(),
        category=category,
        customer_id=customer_id,
    )


def _debit_txn(
    txn_id: str,
    d: date,
    amount: str,
    category: TransactionCategory,
    balance: str = "90000",
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=txn_id,
        date=d,
        description="TEST",
        debit=Decimal(amount),
        balance=Decimal(balance),
        source_reference=_ref(),
        category=category,
    )


def _doc(txns: list[CanonicalTransaction]) -> StatementDocument:
    if txns:
        start = min(t.date for t in txns)
        end = max(t.date for t in txns)
    else:
        start = end = date(2024, 4, 1)
    return StatementDocument(
        account_id="test_acct",
        statement_period_start=start,
        statement_period_end=end,
        source_format="digital_pdf_hdfc",
        validation_status=ValidationStatus.PASSED,
        transactions=txns,
    )


# ─── 3-month document used in many tests ─────────────────────────────────────
#
# April: revenue=200000, burn=80000  → net=120000
# May:   revenue=240000, burn=100000 → net=140000
# June:  revenue=180000, burn=90000  → net=90000
#
# total_revenue=620000, total_burn=270000
# avg_monthly_revenue=206666.67, avg_monthly_burn=90000
# closing_balance=50000 (last txn)
# MoM growth April→May: (240000-200000)/200000 = 0.2000
# MoM growth May→June:  (180000-240000)/240000 = -0.2500
# avg_mom_growth = (-0.0500) / 2 = -0.0250

@pytest.fixture(scope="module")
def three_month_doc() -> StatementDocument:
    txns = [
        # April revenue
        _credit_txn("r1", date(2024, 4, 5),  "120000", TransactionCategory.REVENUE, "1120000"),
        _credit_txn("r2", date(2024, 4, 20), "80000",  TransactionCategory.REVENUE, "1200000"),
        # April burn
        _debit_txn("b1", date(2024, 4, 10), "50000",  TransactionCategory.VENDOR_PAYMENT, "1070000"),
        _debit_txn("b2", date(2024, 4, 25), "30000",  TransactionCategory.SALARY, "1040000"),
        # May revenue
        _credit_txn("r3", date(2024, 5, 8),  "240000", TransactionCategory.REVENUE, "1280000"),
        # May burn
        _debit_txn("b3", date(2024, 5, 15), "60000",  TransactionCategory.VENDOR_PAYMENT, "1220000"),
        _debit_txn("b4", date(2024, 5, 28), "40000",  TransactionCategory.SALARY, "1180000"),
        # June revenue
        _credit_txn("r4", date(2024, 6, 10), "180000", TransactionCategory.REVENUE, "1360000"),
        # June burn
        _debit_txn("b5", date(2024, 6, 20), "90000",  TransactionCategory.VENDOR_PAYMENT, "1270000"),
        # Last transaction sets closing balance
        _credit_txn("r5", date(2024, 6, 30), "1",    TransactionCategory.REVENUE, "50000"),
    ]
    return _doc(txns)


@pytest.fixture(scope="module")
def three_month_metrics(three_month_doc: StatementDocument) -> FinancialMetrics:
    return FinancialAnalyst().analyse(three_month_doc)


# ─── Basic structure ──────────────────────────────────────────────────────────

class TestBasicStructure:
    def test_returns_financial_metrics(self, three_month_metrics: FinancialMetrics) -> None:
        assert isinstance(three_month_metrics, FinancialMetrics)

    def test_period_months(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.period_months == 3

    def test_monthly_stats_sorted(self, three_month_metrics: FinancialMetrics) -> None:
        yms = [m.year_month for m in three_month_metrics.monthly_stats]
        assert yms == sorted(yms)

    def test_monthly_stats_count(self, three_month_metrics: FinancialMetrics) -> None:
        assert len(three_month_metrics.monthly_stats) == 3

    def test_monthly_stats_type(self, three_month_metrics: FinancialMetrics) -> None:
        assert all(isinstance(m, MonthlyStats) for m in three_month_metrics.monthly_stats)


# ─── Revenue metrics ──────────────────────────────────────────────────────────

class TestRevenue:
    def test_total_revenue(self, three_month_metrics: FinancialMetrics) -> None:
        # 120000+80000+240000+180000+1 = 620001
        assert three_month_metrics.total_revenue == Decimal("620001")

    def test_monthly_revenue_april(self, three_month_metrics: FinancialMetrics) -> None:
        april = next(m for m in three_month_metrics.monthly_stats if m.year_month == "2024-04")
        assert april.revenue == Decimal("200000")

    def test_monthly_revenue_may(self, three_month_metrics: FinancialMetrics) -> None:
        may = next(m for m in three_month_metrics.monthly_stats if m.year_month == "2024-05")
        assert may.revenue == Decimal("240000")

    def test_monthly_revenue_june(self, three_month_metrics: FinancialMetrics) -> None:
        june = next(m for m in three_month_metrics.monthly_stats if m.year_month == "2024-06")
        assert june.revenue == Decimal("180001")

    def test_avg_monthly_revenue_positive(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.avg_monthly_revenue > Decimal("0")

    def test_avg_monthly_revenue_approx(self, three_month_metrics: FinancialMetrics) -> None:
        # 620001 / 3 ≈ 206667
        avg = three_month_metrics.avg_monthly_revenue
        assert Decimal("200000") < avg < Decimal("220000")


# ─── Burn metrics ─────────────────────────────────────────────────────────────

class TestBurn:
    def test_total_burn(self, three_month_metrics: FinancialMetrics) -> None:
        # 50000+30000+60000+40000+90000 = 270000
        assert three_month_metrics.total_burn == Decimal("270000")

    def test_monthly_burn_april(self, three_month_metrics: FinancialMetrics) -> None:
        april = next(m for m in three_month_metrics.monthly_stats if m.year_month == "2024-04")
        assert april.burn == Decimal("80000")

    def test_avg_monthly_burn(self, three_month_metrics: FinancialMetrics) -> None:
        # 270000 / 3 = 90000
        assert three_month_metrics.avg_monthly_burn == Decimal("90000")

    def test_burn_categories_counted(self) -> None:
        """VENDOR_PAYMENT, SALARY, TAX, FEES, FOUNDER_WITHDRAWAL, LOAN_OUT all count as burn."""
        txns = [
            _debit_txn("d1", date(2024, 4, 1), "10000", TransactionCategory.VENDOR_PAYMENT),
            _debit_txn("d2", date(2024, 4, 2), "20000", TransactionCategory.SALARY),
            _debit_txn("d3", date(2024, 4, 3), "5000",  TransactionCategory.TAX),
            _debit_txn("d4", date(2024, 4, 4), "1000",  TransactionCategory.FEES),
            _debit_txn("d5", date(2024, 4, 5), "15000", TransactionCategory.FOUNDER_WITHDRAWAL),
            _debit_txn("d6", date(2024, 4, 6), "25000", TransactionCategory.LOAN_OUT),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.total_burn == Decimal("76000")

    def test_transfer_not_counted_as_burn(self) -> None:
        txns = [
            _debit_txn("d1", date(2024, 4, 1), "50000", TransactionCategory.TRANSFER),
            _credit_txn("c1", date(2024, 4, 2), "100",  TransactionCategory.REVENUE),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.total_burn == Decimal("0")

    def test_loan_in_not_counted_as_burn(self) -> None:
        txns = [_credit_txn("c1", date(2024, 4, 1), "500000", TransactionCategory.LOAN_IN)]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.total_burn == Decimal("0")


# ─── Net cash flow and profitability ──────────────────────────────────────────

class TestNetCashFlow:
    def test_net_cash_flow_property(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.net_cash_flow == (
            three_month_metrics.total_revenue - three_month_metrics.total_burn
        )

    def test_is_profitable_when_revenue_exceeds_burn(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.is_profitable

    def test_is_not_profitable_when_burn_exceeds_revenue(self) -> None:
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "10000", TransactionCategory.REVENUE),
            _debit_txn("d1", date(2024, 4, 2), "50000", TransactionCategory.VENDOR_PAYMENT),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert not m.is_profitable

    def test_monthly_net_property(self, three_month_metrics: FinancialMetrics) -> None:
        for ms in three_month_metrics.monthly_stats:
            assert ms.net == ms.revenue - ms.burn


# ─── Closing balance and runway ───────────────────────────────────────────────

class TestRunway:
    def test_closing_balance_is_last_txn_balance(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.closing_balance == Decimal("50000")

    def test_runway_positive_when_burn_positive(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.runway_months is not None
        assert three_month_metrics.runway_months > Decimal("0")

    def test_runway_calculation(self, three_month_metrics: FinancialMetrics) -> None:
        # closing_balance=50000, avg_monthly_burn=90000 → 50000/90000 ≈ 0.556
        expected = Decimal("50000") / Decimal("90000")
        assert abs(three_month_metrics.runway_months - expected) < Decimal("0.01")

    def test_runway_none_when_no_burn(self) -> None:
        txns = [_credit_txn("c1", date(2024, 4, 1), "100000", TransactionCategory.REVENUE, "100000")]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.runway_months is None

    def test_runway_none_on_empty_doc(self) -> None:
        m = FinancialAnalyst().analyse(_doc([]))
        assert m.runway_months is None


# ─── MoM growth ───────────────────────────────────────────────────────────────

class TestMomGrowth:
    def test_growth_rates_count(self, three_month_metrics: FinancialMetrics) -> None:
        # 3 months → 2 consecutive pairs → 2 growth rates
        assert len(three_month_metrics.mom_revenue_growth_rates) == 2

    def test_april_to_may_growth(self, three_month_metrics: FinancialMetrics) -> None:
        # (240000 - 200000) / 200000 = 0.2000
        rate = three_month_metrics.mom_revenue_growth_rates[0]
        assert rate == pytest.approx(Decimal("0.2000"), abs=Decimal("0.001"))

    def test_may_to_june_growth(self, three_month_metrics: FinancialMetrics) -> None:
        # (180001 - 240000) / 240000 ≈ -0.2500
        rate = three_month_metrics.mom_revenue_growth_rates[1]
        assert rate == pytest.approx(Decimal("-0.25"), abs=Decimal("0.001"))

    def test_avg_mom_growth_negative(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.avg_mom_growth is not None
        assert three_month_metrics.avg_mom_growth < Decimal("0")

    def test_has_declining_revenue_flag(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.has_declining_revenue

    def test_growth_rates_empty_for_single_month(self) -> None:
        txns = [_credit_txn("c1", date(2024, 4, 1), "50000", TransactionCategory.REVENUE)]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.mom_revenue_growth_rates == []
        assert m.avg_mom_growth is None

    def test_positive_growth_flag(self) -> None:
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "100000", TransactionCategory.REVENUE),
            _credit_txn("c2", date(2024, 5, 1), "150000", TransactionCategory.REVENUE),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert not m.has_declining_revenue
        assert m.avg_mom_growth == pytest.approx(Decimal("0.5"), abs=Decimal("0.001"))

    def test_growth_skips_zero_revenue_month(self) -> None:
        """If a month has zero revenue, the growth rate for that pair is skipped."""
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "100000", TransactionCategory.REVENUE),
            # May: no revenue (burn only)
            _debit_txn("d1", date(2024, 5, 1), "10000", TransactionCategory.VENDOR_PAYMENT),
            _credit_txn("c2", date(2024, 6, 1), "120000", TransactionCategory.REVENUE),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        # April→May growth skipped (prev.revenue=100000 > 0, curr.revenue=0 → -1.0)
        # Actually April→May: prev=100000>0, rate = (0-100000)/100000 = -1.0 — included
        # May→June: prev=0, skipped
        assert len(m.mom_revenue_growth_rates) == 1


# ─── Customer concentration ───────────────────────────────────────────────────

class TestCustomerConcentration:
    def test_none_when_no_customer_id(self, three_month_metrics: FinancialMetrics) -> None:
        assert three_month_metrics.revenue_hhi is None
        assert three_month_metrics.top_customer_revenue_pct is None

    def test_hhi_one_for_single_customer(self) -> None:
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "100000", TransactionCategory.REVENUE,
                        customer_id="cust_A"),
            _credit_txn("c2", date(2024, 4, 15), "50000", TransactionCategory.REVENUE,
                        customer_id="cust_A"),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.revenue_hhi == Decimal("1.0000")
        assert m.top_customer_revenue_pct == Decimal("1.0000")

    def test_hhi_low_for_many_equal_customers(self) -> None:
        txns = [
            _credit_txn(f"c{i}", date(2024, 4, i + 1), "10000", TransactionCategory.REVENUE,
                        customer_id=f"cust_{i}")
            for i in range(10)
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        # 10 equal customers → HHI = 10 * (0.1)^2 = 0.1
        assert m.revenue_hhi == pytest.approx(Decimal("0.1"), abs=Decimal("0.001"))
        assert m.top_customer_revenue_pct == pytest.approx(Decimal("0.1"), abs=Decimal("0.001"))

    def test_hhi_two_customers_80_20(self) -> None:
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "80000", TransactionCategory.REVENUE,
                        customer_id="big"),
            _credit_txn("c2", date(2024, 4, 2), "20000", TransactionCategory.REVENUE,
                        customer_id="small"),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        # HHI = 0.8^2 + 0.2^2 = 0.64 + 0.04 = 0.68
        assert m.revenue_hhi == pytest.approx(Decimal("0.68"), abs=Decimal("0.001"))
        assert m.top_customer_revenue_pct == pytest.approx(Decimal("0.8"), abs=Decimal("0.001"))

    def test_non_revenue_transactions_ignored_for_hhi(self) -> None:
        """Only REVENUE credits count for customer concentration."""
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "100000", TransactionCategory.REVENUE,
                        customer_id="cust_A"),
            # LOAN_IN with customer_id — should not count
            _credit_txn("c2", date(2024, 4, 2), "500000", TransactionCategory.LOAN_IN,
                        customer_id="bank_X"),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert m.revenue_hhi == Decimal("1.0000")


# ─── Edge cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_document(self) -> None:
        m = FinancialAnalyst().analyse(_doc([]))
        assert m.period_months == 0
        assert m.total_revenue == Decimal("0")
        assert m.total_burn == Decimal("0")
        assert m.closing_balance == Decimal("0")
        assert m.monthly_stats == []
        assert m.runway_months is None
        assert m.mom_revenue_growth_rates == []
        assert m.avg_mom_growth is None
        assert m.revenue_hhi is None

    def test_uncategorised_transactions_excluded(self) -> None:
        """Transactions with category=None are ignored."""
        txn = CanonicalTransaction(
            transaction_id="u1", date=date(2024, 4, 1), description="?",
            debit=Decimal("99999"), balance=Decimal("1"),
            source_reference=_ref(), category=None,
        )
        doc = _doc([txn])
        m = FinancialAnalyst().analyse(doc)
        assert m.total_burn == Decimal("0")

    def test_two_month_growth(self) -> None:
        txns = [
            _credit_txn("c1", date(2024, 4, 1), "100000", TransactionCategory.REVENUE),
            _credit_txn("c2", date(2024, 5, 1), "130000", TransactionCategory.REVENUE),
        ]
        m = FinancialAnalyst().analyse(_doc(txns))
        assert len(m.mom_revenue_growth_rates) == 1
        assert m.avg_mom_growth == pytest.approx(Decimal("0.3"), abs=Decimal("0.001"))


# ─── Integration — full pipeline ─────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("analyst_test")


class TestIntegration:
    def _run_full_pipeline(self, bank: str, profile: str, tmp_root: Path) -> FinancialMetrics:
        from adapters import DigitalPDFAdapter
        from pipeline.normaliser import Normaliser
        from tools.synthetic_gen import generate_statement

        pdf, _ = generate_statement(
            bank=bank, profile=profile, output_dir=tmp_root,
            statement_id=f"analyst_{bank}_{profile}", flags=[], seed=42,
        )
        raw = DigitalPDFAdapter().extract(pdf)
        doc = Normaliser().normalise(raw)
        doc, _ = Categoriser(llm_enabled=False).categorise(doc)
        return FinancialAnalyst().analyse(doc)

    def test_hdfc_healthy_saas_has_positive_revenue(self, tmp_root: Path) -> None:
        m = self._run_full_pipeline("hdfc", "healthy_saas", tmp_root)
        assert m.total_revenue > Decimal("0")

    def test_hdfc_healthy_saas_has_positive_burn(self, tmp_root: Path) -> None:
        m = self._run_full_pipeline("hdfc", "healthy_saas", tmp_root)
        assert m.total_burn > Decimal("0")

    def test_hdfc_healthy_saas_period_months(self, tmp_root: Path) -> None:
        m = self._run_full_pipeline("hdfc", "healthy_saas", tmp_root)
        assert m.period_months >= 2

    def test_hdfc_healthy_saas_has_growth_rates(self, tmp_root: Path) -> None:
        m = self._run_full_pipeline("hdfc", "healthy_saas", tmp_root)
        assert len(m.mom_revenue_growth_rates) >= 1

    def test_icici_services_firm_is_profitable(self, tmp_root: Path) -> None:
        m = self._run_full_pipeline("icici", "services_firm", tmp_root)
        assert m.is_profitable

    def test_closing_balance_positive_for_safe_profiles(self, tmp_root: Path) -> None:
        m = self._run_full_pipeline("hdfc", "services_firm", tmp_root)
        assert m.closing_balance > Decimal("0")

    def test_avg_monthly_revenue_approx_scale(self, tmp_root: Path) -> None:
        """healthy_saas monthly revenue should be in the lakhs range (₹1L–₹50L)."""
        m = self._run_full_pipeline("hdfc", "healthy_saas", tmp_root)
        assert Decimal("100000") <= m.avg_monthly_revenue <= Decimal("5000000")
