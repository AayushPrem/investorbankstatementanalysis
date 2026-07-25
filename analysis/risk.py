"""Risk analyst — six independent flag detectors for suspicious patterns.

Detector overview:
  structuring             — credits clustered just under ₹2L threshold (§269ST)
  round_tripping          — matching outflow/inflow from same counterparty in 3-14d
  spike_drain             — monthly net-flow z-score > 2.5σ (leave-one-out baseline)
  customer_concentration  — top-3 customers account for > 60% of revenue
  round_amount_clustering — > 30% of revenue transactions are exact ₹1,000 multiples
  founder_over_extraction — FOUNDER_WITHDRAWAL > 3× declared salary (or > ₹5L/month)

All detector functions return list[Flag] and are independently testable.
RiskAnalyst runs all detectors, computes a weighted composite score (capped at 100),
and optionally generates a plain-English narrative via Claude Haiku.
"""
from __future__ import annotations

import logging
import os
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from schema.canonical import StatementDocument, TransactionCategory

log = logging.getLogger(__name__)

_ZERO = Decimal("0")
_THOUSAND = Decimal("1000")

# ── Structuring thresholds (§269ST) ──────────────────────────────────────────
_STRUCT_LOWER = Decimal("150000")   # ₹1.5 lakh
_STRUCT_UPPER = Decimal("200000")   # ₹2 lakh
_STRUCT_WINDOW_DAYS = 7
_STRUCT_MIN_COUNT = 4

# ── Round-trip parameters ─────────────────────────────────────────────────────
_RT_MIN_DAYS = 3
_RT_MAX_DAYS = 14
_RT_AMOUNT_TOL = Decimal("0.05")  # 5% tolerance

# ── Spike / drain ─────────────────────────────────────────────────────────────
_SPIKE_Z = 2.5
_SPIKE_Z_HIGH = 3.5

# ── Customer concentration ─────────────────────────────────────────────────────
_CONC_HIGH = Decimal("0.80")
_CONC_MEDIUM = Decimal("0.60")

# ── Round amount clustering ───────────────────────────────────────────────────
_ROUND_THRESHOLD = 0.30
_ROUND_HIGH_THRESHOLD = 0.60

# ── Founder extraction ────────────────────────────────────────────────────────
_FOUNDER_MONTHLY_LIMIT = Decimal("500000")   # ₹5 lakh with no declared salary
_FOUNDER_SALARY_MULTIPLE = Decimal("3")


# ─────────────────────────────────────────────────────────────────────────────
# Data model
# ─────────────────────────────────────────────────────────────────────────────

class Severity(StrEnum):
    LOW    = "LOW"
    MEDIUM = "MEDIUM"
    HIGH   = "HIGH"


@dataclass
class Flag:
    detector_name:             str
    severity:                  Severity
    triggering_transaction_ids: list[str]
    description:               str
    evidence:                  dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskReport:
    flags:           list[Flag]
    composite_score: float   # 0–100 weighted sum (HIGH=10, MEDIUM=5, LOW=2)
    narrative:       str


# ─────────────────────────────────────────────────────────────────────────────
# Detector 1 — Structuring
# ─────────────────────────────────────────────────────────────────────────────

