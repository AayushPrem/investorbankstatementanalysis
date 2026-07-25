"""Reconciliation analyst — cross-checks company's stated claims against bank statement actuals.

Compares declared metrics (from pitch deck or manual input) against what the bank statement shows.
Each mismatch is a ReconciliationFinding with magnitude, direction, and investor framing.

Usage:
    from analysis.reconciliation import ReconciliationAnalyst, CompanyClaims

    claims = CompanyClaims(declared_revenue_total=Decimal("12000000"), declared_customer_count=45)
    report = ReconciliationAnalyst().analyse(doc, claims, customer_analytics, metrics)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from analysis.customer_analytics import CustomerAnalyticsReport
    from analysis.financial_analyst import FinancialMetrics
    from schema.canonical import StatementDocument

log = logging.getLogger(__name__)
_ZERO = Decimal("0")


class MismatchDirection(StrEnum):
    OVER_REPORTED = "over_reported"   # claimed > actual
    UNDER_REPORTED = "under_reported" # claimed < actual
    MATCH = "match"


@dataclass
class CompanyClaims:
    """Metrics as declared by the company in its pitch deck or data room."""
    declared_revenue_total: Decimal | None = None      # total revenue for the statement period
    declared_customer_count: int | None = None         # active customers at time of filing
    declared_nrr: float | None = None                  # stated NRR (0–1 scale or percentage; auto-detected)
    declared_never_lost_customer: bool | None = None   # "we've never lost a customer"
    declared_headcount: int | None = None              # employee count
    declared_avg_monthly_salary: Decimal | None = None # average monthly salary per employee
    declared_loan_count: int | None = None             # number of outstanding loans


@dataclass
class ReconciliationFinding:
    check_name: str
    claimed_value: str
    actual_value: str
    direction: MismatchDirection
    delta_pct: float | None               # % deviation (None if not numeric)
    severity: str                         # "HIGH" / "MEDIUM" / "LOW"
    description: str
    investor_framing: str


@dataclass
class ReconciliationReport:
    findings: list[ReconciliationFinding]
    claims_provided: bool

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")


def _severity_from_delta(delta_pct: float) -> str:
    abs_delta = abs(delta_pct)
    if abs_delta >= 30:
        return "HIGH"
    if abs_delta >= 15:
        return "MEDIUM"
    return "LOW"


def _pct(actual: Decimal, claimed: Decimal) -> float:
    """(claimed - actual) / actual * 100. Positive = over-reported."""
    if actual == _ZERO:
        return 0.0
    return float((claimed - actual) / actual * 100)


# ─────────────────────────────────────────────────────────────────────────────
# Individual checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_revenue(
    claims: CompanyClaims,
    metrics: "FinancialMetrics",
) -> ReconciliationFinding | None:
    if claims.declared_revenue_total is None:
        return None
    claimed = claims.declared_revenue_total
    actual = metrics.total_revenue
    delta = _pct(actual, claimed)
    if abs(delta) < 8:
        return None
    direction = MismatchDirection.OVER_REPORTED if delta > 0 else MismatchDirection.UNDER_REPORTED
    return ReconciliationFinding(
        check_name="Total Revenue",
        claimed_value=f"₹{float(claimed):,.0f}",
        actual_value=f"₹{float(actual):,.0f}",
        direction=direction,
        delta_pct=delta,
        severity=_severity_from_delta(delta),
        description=(
            f"Company claimed ₹{float(claimed):,.0f} revenue but bank statement shows ₹{float(actual):,.0f} "
            f"({abs(delta):.0f}% {'over' if delta > 0 else 'under'}-reported)."
        ),
        investor_framing=(
            f"Revenue is {'overstated' if delta > 0 else 'understated'} by {abs(delta):.0f}%. "
            + (
                "Overstated revenue in fundraising materials is a material misrepresentation and "
                "could void term-sheet representations. Request source invoices and reconcile."
                if delta > 0 else
                "Understated revenue may indicate off-book transactions or conservative accounting — "
                "both warrant explanation before closing."
            )
        ),
    )


def _check_customer_count(
    claims: CompanyClaims,
    customer_analytics: "CustomerAnalyticsReport",
) -> ReconciliationFinding | None:
    if claims.declared_customer_count is None:
        return None
    if not customer_analytics.monthly_active_customers:
        return None
    claimed = claims.declared_customer_count
    actual = max(customer_analytics.monthly_active_customers.values())
    delta = (claimed - actual) / max(actual, 1) * 100
    if abs(delta) < 12:
        return None
    direction = MismatchDirection.OVER_REPORTED if delta > 0 else MismatchDirection.UNDER_REPORTED
    return ReconciliationFinding(
        check_name="Customer Count",
        claimed_value=str(claimed),
        actual_value=str(actual),
        direction=direction,
        delta_pct=delta,
        severity=_severity_from_delta(delta),
        description=(
            f"Company claimed {claimed} customers; bank statement shows max {actual} distinct payers "
            f"({abs(delta):.0f}% {'over' if delta > 0 else 'under'}-reported)."
        ),
        investor_framing=(
            f"Customer count is {'overstated' if delta > 0 else 'understated'} by {abs(delta):.0f}%. "
            + (
                "Overstated customer count inflates perceived market traction. "
                "Request a signed customer list with contract values for verification."
                if delta > 0 else
                "Understated customer count could indicate shadow bookings or barter arrangements "
                "not reflected in bank credits."
            )
        ),
    )


def _check_nrr(
    claims: CompanyClaims,
    customer_analytics: "CustomerAnalyticsReport",
) -> ReconciliationFinding | None:
    if claims.declared_nrr is None or not customer_analytics.nrr_per_month:
        return None
    declared = claims.declared_nrr
    # Auto-detect if caller passed percentage (e.g. 110) vs ratio (e.g. 1.10)
    if declared > 5:
        declared = declared / 100
    actual_nrr = list(customer_analytics.nrr_per_month.values())[-1]
    delta_abs = abs(declared - actual_nrr)
    if delta_abs < 0.08:
        return None
    direction = MismatchDirection.OVER_REPORTED if declared > actual_nrr else MismatchDirection.UNDER_REPORTED
    return ReconciliationFinding(
        check_name="Net Revenue Retention",
        claimed_value=f"{declared*100:.0f}%",
        actual_value=f"{actual_nrr*100:.0f}%",
        direction=direction,
        delta_pct=(declared - actual_nrr) * 100,
        severity="HIGH" if delta_abs > 0.20 else "MEDIUM",
        description=(
            f"Company claimed {declared*100:.0f}% NRR; bank-derived NRR is {actual_nrr*100:.0f}% "
            f"({delta_abs*100:.0f}pp difference)."
        ),
        investor_framing=(
            f"NRR is {'overstated' if declared > actual_nrr else 'understated'} by {delta_abs*100:.0f}pp. "
            "NRR directly drives revenue predictability multiples. A {:.0f}pp overstatement ".format(delta_abs * 100)
            + "in a 10× ARR valuation scenario represents material mis-pricing of the asset."
        ),
    )


def _check_never_lost_customer(
    claims: CompanyClaims,
    customer_analytics: "CustomerAnalyticsReport",
) -> ReconciliationFinding | None:
    if not claims.declared_never_lost_customer:
        return None
    churned = customer_analytics.churn_events
    if not churned:
        return None
    return ReconciliationFinding(
        check_name="'Never Lost a Customer' Claim",
        claimed_value="True",
        actual_value=f"{len(churned)} churn events detected",
        direction=MismatchDirection.OVER_REPORTED,
        delta_pct=None,
        severity="HIGH",
        description=(
            f"Company claims it has never lost a customer; bank statement shows {len(churned)} "
            "customers who stopped paying (3+ months silence)."
        ),
        investor_framing=(
            f"The 'zero churn' claim is contradicted by {len(churned)} customer departures visible "
            "in the bank statement. This is a material misrepresentation that could constitute "
            "fraud in the context of investment representations. Request a reconciled customer list."
        ),
    )


def _check_salary_headcount(
    claims: CompanyClaims,
    document: "StatementDocument",
    metrics: "FinancialMetrics",
) -> ReconciliationFinding | None:
    if claims.declared_headcount is None or claims.declared_avg_monthly_salary is None:
        return None
    expected_annual = (
        claims.declared_headcount
        * claims.declared_avg_monthly_salary
        * Decimal("12")
    )
    months = max(metrics.period_months, 1)
    expected_period = expected_annual * Decimal(str(months)) / Decimal("12")

    # Compute actual salary from document (avoid requiring total_salary on FinancialMetrics)
    from schema.canonical import TransactionCategory
    actual = sum(
        t.debit for t in document.transactions
        if t.debit and t.category == TransactionCategory.SALARY
    ) or _ZERO
    if expected_period == _ZERO:
        return None
    delta = _pct(expected_period, actual + _ZERO)
    if abs(delta) < 18:
        return None
    direction = MismatchDirection.OVER_REPORTED if delta > 0 else MismatchDirection.UNDER_REPORTED
    return ReconciliationFinding(
        check_name="Salary vs Headcount",
        claimed_value=f"₹{float(expected_period):,.0f} ({claims.declared_headcount} × ₹{float(claims.declared_avg_monthly_salary):,.0f}/mo)",
        actual_value=f"₹{float(actual):,.0f}",
        direction=direction,
        delta_pct=delta,
        severity=_severity_from_delta(delta),
        description=(
            f"Declared headcount × avg salary implies ₹{float(expected_period):,.0f} salary outflow "
            f"but bank shows ₹{float(actual):,.0f} ({abs(delta):.0f}% gap)."
        ),
        investor_framing=(
            f"Salary spend is {abs(delta):.0f}% {'higher' if delta > 0 else 'lower'} than the declared "
            "headcount × average salary would imply. "
            + (
                "Higher-than-expected spend suggests undisclosed headcount or off-payroll contractors."
                if delta > 0 else
                "Lower-than-expected spend may indicate declared headcount is overstated or salaries "
                "are partially paid in cash — both flag poor governance."
            )
        ),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Analyst
# ─────────────────────────────────────────────────────────────────────────────

class ReconciliationAnalyst:

    def __init__(self, llm_enabled: bool = True) -> None:
        self._llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))

    def analyse(
        self,
        document: "StatementDocument",
        claims: CompanyClaims | None,
        customer_analytics: "CustomerAnalyticsReport",
        metrics: "FinancialMetrics",
    ) -> ReconciliationReport:
        if claims is None:
            return ReconciliationReport(findings=[], claims_provided=False)

        checks = [
            _check_revenue(claims, metrics),
            _check_customer_count(claims, customer_analytics),
            _check_nrr(claims, customer_analytics),
            _check_never_lost_customer(claims, customer_analytics),
            _check_salary_headcount(claims, document, metrics),
        ]
        findings = [f for f in checks if f is not None]

        if findings and self._llm_enabled:
            self._polish_investor_framing(findings, metrics)

        return ReconciliationReport(findings=findings, claims_provided=True)

    def _polish_investor_framing(
        self,
        findings: list[ReconciliationFinding],
        metrics: "FinancialMetrics",
    ) -> None:
        try:
            import anthropic
        except ImportError:
            return

        items = [
            {"index": i, "check": f.check_name, "claimed": f.claimed_value,
             "actual": f.actual_value, "direction": str(f.direction), "delta_pct": f.delta_pct}
            for i, f in enumerate(findings)
        ]
        prompt = (
            "Below are reconciliation mismatches between a startup's pitch-deck claims and its bank "
            "statement actuals. For each item, rewrite the 'investor_framing' as 2 concise sentences "
            "that explain the investment risk created by this mismatch. Be direct and quantitative. "
            "Return JSON array: [{index, framing}].\n\n"
            f"Mismatches:\n{json.dumps(items, indent=2)}"
        )
        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1000,
                system="You are a VC due-diligence analyst. Respond only with valid JSON.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = json.loads(response.content[0].text.strip())
            framing_map = {item["index"]: item["framing"] for item in raw}
            for i, finding in enumerate(findings):
                if i in framing_map:
                    finding.investor_framing = framing_map[i]
        except Exception as err:
            log.warning("LLM polish for reconciliation failed: %s", err)
