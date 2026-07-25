"""Tests for analysis/portfolio_narrative.py — cross-company Network Lens synthesis."""
from __future__ import annotations

import pytest

from analysis.portfolio_narrative import (
    PortfolioCompany,
    _fallback_narrative,
    generate_portfolio_narrative,
)


def _company(name: str, risk_score: float = 10.0, flags: list[str] | None = None,
             compliance_status: str = "clean", rules: list[str] | None = None,
             runway_months: float | None = 12.0, churn_rate: float | None = 0.05) -> PortfolioCompany:
    return PortfolioCompany(
        name=name, risk_score=risk_score, red_flag_descriptions=flags or [],
        compliance_status=compliance_status, compliance_rule_names=rules or [],
        runway_months=runway_months, churn_rate=churn_rate,
    )


class TestGeneratePortfolioNarrative:
    def test_empty_cohort(self) -> None:
        assert generate_portfolio_narrative([]) == "No companies in this cohort."

    def test_falls_back_without_api_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        result = generate_portfolio_narrative([_company("Acme")], llm_enabled=True)
        assert "Acme" not in result  # fallback doesn't reference clean companies by name
        assert "Portfolio of 1 company" in result

    def test_llm_disabled_explicitly_uses_fallback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-key-for-test")
        result = generate_portfolio_narrative([_company("Acme")], llm_enabled=False)
        assert "Portfolio of 1 company" in result


class TestFallbackNarrative:
    def test_singular_vs_plural_company_count(self) -> None:
        assert "1 company " in _fallback_narrative([_company("A")])
        assert "2 companies " in _fallback_narrative([_company("A"), _company("B")])

    def test_high_risk_companies_named(self) -> None:
        companies = [_company("SafeCo", risk_score=10), _company("RiskyCo", risk_score=75)]
        narrative = _fallback_narrative(companies)
        assert "RiskyCo" in narrative
        assert "1 company(ies) scored" in narrative

    def test_no_high_risk_companies(self) -> None:
        narrative = _fallback_narrative([_company("SafeCo", risk_score=10)])
        assert "No company scored in the high-risk range" in narrative

    def test_compliance_violations_named(self) -> None:
        companies = [
            _company("CleanCo", compliance_status="clean"),
            _company("BadCo", compliance_status="violations"),
        ]
        narrative = _fallback_narrative(companies)
        assert "BadCo" in narrative
        assert "compliance violations" in narrative

    def test_most_common_flag_pattern_surfaced(self) -> None:
        companies = [
            _company("A", flags=["structuring detected"]),
            _company("B", flags=["structuring detected"]),
            _company("C", flags=["round tripping"]),
        ]
        narrative = _fallback_narrative(companies)
        assert "structuring detected" in narrative
        assert "2 companies" in narrative

    def test_low_runway_companies_named(self) -> None:
        companies = [
            _company("HealthyCo", runway_months=18),
            _company("BurningCo", runway_months=3),
        ]
        narrative = _fallback_narrative(companies)
        assert "BurningCo" in narrative
        assert "under 6 months" in narrative

    def test_no_runway_data_does_not_crash(self) -> None:
        narrative = _fallback_narrative([_company("A", runway_months=None)])
        assert isinstance(narrative, str)