def detect_structuring(doc: StatementDocument) -> list[Flag]:
    """Flag clusters of ≥4 credits between ₹1.5L and ₹2L within a 7-day window.

    §269ST (Income Tax Act) restricts cash receipts ≥ ₹2 lakh.  Structuring
    means splitting what would be one large receipt into several smaller ones
    that each stay just below the threshold.
    """
    suspect = sorted(
        [
            t for t in doc.transactions
            if t.credit is not None
            and t.credit > _ZERO
            and _STRUCT_LOWER <= t.credit <= _STRUCT_UPPER
        ],
        key=lambda t: t.date,
    )

    if len(suspect) < _STRUCT_MIN_COUNT:
        return []

    flags: list[Flag] = []
    reported: set[str] = set()

    for i, anchor in enumerate(suspect):
        if anchor.transaction_id in reported:
            continue

        window = [
            t for t in suspect[i:]
            if (t.date - anchor.date).days <= _STRUCT_WINDOW_DAYS
        ]

        if len(window) >= _STRUCT_MIN_COUNT:
            for t in window:
                reported.add(t.transaction_id)
            total = sum((t.credit for t in window if t.credit), _ZERO)
            flags.append(Flag(
                detector_name="structuring",
                severity=Severity.HIGH,
                triggering_transaction_ids=[t.transaction_id for t in window],
                description=(
                    f"{len(window)} credits between "
                    f"₹{int(_STRUCT_LOWER/100000)}L and ₹{int(_STRUCT_UPPER/100000)}L "
                    f"within {_STRUCT_WINDOW_DAYS} days "
                    f"({anchor.date} → {window[-1].date}) — possible §269ST structuring"
                ),
                evidence={
                    "cluster_size":      len(window),
                    "date_start":        str(anchor.date),
                    "date_end":          str(window[-1].date),
                    "total_amount":      float(total),
                    "threshold_lower":   float(_STRUCT_LOWER),
                    "threshold_upper":   float(_STRUCT_UPPER),
                },
            ))

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Detector 2 — Round-tripping
# ─────────────────────────────────────────────────────────────────────────────

def detect_round_tripping(doc: StatementDocument) -> list[Flag]:
    """Flag (outflow, inflow) pairs from the same counterparty within 3-14 days,
    amounts within 5%.
    """
    debits  = [t for t in doc.transactions if t.debit  is not None and t.counterparty]
    credits = [t for t in doc.transactions if t.credit is not None and t.counterparty]

    if not debits or not credits:
        return []

    flags: list[Flag] = []
    reported_pairs: set[tuple[str, str]] = set()

    for d in debits:
        assert d.debit is not None  # for type checker
        for c in credits:
            assert c.credit is not None
            if d.counterparty != c.counterparty:
                continue
            days_diff = (c.date - d.date).days
            if not (_RT_MIN_DAYS <= days_diff <= _RT_MAX_DAYS):
                continue
            deviation = abs(d.debit - c.credit) / d.debit
            if deviation > _RT_AMOUNT_TOL:
                continue

            pair = (d.transaction_id, c.transaction_id)
            if pair in reported_pairs:
                continue
            reported_pairs.add(pair)

            flags.append(Flag(
                detector_name="round_tripping",
                severity=Severity.HIGH,
                triggering_transaction_ids=[d.transaction_id, c.transaction_id],
                description=(
                    f"Possible round-trip: ₹{float(d.debit):,.0f} outflow to "
                    f"'{d.counterparty}' on {d.date}, "
                    f"returned ₹{float(c.credit):,.0f} {days_diff} days later"
                ),
                evidence={
                    "counterparty":         d.counterparty,
                    "outflow_amount":       float(d.debit),
                    "inflow_amount":        float(c.credit),
                    "days_between":         days_diff,
                    "amount_deviation_pct": float(deviation * 100),
                    "outflow_date":         str(d.date),
                    "inflow_date":          str(c.date),
                },
            ))

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Detector 3 — Spikes and drains
# ─────────────────────────────────────────────────────────────────────────────

