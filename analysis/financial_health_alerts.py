"""Financial health alert analyst.

Detects metric-based warning signals from FinancialMetrics and
CustomerAnalyticsReport.  Unlike the transaction-pattern risk flags, these
alerts are derived purely from financial ratios and trends.

Each alert has a rule-based description which is then polished by an LLM
(Claude Haiku) into an investor-facing 2-sentence explanation if an API key
is available.  All alerts are batched into a single LLM call.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from analysis.customer_analytics import CustomerAnalyticsReport
from analysis.financial_analyst import FinancialMetrics
from analysis.risk import _CONC_HIGH, _CONC_MEDIUM, Severity

log = logging.getLogger(__name__)

_ZERO = Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# Shared threshold/band classification (Wave 3.2 consolidation)
#
# This is the single source of truth for runway/NRR/churn/burn/concentration
# bands — every report lens (angel, VC, workbench, network) imports these
# classify_* functions rather than re-implementing its own cut points. Each
# function returns a short string tag from a fixed vocabulary; callers map
# the tag to their own report-specific colour and phrasing, but the numeric
# BOUNDARY always comes from here, so the same statement can no longer get a
# different band from different lenses.
#
# Concentration threshold constants are owned by analysis/risk.py (the actual
# flag-generating detector) and re-exported here rather than duplicated a
# third time — analysis/risk.py has no dependency on this module, so this
# import direction doesn't create a cycle.
# ─────────────────────────────────────────────────────────────────────────────

RUNWAY_CRITICAL_MONTHS = Decimal("2")
RUNWAY_LOW_MONTHS = Decimal("4")
RUNWAY_HEALTHY_MONTHS = Decimal("6")

BURN_EXTREME_RATIO = 3.0
BURN_HIGH_RATIO = 1.5

NRR_SEVERE_PCT = 0.70
NRR_CONTRACTION_PCT = 0.85
NRR_PAR_PCT = 1.00

CHURN_HIGH_PCT = 0.30
CHURN_ELEVATED_PCT = 0.15

# Concentration is measured on TOP-3 customer revenue share (not top-1) —
# matches analysis/risk.py's customer_concentration detector, the actual
# source of the Red Flags a company gets scored against.
CONCENTRATION_HIGH_PCT = float(_CONC_HIGH)
CONCENTRATION_MEDIUM_PCT = float(_CONC_MEDIUM)


def classify_runway(months: Decimal | float | None) -> str:
    """'healthy' (incl. profitable/no-runway-risk) | 'short' | 'low' | 'critical'."""
    if months is None:
        return "healthy"
    m = Decimal(str(months))
    if m < RUNWAY_CRITICAL_MONTHS:
        return "critical"
    if m < RUNWAY_LOW_MONTHS:
        return "low"
    if m < RUNWAY_HEALTHY_MONTHS:
        return "short"
    return "healthy"


def classify_burn_ratio(burn: Decimal | float, revenue: Decimal | float) -> str:
    """'normal' | 'high' | 'extreme'. Ratio of burn to revenue."""
    revenue = float(revenue)
    if revenue <= 0:
        return "normal"
    ratio = float(burn) / revenue
    if ratio >= BURN_EXTREME_RATIO:
        return "extreme"
    if ratio >= BURN_HIGH_RATIO:
        return "high"
    return "normal"


def classify_nrr(ratio: float) -> str:
    """'healthy' (>=100%) | 'below_par' | 'contraction' | 'severe'. *ratio* is 0-1 scale."""
    if ratio < NRR_SEVERE_PCT:
        return "severe"
    if ratio < NRR_CONTRACTION_PCT:
        return "contraction"
    if ratio < NRR_PAR_PCT:
        return "below_par"
    return "healthy"


def classify_churn(churned_pct: float) -> str:
    """'normal' | 'elevated' | 'high'. *churned_pct* is churned/peak-active, 0-1 scale."""
    if churned_pct >= CHURN_HIGH_PCT:
        return "high"
    if churned_pct >= CHURN_ELEVATED_PCT:
        return "elevated"
    return "normal"


def classify_concentration(top3_share: float) -> str:
    """'low' | 'medium' | 'high'. *top3_share* is the top-3 customers' revenue share, 0-1 scale."""
    if top3_share > CONCENTRATION_HIGH_PCT:
        return "high"
    if top3_share > CONCENTRATION_MEDIUM_PCT:
        return "medium"
    return "low"


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class HealthAlert:
    alert_name:   str            # snake_case identifier
    severity:     Severity
    metric_name:  str            # human-readable metric label
    metric_value: str            # formatted current value
    description:  str            # investor-facing explanation (LLM-polished)
    evidence:     dict[str, Any] = field(default_factory=dict)


