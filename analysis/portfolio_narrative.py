"""Portfolio narrative — cross-company synthesis for the Network Lens.

Takes a flattened per-company risk/compliance view (PortfolioCompany — owned
here, not the reports.network_lens.CompanySummary, to keep the dependency
direction reports -> analysis and avoid a cycle) and produces a plain-English
portfolio-level narrative via Claude Haiku, falling back to a rule-based
summary when no API key is set. Same LLM-with-fallback pattern as
analysis.risk.RiskAnalyst._generate_narrative.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_MODEL = "claude-haiku-4-5-20251001"
_HIGH_RISK_THRESHOLD = 50.0


@dataclass
class PortfolioCompany:
    """One company's risk/compliance posture, flattened for cross-company synthesis."""
    name: str
    risk_score: float
    red_flag_descriptions: list[str] = field(default_factory=list)
    compliance_status: str = "unknown"  # "clean" | "warnings" | "violations" | "unknown"
    compliance_rule_names: list[str] = field(default_factory=list)
    runway_months: float | None = None
    churn_rate: float | None = None


def generate_portfolio_narrative(
    companies: list[PortfolioCompany], llm_enabled: bool = True,
) -> str:
    """Produce a portfolio-level narrative for an investment committee."""
    if not companies:
        return "No companies in this cohort."

    llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))
    if llm_enabled:
        try:
            return _llm_narrative(companies)
        except Exception as exc:  # noqa: BLE001 — always fall back, never break the report
            log.warning("Portfolio narrative LLM call failed (%s) — using fallback", exc)

    return _fallback_narrative(companies)


def _llm_narrative(companies: list[PortfolioCompany]) -> str:
    import anthropic

    lines = []
    for c in companies:
        flags = "; ".join(c.red_flag_descriptions[:5]) or "none"
        rules = ", ".join(c.compliance_rule_names) or "none"
        runway = f"{c.runway_months:.1f}mo" if c.runway_months is not None else "n/a"
        churn = f"{c.churn_rate*100:.0f}%" if c.churn_rate is not None else "n/a"
        lines.append(
            f"- {c.name}: risk score {c.risk_score:.0f}/100, flags: {flags}; "
            f"compliance: {c.compliance_status} ({rules}); runway {runway}; churn {churn}"
        )
    company_block = "\n".join(lines)

    client = anthropic.Anthropic()
    response = client.messages.create(
        model=_MODEL,
        max_tokens=500,
        system=(
            "You are a VC portfolio analyst briefing an investment committee on a "
            "cohort of portfolio companies. Write in plain English, 4-6 sentences. "
            "Call out: common risk themes across companies, the specific companies "
            "of greatest concern (name them), any compliance issues worth flagging, "
            "and one concrete recommendation. Be specific and quantify where possible."
        ),
        messages=[{
            "role": "user",
            "content": (
                f"Portfolio of {len(companies)} companies, one line each "
                f"(risk score, flags, compliance status, runway, churn):\n\n{company_block}\n\n"
                "Write the portfolio-level narrative."
            ),
        }],
    )
    text: str = response.content[0].text.strip()
    return text


def _fallback_narrative(companies: list[PortfolioCompany]) -> str:
    n = len(companies)
    high_risk = sorted(
        (c for c in companies if c.risk_score >= _HIGH_RISK_THRESHOLD),
        key=lambda c: -c.risk_score,
    )
    violations = [c for c in companies if c.compliance_status == "violations"]

    flag_counts: dict[str, int] = {}
    for c in companies:
        for desc in c.red_flag_descriptions:
            flag_counts[desc] = flag_counts.get(desc, 0) + 1

    parts = [f"Portfolio of {n} compan{'y' if n == 1 else 'ies'} analysed."]

    if high_risk:
        names = ", ".join(c.name for c in high_risk[:5])
        parts.append(
            f"{len(high_risk)} company(ies) scored {_HIGH_RISK_THRESHOLD:.0f}+ on risk "
            f"and warrant closer review: {names}."
        )
    else:
        parts.append("No company scored in the high-risk range.")

    if violations:
        names = ", ".join(c.name for c in violations[:5])
        parts.append(f"{len(violations)} company(ies) have compliance violations: {names}.")

    if flag_counts:
        most_common = sorted(flag_counts.items(), key=lambda kv: -kv[1])[0]
        if most_common[1] > 1:
            parts.append(
                f"The most common risk pattern across the cohort is \"{most_common[0]}\" "
                f"({most_common[1]} companies)."
            )

    runways = [c.runway_months for c in companies if c.runway_months is not None]
    if runways:
        low_runway = [c for c in companies if c.runway_months is not None and c.runway_months < 6]
        if low_runway:
            names = ", ".join(c.name for c in low_runway[:5])
            parts.append(f"{len(low_runway)} company(ies) have under 6 months of runway: {names}.")

    return " ".join(parts)