def detect_spikes_drains(doc: StatementDocument) -> list[Flag]:
    """Flag months whose net flow deviates > 2.5σ from the leave-one-out baseline.

    Requires at least 3 months of data; skips months where the baseline stdev is 0.
    """
    monthly_net: dict[str, float]       = {}
    monthly_ids: dict[str, list[str]]   = {}

    for t in doc.transactions:
        ym = t.date.strftime("%Y-%m")
        monthly_net.setdefault(ym, 0.0)
        monthly_ids.setdefault(ym, []).append(t.transaction_id)
        if t.credit is not None:
            monthly_net[ym] += float(t.credit)
        elif t.debit is not None:
            monthly_net[ym] -= float(t.debit)

    if len(monthly_net) < 3:
        return []

    months = sorted(monthly_net)
    values = [monthly_net[m] for m in months]

    flags: list[Flag] = []
    for i, m in enumerate(months):
        baseline = [v for j, v in enumerate(values) if j != i]
        mu    = statistics.mean(baseline)
        sigma = statistics.pstdev(baseline)

        if sigma == 0.0:
            continue

        z = (values[i] - mu) / sigma
        if abs(z) <= _SPIKE_Z:
            continue

        direction = "spike" if z > 0 else "drain"
        severity  = Severity.HIGH if abs(z) > _SPIKE_Z_HIGH else Severity.MEDIUM

        flags.append(Flag(
            detector_name="spike_drain",
            severity=severity,
            triggering_transaction_ids=monthly_ids[m],
            description=(
                f"Unusual {direction} in {m}: "
                f"net ₹{monthly_net[m]:+,.0f} "
                f"(z-score {z:.2f}, baseline ₹{mu:,.0f} ± ₹{sigma:,.0f})"
            ),
            evidence={
                "month":           m,
                "net_flow":        monthly_net[m],
                "z_score":         round(z, 3),
                "direction":       direction,
                "baseline_mean":   round(mu, 2),
                "baseline_stdev":  round(sigma, 2),
            },
        ))

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# Detector 4 — Customer concentration
# ─────────────────────────────────────────────────────────────────────────────

def detect_customer_concentration(doc: StatementDocument) -> list[Flag]:
    """Flag when top-3 customers account for > 60% of revenue.

    Requires customer_id to be populated on REVENUE transactions (by the
    CustomerIdentityResolver).  Returns [] if no customer_ids are set.
    """
    rev_by_cust: dict[str, Decimal] = {}
    for t in doc.transactions:
        if (
            t.category == TransactionCategory.REVENUE
            and t.credit is not None
            and t.customer_id
        ):
            rev_by_cust[t.customer_id] = (
                rev_by_cust.get(t.customer_id, _ZERO) + t.credit
            )

    if not rev_by_cust:
        return []

    total = sum(rev_by_cust.values(), _ZERO)
    if total == _ZERO:
        return []

    ranked = sorted(rev_by_cust.items(), key=lambda x: x[1], reverse=True)
    top3_share = sum(v for _, v in ranked[:3]) / total
    top1_share = ranked[0][1] / total

    if top3_share > _CONC_HIGH:
        severity = Severity.HIGH
    elif top3_share > _CONC_MEDIUM:
        severity = Severity.MEDIUM
    else:
        return []

    top3_ids = {cid for cid, _ in ranked[:3]}
    txn_ids = [
        t.transaction_id
        for t in doc.transactions
        if t.customer_id in top3_ids and t.category == TransactionCategory.REVENUE
    ]

    return [Flag(
        detector_name="customer_concentration",
        severity=severity,
        triggering_transaction_ids=txn_ids,
        description=(
            f"Top-3 customers: {float(top3_share)*100:.1f}% of revenue "
            f"(largest single customer: {float(top1_share)*100:.1f}%)"
        ),
        evidence={
            "top3_share_pct":     float(top3_share * 100),
            "top1_share_pct":     float(top1_share * 100),
            "total_customers":    len(rev_by_cust),
            "threshold_high_pct": float(_CONC_HIGH * 100),
            "threshold_med_pct":  float(_CONC_MEDIUM * 100),
        },
    )]


# ─────────────────────────────────────────────────────────────────────────────
# Detector 5 — Round-amount clustering
# ─────────────────────────────────────────────────────────────────────────────