@dataclass
class FinancialHealthReport:
    alerts: list[HealthAlert]

    @property
    def has_alerts(self) -> bool:
        return bool(self.alerts)

    @property
    def high_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == Severity.HIGH)

    @property
    def medium_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == Severity.MEDIUM)

    @property
    def low_count(self) -> int:
        return sum(1 for a in self.alerts if a.severity == Severity.LOW)


# ─────────────────────────────────────────────────────────────────────────────
# Rule-based detectors
# Each returns (alert_name, severity, metric_name, metric_value, raw_desc, evidence)
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_inr(v: Decimal) -> str:
    f = float(v)
    if f >= 1e7:
        return f"₹{f/1e7:.1f}Cr"
    if f >= 1e5:
        return f"₹{f/1e5:.1f}L"
    return f"₹{f/1e3:.0f}K"


def _detect_runway(m: FinancialMetrics) -> list[tuple]:
    if m.runway_months is None:
        return []
    r = float(m.runway_months)
    band = classify_runway(m.runway_months)
    if band == "critical":
        return [("critical_runway", Severity.HIGH, "Runway", f"{r:.1f} months",
                 f"Runway is critically low at {r:.1f} months. At the current burn rate of "
                 f"{_fmt_inr(m.avg_monthly_burn)}/month the company will run out of cash imminently "
                 f"without fresh capital or drastic cost cuts.",
                 {"runway_months": r, "monthly_burn": float(m.avg_monthly_burn),
                  "closing_balance": float(m.closing_balance)})]
    if band == "low":
        return [("low_runway", Severity.MEDIUM, "Runway", f"{r:.1f} months",
                 f"Runway of {r:.1f} months is uncomfortably short. The company needs to either "
                 f"close a fundraise or reduce its {_fmt_inr(m.avg_monthly_burn)}/month burn "
                 f"within the next 1–2 months to avoid a cash crisis.",
                 {"runway_months": r, "monthly_burn": float(m.avg_monthly_burn)})]
    if band == "short":
        return [("short_runway", Severity.LOW, "Runway", f"{r:.1f} months",
                 f"With {r:.1f} months of runway the company is not yet in crisis, but fundraising "
                 f"conversations should already be underway given typical closing timelines of 3–6 months.",
                 {"runway_months": r})]
    return []


def _detect_burn_vs_revenue(m: FinancialMetrics) -> list[tuple]:
    if m.avg_monthly_revenue == _ZERO:
        return []
    ratio = float(m.avg_monthly_burn / m.avg_monthly_revenue)
    band = classify_burn_ratio(m.avg_monthly_burn, m.avg_monthly_revenue)
    if band == "extreme":
        return [("extreme_burn_rate", Severity.HIGH, "Burn / Revenue ratio", f"{ratio:.1f}×",
                 f"The company burns {ratio:.1f}× its monthly revenue — spending "
                 f"{_fmt_inr(m.avg_monthly_burn)} to earn {_fmt_inr(m.avg_monthly_revenue)}. "
                 f"This level of cash consumption is unsustainable without very high-confidence "
                 f"growth evidence.",
                 {"burn_revenue_ratio": ratio, "avg_monthly_burn": float(m.avg_monthly_burn),
                  "avg_monthly_revenue": float(m.avg_monthly_revenue)})]
    if band == "high":
        return [("high_burn_rate", Severity.MEDIUM, "Burn / Revenue ratio", f"{ratio:.1f}×",
                 f"Monthly burn of {_fmt_inr(m.avg_monthly_burn)} is {ratio:.1f}× revenue of "
                 f"{_fmt_inr(m.avg_monthly_revenue)}. The company is spending significantly more "
                 f"than it earns and requires close monitoring of the path to unit-economics breakeven.",
                 {"burn_revenue_ratio": ratio})]
    return []


