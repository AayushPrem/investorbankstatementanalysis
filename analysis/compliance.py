"""Compliance analyst — runs jurisdiction rules against a StatementDocument.

Usage:
    from analysis.compliance import ComplianceAnalyst
    from jurisdictions.india.rules import INDIA_RULES

    report = ComplianceAnalyst().analyse(doc, INDIA_RULES)
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from analysis.risk import Severity
from jurisdictions import ComplianceException

if TYPE_CHECKING:
    from schema.canonical import StatementDocument

log = logging.getLogger(__name__)


@dataclass
class ComplianceReport:
    exceptions: list[ComplianceException]
    jurisdiction: str
    by_severity: dict[str, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.by_severity = {
            "HIGH": sum(1 for e in self.exceptions if e.severity == Severity.HIGH),
            "MEDIUM": sum(1 for e in self.exceptions if e.severity == Severity.MEDIUM),
            "LOW": sum(1 for e in self.exceptions if e.severity == Severity.LOW),
        }

    @property
    def has_exceptions(self) -> bool:
        return bool(self.exceptions)

    @property
    def high_count(self) -> int:
        return self.by_severity.get("HIGH", 0)


class ComplianceAnalyst:
    """Runs a jurisdiction's compliance rules and optionally polishes descriptions via LLM."""

    def __init__(self, llm_enabled: bool = True) -> None:
        self._llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))

    def analyse(
        self,
        document: "StatementDocument",
        rules: list | object,
        jurisdiction: str | None = None,
    ) -> ComplianceReport:
        # Accept either a raw rule list or a JurisdictionModule instance.
        if hasattr(rules, "rules"):
            if jurisdiction is None:
                jurisdiction = getattr(rules, "name", "unknown")
            rules = rules.rules
        if jurisdiction is None:
            jurisdiction = "India"

        all_exceptions: list[ComplianceException] = []
        for rule in rules:
            try:
                exceptions = rule.evaluate(document)
                all_exceptions.extend(exceptions)
            except Exception as exc:
                log.warning("Rule %s failed: %s", getattr(rule, "rule_id", "?"), exc)

        # Sort: HIGH first
        all_exceptions.sort(key=lambda e: {"HIGH": 0, "MEDIUM": 1, "LOW": 2}.get(str(e.severity), 9))

        if all_exceptions and self._llm_enabled:
            self._polish_investor_framing(all_exceptions)

        return ComplianceReport(exceptions=all_exceptions, jurisdiction=jurisdiction)

    def _polish_investor_framing(self, exceptions: list[ComplianceException]) -> None:
        try:
            import anthropic
        except ImportError:
            return

        items = [
            {
                "index": i,
                "rule": e.rule_name,
                "citation": e.regulatory_citation,
                "severity": str(e.severity),
                "technical": e.description,
            }
            for i, e in enumerate(exceptions)
        ]
        prompt = (
            "Below are compliance exceptions found in an Indian company's bank statement. "
            "For each item, write a 2-sentence investor-risk framing: explain what the regulatory "
            "breach means for an investor doing due diligence. Be specific about the legal consequence "
            "and how it affects the investment thesis. Return JSON array: [{index, framing}].\n\n"
            f"Exceptions:\n{json.dumps(items, indent=2)}"
        )
        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=1500,
                system="You are a legal and compliance expert. Respond only with valid JSON.",
                messages=[{"role": "user", "content": prompt}],
            )
            raw = json.loads(response.content[0].text.strip())
            framing_map = {item["index"]: item["framing"] for item in raw}
            for i, exc in enumerate(exceptions):
                if i in framing_map:
                    exc.investor_risk_framing = framing_map[i]
        except Exception as err:
            log.warning("LLM polish for compliance failed: %s", err)