def detect_round_amount_clustering(doc: StatementDocument) -> list[Flag]:
    """Flag when > 30% of REVENUE transactions are exact multiples of ₹1,000.

    Legitimate businesses collect payments at irregular amounts (invoices,
    GST-inclusive totals, pro-rata charges).  A high proportion of round
    amounts may indicate fabricated or manually adjusted revenue.
    """
    rev_txns = [
        t for t in doc.transactions
        if t.category == TransactionCategory.REVENUE
        and t.credit is not None
        and t.credit > _ZERO
    ]

    if not rev_txns:
        return []

    round_txns = [t for t in rev_txns if t.credit % _THOUSAND == _ZERO]  # type: ignore[operator]
    proportion  = len(round_txns) / len(rev_txns)

    if proportion <= _ROUND_THRESHOLD:
        return []

    severity = Severity.HIGH if proportion > _ROUND_HIGH_THRESHOLD else Severity.MEDIUM

    return [Flag(
        detector_name="round_amount_clustering",
        severity=severity,
        triggering_transaction_ids=[t.transaction_id for t in round_txns],
        description=(
            f"{len(round_txns)}/{len(rev_txns)} revenue transactions "
            f"({proportion*100:.0f}%) are exact ₹1,000 multiples — "
            "atypically high for real commercial receipts"
        ),
        evidence={
            "round_count":       len(round_txns),
            "total_rev_count":   len(rev_txns),
            "proportion_pct":    round(proportion * 100, 1),
            "threshold_pct":     _ROUND_THRESHOLD * 100,
        },
    )]


# ─────────────────────────────────────────────────────────────────────────────
# Detector 6 — Founder over-extraction
# ─────────────────────────────────────────────────────────────────────────────

def detect_founder_over_extraction(
    doc: StatementDocument,
    declared_founder_salary: Decimal | None = None,
) -> list[Flag]:
    """Flag months where FOUNDER_WITHDRAWAL > 3× declared salary or > ₹5L."""
    monthly_total: dict[str, Decimal]      = {}
    monthly_ids:   dict[str, list[str]]    = {}

    for t in doc.transactions:
        if t.category == TransactionCategory.FOUNDER_WITHDRAWAL and t.debit is not None:
            ym = t.date.strftime("%Y-%m")
            monthly_total[ym] = monthly_total.get(ym, _ZERO) + t.debit
            monthly_ids.setdefault(ym, []).append(t.transaction_id)

    if not monthly_total:
        return []

    flags: list[Flag] = []
    for ym, total in monthly_total.items():
        if declared_founder_salary is not None and declared_founder_salary > _ZERO:
            threshold = declared_founder_salary * _FOUNDER_SALARY_MULTIPLE
            if total > threshold:
                multiple = float(total / declared_founder_salary)
                flags.append(Flag(
                    detector_name="founder_over_extraction",
                    severity=Severity.HIGH,
                    triggering_transaction_ids=monthly_ids[ym],
                    description=(
                        f"Founder withdrew ₹{float(total):,.0f} in {ym} — "
                        f"{multiple:.1f}× declared salary of "
                        f"₹{float(declared_founder_salary):,.0f}"
                    ),
                    evidence={
                        "month":              ym,
                        "withdrawal_amount":  float(total),
                        "declared_salary":    float(declared_founder_salary),
                        "multiple_of_salary": multiple,
                        "threshold":          float(threshold),
                    },
                ))
        else:
            if total > _FOUNDER_MONTHLY_LIMIT:
                flags.append(Flag(
                    detector_name="founder_over_extraction",
                    severity=Severity.MEDIUM,
                    triggering_transaction_ids=monthly_ids[ym],
                    description=(
                        f"Founder withdrew ₹{float(total):,.0f} in {ym}, "
                        f"exceeding ₹{int(_FOUNDER_MONTHLY_LIMIT/100000)}L/month "
                        f"(no declared salary on file)"
                    ),
                    evidence={
                        "month":             ym,
                        "withdrawal_amount": float(total),
                        "declared_salary":   None,
                        "threshold":         float(_FOUNDER_MONTHLY_LIMIT),
                    },
                ))

    return flags