def _detect_revenue_trend(m: FinancialMetrics) -> list[tuple]:
    rates = m.mom_revenue_growth_rates
    if len(rates) < 2:
        return []

    # Count consecutive months of decline (most recent first)
    consecutive_declines = 0
    for r in reversed(rates):
        if r < _ZERO:
            consecutive_declines += 1
        else:
            break

    if consecutive_declines >= 3:
        avg_decline = float(sum(rates[-3:]) / 3 * 100)
        return [("sustained_revenue_decline", Severity.HIGH, "Revenue trend",
                 f"{consecutive_declines} months declining",
                 f"Revenue has declined for {consecutive_declines} consecutive months "
                 f"(avg {avg_decline:.1f}%/month). A multi-month contraction is a structural demand "
                 f"signal, not a one-off fluctuation, and warrants deep diligence on customer retention "
                 f"and product-market fit.",
                 {"consecutive_decline_months": consecutive_declines, "avg_monthly_decline_pct": avg_decline})]

    if consecutive_declines == 2:
        return [("revenue_decline", Severity.MEDIUM, "Revenue trend",
                 "2 months declining",
                 f"Revenue has fallen in each of the last 2 months. While not yet a confirmed trend, "
                 f"two consecutive declines warrant investigation into churn, pipeline, or seasonality.",
                 {"consecutive_decline_months": 2})]

    # Check for stagnation: all months within ±2% growth
    if len(rates) >= 3:
        recent = rates[-3:]
        if all(abs(r) < Decimal("0.02") for r in recent):
            return [("revenue_stagnation", Severity.LOW, "Revenue trend",
                     "Flat 3+ months",
                     f"Revenue has been essentially flat for the last {len(recent)} months (all MoM changes < 2%). "
                     f"Stagnation at this stage can indicate market saturation, sales execution gaps, "
                     f"or a product that has reached its natural ceiling.",
                     {"months_flat": len(recent)})]

    return []


def _detect_burn_acceleration(m: FinancialMetrics) -> list[tuple]:
    stats = m.monthly_stats
    if len(stats) < 3:
        return []
    burns = [float(s.burn) for s in stats]
    revs  = [float(s.revenue) for s in stats]

    # Check if burn is growing faster than revenue over last 3 months
    burn_growth = (burns[-1] - burns[-3]) / burns[-3] if burns[-3] else 0
    rev_growth  = (revs[-1]  - revs[-3])  / revs[-3]  if revs[-3]  else 0

    if burn_growth > 0.20 and burn_growth > rev_growth + 0.15:
        return [("accelerating_burn", Severity.MEDIUM, "Burn acceleration",
                 f"+{burn_growth*100:.0f}% over 3 months",
                 f"Operating costs have grown {burn_growth*100:.0f}% over the last 3 months while "
                 f"revenue grew only {rev_growth*100:.0f}%. Costs expanding faster than revenue compresses "
                 f"margins and shortens runway even if topline looks healthy.",
                 {"burn_3mo_growth_pct": round(burn_growth*100, 1),
                  "revenue_3mo_growth_pct": round(rev_growth*100, 1)})]
    return []


