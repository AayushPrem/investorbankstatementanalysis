"""Report narrative — single-company synthesis for the Summary tab/sheet in
every report lens (Angel, VC, Workbench).

Produces one plain-English paragraph describing what the evidence shows —
financial health, risk flags, customer behaviour, and (where available)
compliance/reconciliation posture — via Claude Haiku, falling back to a
rule-based summary when no API key is set. Same LLM-with-fallback pattern as
analysis.risk.RiskAnalyst._generate_narrative and
analysis.portfolio_narrative.generate_portfolio_narrative.

Deliberately descriptive, not evaluative: this module reports what was
found, not whether the company is "investable" — that judgment belongs to
the reader, not the tool. See Wave 3.3/4.5 (`the product surfaces evidence;
it does not render investment verdicts`).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"

_SYSTEM_PROMPT = (
    "You are a due-diligence analyst summarising an automated bank-statement "
    "analysis for an investor. Write ONE paragraph, 4-6 sentences, plain "
    "English, quantified where possible. Describe what the evidence shows — "
    "financial health, risk flags, customer behaviour, and compliance/"
    "reconciliation findings if present. Do NOT render an investment "
    "verdict, recommendation, or overall rating (no 'investable', 'strong "
    "buy', 'pass', star ratings, or similar) — describe the findings, let "
    "the reader judge them."
)


@dataclass
class ReportNarrativeInput:
    """Flattened view of one company's analysis, for narrative generation."""
    company_name: str
    total_revenue: float
    avg_monthly_burn: float
    runway_months: float | None
    avg_mom_growth: float | None
    active_customers: int | None
    latest_nrr: float | None
    risk_flag_counts: dict[str, int]        # {"HIGH": n, "MEDIUM": n, "LOW": n}
    health_alert_counts: dict[str, int]      # {"HIGH": n, "MEDIUM": n, "LOW": n}
    compliance_exception_counts: dict[str, int] | None = None
    reconciliation_finding_counts: dict[str, int] | None = None
    top_risk_descriptions: list[str] = field(default_factory=list)
    top_health_descriptions: list[str] = field(default_factory=list)


def severity_counts(items: list, severity_attr: str = "severity") -> dict[str, int]:
    """Count a list of flag/alert/exception/finding objects by severity.

    Shared by every report lens and the demo UI so "N risk flags, X high /
    Y medium / Z low" is computed the same way everywhere — the flag-count
    summary that replaced the verdict banner and composite risk score.
    """
    counts = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    for item in items:
        sev = str(getattr(item, severity_attr)).upper()
        if sev in counts:
            counts[sev] += 1
    return counts


def generate_report_narrative(data: ReportNarrativeInput, llm_enabled: bool = True) -> str:
    """Produce the Summary paragraph for one company's report."""
    llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))
    if llm_enabled:
        try:
            return _llm_narrative(data)
        except Exception as exc:  # noqa: BLE001 — always fall back, never break the report
            log.warning("Report narrative LLM call failed (%s) — using fallback", exc)

    return _fallback_narrative(data)


def _fmt_inr(v: float) -> str:
    if v >= 1e7:
        return f"₹{v/1e7:.1f}Cr"
    if v >= 1e5:
        return f"₹{v/1e5:.1f}L"
    return f"₹{v:,.0f}"


def _llm_narrative(data: ReportNarrativeInput) -> str:
    import anthropic

    facts = {
        "company": data.company_name,
        "total_revenue": _fmt_inr(data.total_revenue),
        "avg_monthly_burn": _fmt_inr(data.avg_monthly_burn) + "/mo",
        "runway_months": f"{data.runway_months:.1f}" if data.runway_months is not None else "profitable / n/a",
        "avg_mom_growth_pct": f"{data.avg_mom_growth*100:.1f}%" if data.avg_mom_growth is not None else "n/a",
        "active_customers": data.active_customers if data.active_customers is not None else "n/a",
        "latest_nrr_pct": f"{data.latest_nrr*100:.0f}%" if data.latest_nrr is not None else "n/a",
        "risk_flags": data.risk_flag_counts,
        "risk_flag_examples": data.top_risk_descriptions[:5],
        "financial_health_alerts": data.health_alert_counts,
        "financial_health_alert_examples": data.top_health_descriptions[:5],
        "compliance_exceptions": data.compliance_exception_counts,
        "reconciliation_findings": data.reconciliation_finding_counts,
    }

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=_MODEL,
        max_tokens=400,
        system=_SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": f"Analysis facts for {data.company_name}:\n\n{facts}\n\nWrite the summary paragraph.",
        }],
    )
    text: str = response.content[0].text.strip()
    return text


def _fallback_narrative(data: ReportNarrativeInput) -> str:
    parts: list[str] = []

    rev = _fmt_inr(data.total_revenue)
    burn = _fmt_inr(data.avg_monthly_burn)
    if data.runway_months is not None:
        parts.append(
            f"{data.company_name} shows {rev} in total revenue against an average "
            f"monthly burn of {burn}, giving {data.runway_months:.1f} months of runway "
            f"at the current rate."
        )
    else:
        parts.append(
            f"{data.company_name} shows {rev} in total revenue against an average "
            f"monthly burn of {burn}; the statement shows no sustained net cash outflow."
        )

    if data.avg_mom_growth is not None:
        direction = "growing" if data.avg_mom_growth > 0 else ("flat" if data.avg_mom_growth == 0 else "declining")
        parts.append(f"Revenue is {direction} at {data.avg_mom_growth*100:+.1f}% month-over-month on average.")

    if data.active_customers is not None or data.latest_nrr is not None:
        cust_bits = []
        if data.active_customers is not None:
            cust_bits.append(f"{data.active_customers} active customers in the latest month")
        if data.latest_nrr is not None:
            cust_bits.append(f"net revenue retention of {data.latest_nrr*100:.0f}%")
        if cust_bits:
            parts.append("Customer analytics show " + " and ".join(cust_bits) + ".")

    risk_total = sum(data.risk_flag_counts.values())
    if risk_total:
        breakdown = ", ".join(f"{n} {sev}" for sev, n in data.risk_flag_counts.items() if n)
        parts.append(f"The risk analysis found {risk_total} flag(s) ({breakdown}).")
    else:
        parts.append("The risk analysis found no flags across the automated detectors in scope.")

    health_total = sum(data.health_alert_counts.values())
    if health_total:
        breakdown = ", ".join(f"{n} {sev}" for sev, n in data.health_alert_counts.items() if n)
        parts.append(f"{health_total} financial health alert(s) were raised ({breakdown}).")

    if data.compliance_exception_counts is not None:
        comp_total = sum(data.compliance_exception_counts.values())
        if comp_total:
            breakdown = ", ".join(f"{n} {sev}" for sev, n in data.compliance_exception_counts.items() if n)
            parts.append(f"{comp_total} Indian compliance exception(s) were identified ({breakdown}).")
        else:
            parts.append("No compliance exceptions were identified.")

    if data.reconciliation_finding_counts is not None:
        recon_total = sum(data.reconciliation_finding_counts.values())
        if recon_total:
            breakdown = ", ".join(f"{n} {sev}" for sev, n in data.reconciliation_finding_counts.items() if n)
            parts.append(f"Reconciliation against declared claims surfaced {recon_total} mismatch(es) ({breakdown}).")

    return " ".join(parts)