# ─────────────────────────────────────────────────────────────────────────────
# RiskAnalyst — orchestrates all detectors
# ─────────────────────────────────────────────────────────────────────────────

_SEVERITY_WEIGHTS: dict[Severity, float] = {
    Severity.HIGH:   10.0,
    Severity.MEDIUM:  5.0,
    Severity.LOW:     2.0,
}


def compute_composite_score(flags: list[Flag]) -> float:
    """Weighted sum of flag severities, capped at 100 (HIGH=10, MEDIUM=5, LOW=2).

    Public so callers that rebuild a flag list after applying custom fund
    detectors (see workbench_config.py) can recompute the score consistently
    instead of duplicating the weight table.
    """
    return min(100.0, sum(_SEVERITY_WEIGHTS.get(f.severity, 0.0) for f in flags))


class RiskAnalyst:
    """Runs all six detectors and produces a RiskReport with composite score."""

    def __init__(self, llm_enabled: bool = True) -> None:
        self._llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))
        if llm_enabled and not os.getenv("ANTHROPIC_API_KEY"):
            log.warning(
                "ANTHROPIC_API_KEY not set — RiskAnalyst narrative will use fallback text"
            )

    def analyse(
        self,
        doc: StatementDocument,
        declared_founder_salary: Decimal | None = None,
    ) -> RiskReport:
        """Run all detectors and return a RiskReport."""
        flags: list[Flag] = (
            detect_structuring(doc)
            + detect_round_tripping(doc)
            + detect_spikes_drains(doc)
            + detect_customer_concentration(doc)
            + detect_round_amount_clustering(doc)
            + detect_founder_over_extraction(doc, declared_founder_salary)
        )

        score = compute_composite_score(flags)

        narrative = self._generate_narrative(flags)
        return RiskReport(flags=flags, composite_score=score, narrative=narrative)

    # ── Narrative generation ──────────────────────────────────────────────────

    def _generate_narrative(self, flags: list[Flag]) -> str:
        if not flags:
            return (
                "No risk flags detected. The statement appears clean based on "
                "automated pattern analysis."
            )
        if not self._llm_enabled:
            return _fallback_narrative(flags)
        try:
            import anthropic
        except ImportError:
            return _fallback_narrative(flags)

        try:
            flag_lines = "\n".join(
                f"- [{f.severity}] {f.detector_name}: {f.description}"
                for f in flags
            )
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=400,
                system=(
                    "You are a financial risk analyst summarising findings for an "
                    "early-stage investor. Write in plain English — 3 to 5 sentences. "
                    "Be specific about the risk, not just the detector name. "
                    "Quantify where possible. Be professional and concise."
                ),
                messages=[{
                    "role": "user",
                    "content": (
                        "Summarise these risk flags from an automated bank statement "
                        f"analysis:\n\n{flag_lines}\n\n"
                        "Write a 3-5 sentence investor-facing risk narrative."
                    ),
                }],
            )
            return response.content[0].text.strip()
        except Exception as exc:
            log.warning("Narrative generation failed (%s) — using fallback", exc)
            return _fallback_narrative(flags)


def _fallback_narrative(flags: list[Flag]) -> str:
    high   = [f for f in flags if f.severity == Severity.HIGH]
    medium = [f for f in flags if f.severity == Severity.MEDIUM]
    low    = [f for f in flags if f.severity == Severity.LOW]

    parts = [f"Risk analysis identified {len(flags)} flag(s)."]
    for label, group in (("HIGH", high), ("MEDIUM", medium), ("LOW", low)):
        if group:
            descs = "; ".join(f.description for f in group[:2])
            suffix = " [+more]" if len(group) > 2 else ""
            parts.append(f"{label}: {descs}{suffix}.")
    return " ".join(parts)