def _detect_consistently_cash_negative(m: FinancialMetrics) -> list[tuple]:
    stats = m.monthly_stats
    if len(stats) < 2:
        return []
    nets = [float(s.net) for s in stats]
    if all(n < 0 for n in nets):
        worst = min(nets)
        return [("consistently_cash_negative", Severity.MEDIUM, "Monthly cash flow",
                 f"Negative all {len(nets)} months",
                 f"The company has been cash-flow negative in every month of the statement period "
                 f"(worst: ₹{abs(worst):,.0f} outflow). While pre-profitability burn is expected for "
                 f"growth-stage companies, investors should confirm this is deliberate investment "
                 f"rather than an inability to monetise.",
                 {"months_negative": len(nets), "worst_monthly_outflow": worst})]
    return []


def _detect_nrr(ca: CustomerAnalyticsReport) -> list[tuple]:
    if not ca.nrr_per_month:
        return []
    recent_nrr = list(ca.nrr_per_month.values())[-1]
    pct = float(recent_nrr) * 100
    band = classify_nrr(recent_nrr)

    if band == "severe":
        return [("severe_nrr_contraction", Severity.HIGH, "Net Revenue Retention", f"{pct:.0f}%",
                 f"Period-over-period NRR of {pct:.0f}% means the existing customer base is contracting sharply — "
                 f"the company retains only ₹{pct:.0f} for every ₹100 of revenue from the prior month. "
                 f"At this rate, the company would lose all existing revenue without sustained new acquisition.",
                 {"nrr_pct": pct})]
    if band == "contraction":
        return [("nrr_contraction", Severity.MEDIUM, "Net Revenue Retention", f"{pct:.0f}%",
                 f"Period-over-period NRR of {pct:.0f}% indicates net revenue contraction from existing customers. "
                 f"The company must continuously acquire new customers just to maintain flat revenue, "
                 f"which increases CAC pressure and masks underlying retention problems.",
                 {"nrr_pct": pct})]
    if band == "below_par":
        return [("below_par_nrr", Severity.LOW, "Net Revenue Retention", f"{pct:.0f}%",
                 f"Period-over-period NRR of {pct:.0f}% is below the 100% breakeven. While not alarming, it means the "
                 f"company is not yet generating net expansion from existing customers — a key efficiency "
                 f"driver for SaaS businesses.",
                 {"nrr_pct": pct})]
    return []


def _detect_churn(ca: CustomerAnalyticsReport, m: FinancialMetrics) -> list[tuple]:
    if not ca.churn_events or not ca.monthly_active_customers:
        return []

    # Estimate % of customer base that churned
    max_active = max(ca.monthly_active_customers.values(), default=1)
    churn_count = len(ca.churn_events)
    churn_ratio = churn_count / max(max_active, 1)
    churn_pct = churn_ratio * 100
    band = classify_churn(churn_ratio)

    if band == "high":
        return [("high_churn", Severity.HIGH, "Customer churn",
                 f"{churn_count} customers ({churn_pct:.0f}%)",
                 f"{churn_count} customers ({churn_pct:.0f}% of peak base) have churned during the "
                 f"statement period. Churn at this level indicates a serious product-market fit or "
                 f"customer success problem and will severely constrain growth even with strong acquisition.",
                 {"churned_customers": churn_count, "churn_pct": round(churn_pct, 1),
                  "peak_active": max_active})]
    if band == "elevated":
        return [("elevated_churn", Severity.MEDIUM, "Customer churn",
                 f"{churn_count} customers ({churn_pct:.0f}%)",
                 f"{churn_count} customers ({churn_pct:.0f}% of peak base) stopped paying during the "
                 f"statement period. While some churn is normal, this rate will require above-average "
                 f"new customer acquisition to sustain revenue growth.",
                 {"churned_customers": churn_count, "churn_pct": round(churn_pct, 1)})]
    return []


