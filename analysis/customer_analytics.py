"""Customer analytics analyst — churn, NRR, cohort retention, concentration, regularity.

Operates on REVENUE transactions that have customer_id populated (by the
CustomerIdentityResolver).  Transactions without customer_id are ignored.
All monetary arithmetic uses Decimal; ratios and shares use float.
"""
from __future__ import annotations

import datetime
import logging
import statistics
from dataclasses import dataclass, field
from decimal import Decimal

from pipeline.customer_identity import ANOMALY_FLAG_AGGREGATOR_SETTLEMENT
from schema.canonical import StatementDocument, TransactionCategory

log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_CHURN_SILENCE_MONTHS = 3
_MIN_MONTHS_FOR_REGULARITY = 6
_REGULARITY_GAP_MULTIPLE = 2.0
# Matches the "material" threshold used elsewhere in this codebase for
# round-amount clustering (analysis/risk.py) — below this, aggregator revenue
# is treated as a normal minority payment channel, not a blind spot.
_AGGREGATOR_DOMINANCE_THRESHOLD = 0.30


# ---------------------------------------------------------------------------
# Output types
# ---------------------------------------------------------------------------

@dataclass
class ChurnEvent:
    customer_id: str
    last_payment_date: datetime.date
    previous_avg_monthly_spend: Decimal
    months_active_before_churn: int


@dataclass
class ConcentrationPoint:
    month: str          # "YYYY-MM"
    top3_share: float
    top10_share: float


@dataclass
class RegularityAlert:
    customer_id: str
    expected_gap_days: int
    actual_gap_days: int
    last_payment_date: datetime.date


@dataclass
class CustomerAnalyticsReport:
    monthly_active_customers: dict[str, int]
    monthly_active_trend: float             # linear-regression slope (customers per month)
    churn_events: list[ChurnEvent]
    new_acquisitions: dict[str, list[str]]  # month -> list of customer_ids first seen
    nrr_per_month: dict[str, float]         # month M+1 -> NRR vs M cohort
    cohort_retention: dict[str, dict[int, float]]  # cohort_month -> {offset: retention}
    concentration_trajectory: list[ConcentrationPoint]
    payment_regularity_alerts: list[RegularityAlert]
    # Share (0-1) of REVENUE credit value from payment-aggregator settlement
    # lines (Razorpay, Cashfree, ...) rather than an identifiable customer —
    # these are excluded from every metric above. See is_aggregator_dominated.
    aggregator_revenue_pct: float = 0.0
    # month ("YYYY-MM") -> the set of customer_ids active that month. Lets
    # callers (e.g. reports/workbench_lens.py's Customer Master sheet) look up
    # per-customer active status without re-deriving it from raw transactions.
    active_customer_ids_by_month: dict[str, frozenset[str]] = field(default_factory=dict)

    @property
    def is_aggregator_dominated(self) -> bool:
        return self.aggregator_revenue_pct >= _AGGREGATOR_DOMINANCE_THRESHOLD


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def aggregator_caveat_text(report: CustomerAnalyticsReport) -> str | None:
    """Prominent, investor-facing caveat for any customer-analytics surface
    (alerts, lens reports) when aggregator-settled revenue dominates.
    Returns None when not applicable, so callers can just skip rendering."""
    if not report.is_aggregator_dominated:
        return None
    pct = report.aggregator_revenue_pct * 100
    return (
        f"Customer analytics unreliable — revenue is {pct:.0f}% aggregator-settled; "
        "individual customers are not visible in bank data."
    )


def _add_months(ym: str, n: int) -> str:
    """Add n months to a 'YYYY-MM' string."""
    year, month = int(ym[:4]), int(ym[5:])
    total = year * 12 + (month - 1) + n
    return f"{total // 12:04d}-{total % 12 + 1:02d}"


