"""Jurisdiction module protocol — pluggable compliance rule sets."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from analysis.risk import Severity

if TYPE_CHECKING:
    from schema.canonical import StatementDocument


@dataclass
class ComplianceException:
    rule_id: str
    rule_name: str
    regulatory_citation: str
    severity: Severity
    description: str                       # technical description
    investor_risk_framing: str             # investor-facing reframing
    triggering_transaction_ids: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)


class ComplianceRule(Protocol):
    rule_id: str
    name: str
    regulatory_citation: str
    severity: Severity
    description: str

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        ...


class JurisdictionModule(Protocol):
    name: str
    rules: list[ComplianceRule]