def _detect_concentration(ca: CustomerAnalyticsReport) -> list[tuple]:
    if not ca.concentration_trajectory:
        return []
    top3 = ca.concentration_trajectory[-1].top3_share
    pct = top3 * 100
    band = classify_concentration(top3)

    if band == "high":
        return [("high_customer_concentration", Severity.HIGH, "Revenue concentration",
                 f"{pct:.0f}% (top 3 customers)",
                 f"The top 3 customers account for {pct:.0f}% of revenue — above the "
                 f"{CONCENTRATION_HIGH_PCT*100:.0f}% high-concentration threshold. Losing any one of them "
                 f"would be a material, possibly existential, revenue shock. Verify contract duration and "
                 f"exclusivity terms with these customers.",
                 {"top3_concentration_pct": round(pct, 1)})]
    if band == "medium":
        return [("moderate_customer_concentration", Severity.MEDIUM, "Revenue concentration",
                 f"{pct:.0f}% (top 3 customers)",
                 f"The top 3 customers account for {pct:.0f}% of revenue. This is a moderate concentration "
                 f"level — worth monitoring, and diversifying the customer base would reduce risk, but it "
                 f"is not yet at a critical level.",
                 {"top3_concentration_pct": round(pct, 1)})]
    return []


def _detect_aggregator_dominated_revenue(ca: CustomerAnalyticsReport) -> list[tuple]:
    if not ca.is_aggregator_dominated:
        return []
    pct = ca.aggregator_revenue_pct * 100
    return [("aggregator_dominated_revenue", Severity.HIGH, "Customer analytics reliability",
             f"{pct:.0f}% aggregator-settled",
             f"Customer analytics unreliable — revenue is {pct:.0f}% aggregator-settled "
             f"(Razorpay, Cashfree, PayU, or similar); individual customers are not visible "
             f"in bank data. Active customer counts, churn, NRR, and concentration figures "
             f"in this report exclude these settlements and should not be treated as a "
             f"complete picture of the customer base.",
             {"aggregator_revenue_pct": round(pct, 1)})]


def _detect_customer_count_trend(ca: CustomerAnalyticsReport) -> list[tuple]:
    if not ca.monthly_active_customers:
        return []
    months = sorted(ca.monthly_active_customers)
    if len(months) < 3:
        return []
    counts = [ca.monthly_active_customers[m] for m in months[-3:]]
    if counts[0] > counts[1] > counts[2]:
        drop = counts[0] - counts[2]
        drop_pct = drop / counts[0] * 100 if counts[0] else 0
        return [("shrinking_customer_base", Severity.MEDIUM, "Active customers",
                 f"−{drop} over 3 months",
                 f"Active customer count has declined for 3 consecutive months, falling from "
                 f"{counts[0]} to {counts[2]} (−{drop_pct:.0f}%). A contracting customer base is a "
                 f"leading indicator of future revenue decline and difficult to reverse without "
                 f"addressing the underlying acquisition or retention problem.",
                 {"count_3mo_ago": counts[0], "count_now": counts[2],
                  "absolute_drop": drop, "drop_pct": round(drop_pct, 1)})]
    return []


# ─────────────────────────────────────────────────────────────────────────────
# LLM description polisher
# ─────────────────────────────────────────────────────────────────────────────

def _polish_descriptions(
    alerts: list[tuple],
    metrics_context: str,
) -> list[str]:
    """Call Claude Haiku once with all alerts; returns polished descriptions."""
    try:
        import anthropic
    except ImportError:
        return [a[4] for a in alerts]

    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return [a[4] for a in alerts]

    items = [
        {"index": i, "alert": a[0], "severity": str(a[1]),
         "metric": a[2], "value": a[3], "raw_description": a[4]}
        for i, a in enumerate(alerts)
    ]

    prompt = (
        f"Company financial context:\n{metrics_context}\n\n"
        "Below are financial health alerts detected from a bank statement analysis. "
        "For each alert, rewrite the description as 2 concise sentences aimed at an early-stage "
        "investor doing due diligence. Be specific with numbers. Explain the risk clearly without "
        "jargon. Do not be alarmist but do not sugarcoat. Return a JSON array where each element "
        "has 'index' (int) and 'description' (string).\n\n"
        f"Alerts:\n{json.dumps(items, indent=2)}"
    )

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            system=(
                "You are a financial analyst writing investor-facing due diligence alerts. "
                "Respond only with valid JSON — no extra text."
            ),
            messages=[{"role": "user", "content": prompt}],
        )
        raw = json.loads(response.content[0].text.strip())
        desc_map = {item["index"]: item["description"] for item in raw}
        return [desc_map.get(i, alerts[i][4]) for i in range(len(alerts))]
    except Exception as exc:
        log.warning("LLM description polishing failed (%s) — using rule-based text", exc)
        return [a[4] for a in alerts]