def _linear_slope(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    sum_x = sum(xs)
    sum_y = sum(ys)
    sum_xy = sum(x * y for x, y in zip(xs, ys))
    sum_x2 = sum(x * x for x in xs)
    denom = n * sum_x2 - sum_x * sum_x
    return (n * sum_xy - sum_x * sum_y) / denom if denom != 0 else 0.0


def _detect_churn(
    customer_active_months: dict[str, set[str]],
    customer_month_revenue: dict[tuple[str, str], Decimal],
    customer_last_date: dict[str, datetime.date],
    all_months: list[str],
) -> list[ChurnEvent]:
    """A customer is churned if their last active month is ≥ 3 months before the
    final month in the data (guaranteeing 3 full months of confirmed silence)."""
    if len(all_months) < _CHURN_SILENCE_MONTHS + 1:
        return []

    # Latest month index where churn can be confirmed (need 3 more months after it)
    max_churn_month = all_months[-(  _CHURN_SILENCE_MONTHS + 1)]

    events: list[ChurnEvent] = []
    for cid, active_months in customer_active_months.items():
        last_active = max(active_months)
        if last_active > max_churn_month:
            continue  # Not enough trailing silence to confirm churn

        total_rev = sum(
            customer_month_revenue.get((cid, m), _ZERO) for m in active_months
        )
        avg_monthly_spend = total_rev / len(active_months)

        events.append(ChurnEvent(
            customer_id=cid,
            last_payment_date=customer_last_date[cid],
            previous_avg_monthly_spend=avg_monthly_spend,
            months_active_before_churn=len(active_months),
        ))

    return events


def _compute_nrr(
    customer_month_revenue: dict[tuple[str, str], Decimal],
    customer_active_months: dict[str, set[str]],
    all_months: list[str],
) -> dict[str, float]:
    """For each consecutive month pair (M, M+1): period-over-period NRR = revenue
    from M's cohort in M+1 divided by their revenue in M (>100% = net expansion,
    <100% = net contraction). Reported as-is, per period — NOT annualised. A
    single noisy month should not be raised to the 12th power and presented as
    a full-year figure; the statement windows this pipeline analyses are
    typically 3-24 months, so a per-period ratio is what's actually meaningful.

    Only existing customers from M are counted; new customers first appearing in M+1
    are excluded from the numerator and denominator.
    """
    nrr: dict[str, float] = {}
    for i in range(len(all_months) - 1):
        m0, m1 = all_months[i], all_months[i + 1]
        cohort = {cid for cid, months in customer_active_months.items() if m0 in months}
        if not cohort:
            continue
        rev_m0 = sum(customer_month_revenue.get((cid, m0), _ZERO) for cid in cohort)
        if rev_m0 == _ZERO:
            continue
        rev_m1 = sum(customer_month_revenue.get((cid, m1), _ZERO) for cid in cohort)
        nrr[m1] = float(rev_m1 / rev_m0)
    return nrr


def _compute_cohort_retention(
    customer_first_month: dict[str, str],
    customer_active_months: dict[str, set[str]],
    all_months: list[str],
) -> dict[str, dict[int, float]]:
    """For each cohort (month of first payment), compute the fraction of cohort
    members who paid at each offset n = 0, 1, 2, … from their cohort month."""
    cohorts: dict[str, list[str]] = {}
    for cid, first_m in customer_first_month.items():
        cohorts.setdefault(first_m, []).append(cid)

    all_months_set = set(all_months)
    retention: dict[str, dict[int, float]] = {}

    for cohort_month, members in cohorts.items():
        size = len(members)
        month_ret: dict[int, float] = {}
        n = 0
        while True:
            target = _add_months(cohort_month, n)
            if target not in all_months_set:
                break
            active = sum(
                1 for cid in members if target in customer_active_months.get(cid, set())
            )
            month_ret[n] = active / size
            n += 1
        retention[cohort_month] = month_ret

    return retention


def _compute_concentration(
    customer_month_revenue: dict[tuple[str, str], Decimal],
    all_months: list[str],
) -> list[ConcentrationPoint]:
    """Top-3 and top-10 revenue share per month."""
    # Pre-group: month -> {cid: revenue}
    monthly: dict[str, dict[str, Decimal]] = {}
    for (cid, ym), rev in customer_month_revenue.items():
        monthly.setdefault(ym, {})[cid] = rev

    points: list[ConcentrationPoint] = []
    for m in all_months:
        rev_by_cust = monthly.get(m, {})
        if not rev_by_cust:
            continue
        total = sum(rev_by_cust.values(), _ZERO)
        if total == _ZERO:
            continue
        sorted_rev = sorted(rev_by_cust.values(), reverse=True)
        top3  = float(sum(sorted_rev[:3])  / total)
        top10 = float(sum(sorted_rev[:10]) / total)
        points.append(ConcentrationPoint(month=m, top3_share=top3, top10_share=top10))

    return points


def _detect_regularity_alerts(
    customer_active_months: dict[str, set[str]],
    customer_last_date: dict[str, datetime.date],
) -> list[RegularityAlert]:
    """Flag customers with ≥ 6 active months whose most-recent payment gap is
    > 2× their historical median gap.

    We use one representative date per active month (1st of the month) to
    avoid within-month invoice noise polluting the cadence signal.
    """
    alerts: list[RegularityAlert] = []
    for cid, active_months in customer_active_months.items():
        if len(active_months) < _MIN_MONTHS_FOR_REGULARITY:
            continue
        sorted_months = sorted(active_months)
        # One representative date per month: the 1st
        dates = [
            datetime.date(int(ym[:4]), int(ym[5:]), 1) for ym in sorted_months
        ]
        if len(dates) < 3:
            continue  # Need at least 3 payment events for historical + recent gap

        all_gaps = [(dates[i + 1] - dates[i]).days for i in range(len(dates) - 1)]
        historical_gaps = all_gaps[:-1]
        recent_gap = all_gaps[-1]

        if len(historical_gaps) < 2:
            continue  # Can't establish a reliable baseline

        modal_gap = statistics.median(historical_gaps)
        if modal_gap > 0 and recent_gap > _REGULARITY_GAP_MULTIPLE * modal_gap:
            alerts.append(RegularityAlert(
                customer_id=cid,
                expected_gap_days=int(modal_gap),
                actual_gap_days=recent_gap,
                last_payment_date=customer_last_date[cid],
            ))

    return alerts


def _empty_report(aggregator_revenue_pct: float = 0.0) -> CustomerAnalyticsReport:
    return CustomerAnalyticsReport(
        monthly_active_customers={},
        monthly_active_trend=0.0,
        churn_events=[],
        new_acquisitions={},
        nrr_per_month={},
        cohort_retention={},
        concentration_trajectory=[],
        payment_regularity_alerts=[],
        aggregator_revenue_pct=aggregator_revenue_pct,
        active_customer_ids_by_month={},
    )


def _aggregator_revenue_pct(doc: StatementDocument) -> float:
    """Share of REVENUE credit value from aggregator-settlement transactions.

    Computed over ALL revenue transactions, independent of customer_id —
    the resolver never assigns one to aggregator transactions, so a
    100%-aggregator-settled statement must still report this correctly
    rather than looking like "no revenue data at all".
    """
    all_rev = [
        t for t in doc.transactions
        if t.category == TransactionCategory.REVENUE and t.credit is not None
    ]
    total = sum(t.credit for t in all_rev) or _ZERO
    if total == _ZERO:
        return 0.0
    aggregator_total = sum(
        t.credit for t in all_rev
        if ANOMALY_FLAG_AGGREGATOR_SETTLEMENT in t.anomaly_flags
    )
    return float(aggregator_total / total)


# ---------------------------------------------------------------------------
# Analyst
# ---------------------------------------------------------------------------

class CustomerAnalyticsAnalyst:
    """Derives customer-level analytics from a fully categorised StatementDocument."""

    def analyse(self, doc: StatementDocument) -> CustomerAnalyticsReport:
        aggregator_pct = _aggregator_revenue_pct(doc)

        rev_txns = [
            t for t in doc.transactions
            if t.category == TransactionCategory.REVENUE
            and t.credit is not None
            and t.customer_id
        ]
        if not rev_txns:
            return _empty_report(aggregator_pct)

        # ---- Build core data structures ----
        customer_month_revenue: dict[tuple[str, str], Decimal] = {}
        customer_last_date: dict[str, datetime.date] = {}

        for t in rev_txns:
            cid = t.customer_id
            assert cid is not None and t.credit is not None
            ym = t.date.strftime("%Y-%m")
            key = (cid, ym)
            customer_month_revenue[key] = customer_month_revenue.get(key, _ZERO) + t.credit
            if cid not in customer_last_date or t.date > customer_last_date[cid]:
                customer_last_date[cid] = t.date

        customer_active_months: dict[str, set[str]] = {}
        for (cid, ym) in customer_month_revenue:
            customer_active_months.setdefault(cid, set()).add(ym)

        customer_first_month: dict[str, str] = {
            cid: min(months) for cid, months in customer_active_months.items()
        }

        all_months = sorted({ym for _, ym in customer_month_revenue})

        # ---- Monthly active counts ----
        monthly_active = {
            m: sum(1 for months in customer_active_months.values() if m in months)
            for m in all_months
        }

        # ---- Trend (linear regression slope) ----
        if len(all_months) >= 2:
            xs = [float(i) for i in range(len(all_months))]
            ys = [float(monthly_active[m]) for m in all_months]
            trend = _linear_slope(xs, ys)
        else:
            trend = 0.0

        # ---- New acquisitions ----
        new_acquisitions: dict[str, list[str]] = {m: [] for m in all_months}
        for cid, first_m in customer_first_month.items():
            new_acquisitions[first_m].append(cid)

        # ---- Churn ----
        churn_events = _detect_churn(
            customer_active_months, customer_month_revenue, customer_last_date, all_months
        )

        # ---- NRR ----
        nrr = _compute_nrr(customer_month_revenue, customer_active_months, all_months)

        # ---- Cohort retention ----
        cohort_ret = _compute_cohort_retention(
            customer_first_month, customer_active_months, all_months
        )

        # ---- Concentration trajectory ----
        conc = _compute_concentration(customer_month_revenue, all_months)

        # ---- Payment regularity ----
        alerts = _detect_regularity_alerts(customer_active_months, customer_last_date)

        # ---- Active customer IDs per month (inverted from customer_active_months) ----
        active_by_month: dict[str, frozenset[str]] = {
            m: frozenset(cid for cid, months in customer_active_months.items() if m in months)
            for m in all_months
        }

        return CustomerAnalyticsReport(
            monthly_active_customers=monthly_active,
            monthly_active_trend=trend,
            churn_events=churn_events,
            new_acquisitions=new_acquisitions,
            nrr_per_month=nrr,
            cohort_retention=cohort_ret,
            concentration_trajectory=conc,
            payment_regularity_alerts=alerts,
            aggregator_revenue_pct=aggregator_pct,
            active_customer_ids_by_month=active_by_month,
        )
