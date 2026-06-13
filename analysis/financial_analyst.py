"""Financial-health analyst — burn rate, runway, MoM revenue growth, customer concentration.

Takes a fully categorised StatementDocument and returns FinancialMetrics.
All monetary values are in the document's currency (INR by default).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from itertools import pairwise

from schema.canonical import CanonicalTransaction, StatementDocument, TransactionCategory

log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_FOUR = Decimal("0.0001")

# Categories counted as cash burn (operating + debt service)
_BURN_CATEGORIES = frozenset({
    TransactionCategory.VENDOR_PAYMENT,
    TransactionCategory.SALARY,
    TransactionCategory.TAX,
    TransactionCategory.FEES,
    TransactionCategory.FOUNDER_WITHDRAWAL,
    TransactionCategory.LOAN_OUT,
})


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class MonthlyStats:
    """Revenue and burn for one calendar month."""
    year_month: str  # "YYYY-MM"
    revenue: Decimal = field(default_factory=lambda: Decimal("0"))
    burn: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def net(self) -> Decimal:
        return self.revenue - self.burn


@dataclass
class FinancialMetrics:
    """All financial health metrics for an investor lens report."""

    # ── Period ────────────────────────────────────────────────────────────────
    period_months: int  # distinct calendar months with at least one transaction

    # ── Aggregate totals ──────────────────────────────────────────────────────
    total_revenue: Decimal
    total_burn: Decimal
    closing_balance: Decimal  # balance of the last transaction in PDF order

    # ── Monthly detail ────────────────────────────────────────────────────────
    monthly_stats: list[MonthlyStats]  # sorted chronologically

    # ── Per-month averages ────────────────────────────────────────────────────
    avg_monthly_revenue: Decimal
    avg_monthly_burn: Decimal

    # ── Runway ────────────────────────────────────────────────────────────────
    runway_months: Decimal | None  # None when avg_monthly_burn == 0 (profitable)

    # ── MoM revenue growth ────────────────────────────────────────────────────
    mom_revenue_growth_rates: list[Decimal]  # one entry per consecutive month pair
    avg_mom_growth: Decimal | None  # None when < 2 months of revenue data

    # ── Customer concentration (None when customer_id not populated) ───────────
    revenue_hhi: Decimal | None          # Herfindahl-Hirschman Index [0, 1]
    top_customer_revenue_pct: Decimal | None  # share of the single largest customer

    # ── Derived flags ─────────────────────────────────────────────────────────
    @property
    def net_cash_flow(self) -> Decimal:
        return self.total_revenue - self.total_burn

    @property
    def is_profitable(self) -> bool:
        return self.avg_monthly_revenue > self.avg_monthly_burn

    @property
    def has_declining_revenue(self) -> bool:
        return self.avg_mom_growth is not None and self.avg_mom_growth < _ZERO


# ─────────────────────────────────────────────────────────────────────────────
# Analyst
# ─────────────────────────────────────────────────────────────────────────────

class FinancialAnalyst:
    """Computes FinancialMetrics from a categorised StatementDocument."""

    def analyse(self, doc: StatementDocument) -> FinancialMetrics:
        txns = [t for t in doc.transactions if t.category is not None]

        closing_balance = doc.transactions[-1].balance if doc.transactions else _ZERO

        monthly = _build_monthly_stats(txns)
        sorted_months = sorted(monthly.values(), key=lambda m: m.year_month)
        period_months = len(sorted_months)

        total_revenue = sum((m.revenue for m in sorted_months), _ZERO)
        total_burn = sum((m.burn for m in sorted_months), _ZERO)

        avg_monthly_revenue = _safe_divide(total_revenue, period_months)
        avg_monthly_burn = _safe_divide(total_burn, period_months)

        runway_months = (
            _safe_divide(closing_balance, avg_monthly_burn)
            if avg_monthly_burn > _ZERO
            else None
        )

        growth_rates = _mom_growth_rates(sorted_months)
        avg_mom_growth = (
            _safe_divide(sum(growth_rates, _ZERO), len(growth_rates))
            if growth_rates
            else None
        )

        hhi, top_pct = _customer_concentration(txns)

        return FinancialMetrics(
            period_months=period_months,
            total_revenue=total_revenue,
            total_burn=total_burn,
            closing_balance=closing_balance,
            monthly_stats=sorted_months,
            avg_monthly_revenue=avg_monthly_revenue,
            avg_monthly_burn=avg_monthly_burn,
            runway_months=runway_months,
            mom_revenue_growth_rates=growth_rates,
            avg_mom_growth=avg_mom_growth,
            revenue_hhi=hhi,
            top_customer_revenue_pct=top_pct,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _build_monthly_stats(txns: list[CanonicalTransaction]) -> dict[str, MonthlyStats]:
    monthly: dict[str, MonthlyStats] = {}
    for txn in txns:
        ym = txn.date.strftime("%Y-%m")
        if ym not in monthly:
            monthly[ym] = MonthlyStats(year_month=ym)
        ms = monthly[ym]
        if txn.category == TransactionCategory.REVENUE and txn.credit is not None:
            ms.revenue += txn.credit
        elif txn.category in _BURN_CATEGORIES and txn.debit is not None:
            ms.burn += txn.debit
    return monthly


def _mom_growth_rates(months: list[MonthlyStats]) -> list[Decimal]:
    rates = []
    for prev, curr in pairwise(months):
        if prev.revenue > _ZERO:
            rate = ((curr.revenue - prev.revenue) / prev.revenue).quantize(
                _FOUR, ROUND_HALF_UP
            )
            rates.append(rate)
    return rates


def _customer_concentration(
    txns: list[CanonicalTransaction],
) -> tuple[Decimal | None, Decimal | None]:
    """HHI and top-customer share, or (None, None) if customer_id not populated."""
    rev_by_customer: dict[str, Decimal] = {}
    for txn in txns:
        if (
            txn.category == TransactionCategory.REVENUE
            and txn.credit is not None
            and txn.customer_id
        ):
            rev_by_customer[txn.customer_id] = (
                rev_by_customer.get(txn.customer_id, _ZERO) + txn.credit
            )

    if not rev_by_customer:
        return None, None

    total = sum(rev_by_customer.values(), _ZERO)
    if total == _ZERO:
        return None, None

    shares = [v / total for v in rev_by_customer.values()]
    hhi = sum(s * s for s in shares).quantize(_FOUR, ROUND_HALF_UP)
    top_pct = max(shares).quantize(_FOUR, ROUND_HALF_UP)
    return hhi, top_pct


def _safe_divide(numerator: Decimal, denominator: int | Decimal) -> Decimal:
    d = Decimal(str(denominator))
    if d == _ZERO:
        return _ZERO
    return numerator / d