# ─────────────────────────────────────────────────────────────────────────────
# Main analyst class
# ─────────────────────────────────────────────────────────────────────────────

class FinancialHealthAnalyst:
    """Runs all financial health detectors and returns a FinancialHealthReport."""

    def __init__(self, llm_enabled: bool = True) -> None:
        self._llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))
        if llm_enabled and not os.getenv("ANTHROPIC_API_KEY"):
            log.info("ANTHROPIC_API_KEY not set — FinancialHealthAnalyst using rule-based descriptions")

    def analyse(
        self,
        metrics: FinancialMetrics,
        customer_analytics: CustomerAnalyticsReport,
    ) -> FinancialHealthReport:
        raw: list[tuple] = []
        raw.extend(_detect_runway(metrics))
        raw.extend(_detect_burn_vs_revenue(metrics))
        raw.extend(_detect_revenue_trend(metrics))
        raw.extend(_detect_burn_acceleration(metrics))
        raw.extend(_detect_consistently_cash_negative(metrics))
        raw.extend(_detect_nrr(customer_analytics))
        raw.extend(_detect_churn(customer_analytics, metrics))
        raw.extend(_detect_customer_count_trend(customer_analytics))
        raw.extend(_detect_concentration(customer_analytics))
        raw.extend(_detect_aggregator_dominated_revenue(customer_analytics))

        if not raw:
            return FinancialHealthReport(alerts=[])

        # Sort: HIGH first, then MEDIUM, then LOW
        _order = {Severity.HIGH: 0, Severity.MEDIUM: 1, Severity.LOW: 2}
        raw.sort(key=lambda x: _order.get(x[1], 9))

        # Polish descriptions with LLM (single batched call)
        metrics_context = self._build_context(metrics, customer_analytics)
        if self._llm_enabled:
            descriptions = _polish_descriptions(raw, metrics_context)
        else:
            descriptions = [a[4] for a in raw]

        alerts = [
            HealthAlert(
                alert_name=r[0],
                severity=r[1],
                metric_name=r[2],
                metric_value=r[3],
                description=descriptions[i],
                evidence=r[5],
            )
            for i, r in enumerate(raw)
        ]

        return FinancialHealthReport(alerts=alerts)

    def _build_context(
        self,
        m: FinancialMetrics,
        ca: CustomerAnalyticsReport,
    ) -> str:
        lines = [
            f"- Period: {m.period_months} months",
            f"- Total revenue: {_fmt_inr(m.total_revenue)}",
            f"- Avg monthly revenue: {_fmt_inr(m.avg_monthly_revenue)}",
            f"- Avg monthly burn: {_fmt_inr(m.avg_monthly_burn)}",
            f"- Closing balance: {_fmt_inr(m.closing_balance)}",
            f"- Runway: {float(m.runway_months):.1f} months" if m.runway_months else "- Runway: profitable",
            f"- MoM revenue growth (avg): {float(m.avg_mom_growth)*100:+.1f}%" if m.avg_mom_growth else "",
        ]
        if ca.nrr_per_month:
            nrr = list(ca.nrr_per_month.values())[-1]
            lines.append(f"- Latest NRR: {float(nrr)*100:.0f}%")
        if ca.monthly_active_customers:
            latest = list(ca.monthly_active_customers.values())[-1]
            lines.append(f"- Latest active customers: {latest}")
        if ca.churn_events:
            lines.append(f"- Churn events in period: {len(ca.churn_events)}")
        return "\n".join(l for l in lines if l)
