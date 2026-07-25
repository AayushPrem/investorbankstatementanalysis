"""Tests for the Angel Lens report generator (Step 1.11).

Coverage:
  - Verdict logic (_verdict) for all three outcomes
  - Formatting helpers (_fmt_amount, _fmt_growth, _fmt_runway)
  - Health signals (_signals) content and length
  - generate() produces a valid 1-page PDF
  - Integration: full pipeline → report for HDFC and ICICI statements
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pdfplumber
import pytest

from analysis.financial_analyst import FinancialMetrics, MonthlyStats
from reports.angel_lens import (
    AngelLensReport,
    _fmt_amount,
    _fmt_growth,
    _fmt_runway,
    _signals,
    _verdict,
)
from schema.canonical import StatementDocument, ValidationStatus

# ─── helpers ─────────────────────────────────────────────────────────────────

def _metrics(
    total_revenue: str = "1000000",
    total_burn: str = "600000",
    closing_balance: str = "500000",
    avg_monthly_revenue: str = "333333",
    avg_monthly_burn: str = "200000",
    runway_months: str | None = "2.5",
    avg_mom_growth: str | None = "0.10",
    period_months: int = 3,
    revenue_hhi: str | None = None,
    top_customer_revenue_pct: str | None = None,
    monthly_stats: list[MonthlyStats] | None = None,
) -> FinancialMetrics:
    return FinancialMetrics(
        period_months=period_months,
        total_revenue=Decimal(total_revenue),
        total_burn=Decimal(total_burn),
        closing_balance=Decimal(closing_balance),
        monthly_stats=monthly_stats or [
            MonthlyStats("2024-04", Decimal("300000"), Decimal("180000")),
            MonthlyStats("2024-05", Decimal("350000"), Decimal("200000")),
            MonthlyStats("2024-06", Decimal("350001"), Decimal("220000")),
        ],
        avg_monthly_revenue=Decimal(avg_monthly_revenue),
        avg_monthly_burn=Decimal(avg_monthly_burn),
        runway_months=Decimal(runway_months) if runway_months is not None else None,
        mom_revenue_growth_rates=[Decimal("0.1667"), Decimal("0.0000")] if avg_mom_growth else [],
        avg_mom_growth=Decimal(avg_mom_growth) if avg_mom_growth is not None else None,
        revenue_hhi=Decimal(revenue_hhi) if revenue_hhi is not None else None,
        top_customer_revenue_pct=Decimal(top_customer_revenue_pct) if top_customer_revenue_pct else None,
    )


def _doc(account_id: str = "TEST001") -> StatementDocument:
    return StatementDocument(
        account_id=account_id,
        statement_period_start=date(2024, 4, 1),
        statement_period_end=date(2024, 6, 30),
        source_format="digital_pdf_hdfc",
        bank_name="HDFC",
        validation_status=ValidationStatus.PASSED,
    )


@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("angel_lens")


# ─── _verdict ─────────────────────────────────────────────────────────────────

class TestVerdict:
    def test_investable_when_profitable_and_growing(self) -> None:
        m = _metrics(avg_monthly_revenue="400000", avg_monthly_burn="300000",
                     avg_mom_growth="0.10", runway_months="2.0")
        label, *_ = _verdict(m)
        assert label == "INVESTABLE"

    def test_investable_when_6_plus_months_runway_and_growing(self) -> None:
        m = _metrics(avg_monthly_revenue="100000", avg_monthly_burn="500000",
                     avg_mom_growth="0.05", runway_months="8.0")
        label, *_ = _verdict(m)
        assert label == "INVESTABLE"

    def test_monitor_when_profitable_but_declining(self) -> None:
        m = _metrics(avg_monthly_revenue="400000", avg_monthly_burn="300000",
                     avg_mom_growth="-0.05", runway_months="4.0")
        label, *_ = _verdict(m)
        assert label == "MONITOR"

    def test_monitor_when_3_to_6_months_runway(self) -> None:
        m = _metrics(avg_monthly_revenue="100000", avg_monthly_burn="500000",
                     avg_mom_growth="-0.02", runway_months="4.0")
        label, *_ = _verdict(m)
        assert label == "MONITOR"

    def test_caution_when_very_short_runway(self) -> None:
        m = _metrics(avg_monthly_revenue="50000", avg_monthly_burn="500000",
                     avg_mom_growth="-0.10", runway_months="1.5")
        label, *_ = _verdict(m)
        assert label == "CAUTION"

    def test_verdict_returns_three_values(self) -> None:
        m = _metrics()
        result = _verdict(m)
        assert len(result) == 3

    def test_verdict_label_is_string(self) -> None:
        m = _metrics()
        label, fg, bg = _verdict(m)
        assert isinstance(label, str)
        assert label in {"INVESTABLE", "MONITOR", "CAUTION"}

    def test_no_runway_and_profitable(self) -> None:
        """Profitable business with no burn has no runway risk — INVESTABLE."""
        m = _metrics(
            avg_monthly_revenue="500000", avg_monthly_burn="300000",
            runway_months=None, avg_mom_growth="0.05",
        )
        label, *_ = _verdict(m)
        assert label == "INVESTABLE"


# ─── formatting helpers ───────────────────────────────────────────────────────

class TestFmtAmount:
    def test_crore(self) -> None:
        assert "Cr" in _fmt_amount(Decimal("15000000"))

    def test_lakh(self) -> None:
        assert "L" in _fmt_amount(Decimal("500000"))

    def test_thousands(self) -> None:
        assert "K" in _fmt_amount(Decimal("5000"))

    def test_small(self) -> None:
        result = _fmt_amount(Decimal("500"))
        assert "Rs." in result
        assert "K" not in result
        assert "L" not in result

    def test_negative(self) -> None:
        result = _fmt_amount(Decimal("-100000"))
        assert result.startswith("-Rs.")

    def test_one_crore(self) -> None:
        result = _fmt_amount(Decimal("10000000"))
        assert "1.0Cr" in result

    def test_one_lakh(self) -> None:
        result = _fmt_amount(Decimal("100000"))
        assert "1.0L" in result


class TestFmtGrowth:
    def test_positive(self) -> None:
        assert _fmt_growth(Decimal("0.12")) == "+12.0%"

    def test_negative(self) -> None:
        result = _fmt_growth(Decimal("-0.05"))
        assert "-5.0%" in result

    def test_zero(self) -> None:
        assert _fmt_growth(Decimal("0")) == "+0.0%"

    def test_none(self) -> None:
        assert _fmt_growth(None) == "N/A"


class TestFmtRunway:
    def test_months_shown(self) -> None:
        assert "mo" in _fmt_runway(Decimal("3.5"))

    def test_profitable(self) -> None:
        assert _fmt_runway(None) == "Profitable"

    def test_two_decimal_places(self) -> None:
        result = _fmt_runway(Decimal("2.567"))
        assert "2.6" in result


# ─── _signals ─────────────────────────────────────────────────────────────────

class TestSignals:
    def test_returns_up_to_eight(self) -> None:
        m = _metrics()
        assert len(_signals(m)) <= 8

    def test_at_least_one_signal(self) -> None:
        m = _metrics()
        assert len(_signals(m)) >= 1

    def test_signals_are_tuples(self) -> None:
        m = _metrics()
        for item in _signals(m):
            assert isinstance(item, tuple)
            assert len(item) == 2

    def test_growing_revenue_signal(self) -> None:
        m = _metrics(avg_mom_growth="0.15")
        texts = [text for _, text in _signals(m)]
        assert any("growing" in t.lower() or "stable" in t.lower() for t in texts)

    def test_declining_revenue_signal(self) -> None:
        m = _metrics(avg_mom_growth="-0.20")
        texts = [text for _, text in _signals(m)]
        assert any("declining" in t.lower() for t in texts)

    def test_profitable_signal(self) -> None:
        m = _metrics(avg_monthly_revenue="400000", avg_monthly_burn="200000")
        texts = [text for _, text in _signals(m)]
        assert any("positive" in t.lower() or "sustain" in t.lower() for t in texts)

    def test_short_runway_signal(self) -> None:
        m = _metrics(avg_monthly_revenue="100000", avg_monthly_burn="500000",
                     runway_months="1.5")
        texts = [text for _, text in _signals(m)]
        assert any("runway" in t.lower() for t in texts)

    def test_concentration_signal_when_available(self) -> None:
        m = _metrics(top_customer_revenue_pct="0.70", revenue_hhi="0.50")
        texts = [text for _, text in _signals(m)]
        assert any("customer" in t.lower() or "concentration" in t.lower() for t in texts)

    def test_no_concentration_no_signal(self) -> None:
        m = _metrics(top_customer_revenue_pct=None, revenue_hhi=None)
        # No concentration data → no concentration signal (that's correct behaviour)
        texts = [text for _, text in _signals(m)]
        # Should still have at least the revenue and profitability signals
        assert len(texts) >= 2


# ─── generate() ───────────────────────────────────────────────────────────────

class TestGenerate:
    def test_returns_path(self, tmp_root: Path) -> None:
        out = tmp_root / "basic.pdf"
        result = AngelLensReport().generate(_doc(), _metrics(), out)
        assert isinstance(result, Path)

    def test_file_created(self, tmp_root: Path) -> None:
        out = tmp_root / "created.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out)
        assert out.exists()

    def test_file_not_empty(self, tmp_root: Path) -> None:
        out = tmp_root / "nonempty.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out)
        assert out.stat().st_size > 500

    def test_valid_pdf_header(self, tmp_root: Path) -> None:
        out = tmp_root / "valid.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out)
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_exactly_one_page(self, tmp_root: Path) -> None:
        out = tmp_root / "onepage.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out)
        with pdfplumber.open(out) as pdf:
            assert len(pdf.pages) == 1

    def test_returns_output_path(self, tmp_root: Path) -> None:
        out = tmp_root / "retval.pdf"
        returned = AngelLensReport().generate(_doc(), _metrics(), out)
        assert returned == out

    def test_creates_parent_dirs(self, tmp_root: Path) -> None:
        out = tmp_root / "nested" / "deep" / "report.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out)
        assert out.exists()

    def test_custom_company_name(self, tmp_root: Path) -> None:
        """Smoke-test: custom company_name doesn't break generation."""
        out = tmp_root / "named.pdf"
        AngelLensReport().generate(_doc(), _metrics(), out, company_name="Acme Pvt Ltd")
        assert out.exists()

    def test_investable_verdict_generates(self, tmp_root: Path) -> None:
        m = _metrics(avg_monthly_revenue="500000", avg_monthly_burn="200000",
                     avg_mom_growth="0.15", runway_months=None)
        out = tmp_root / "investable.pdf"
        AngelLensReport().generate(_doc(), m, out)
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_caution_verdict_generates(self, tmp_root: Path) -> None:
        m = _metrics(avg_monthly_revenue="50000", avg_monthly_burn="800000",
                     avg_mom_growth="-0.20", runway_months="1.0")
        out = tmp_root / "caution.pdf"
        AngelLensReport().generate(_doc(), m, out)
        assert out.read_bytes()[:5] == b"%PDF-"

    def test_empty_metrics_generates(self, tmp_root: Path) -> None:
        """Edge case: no monthly data, no growth — must not crash."""
        m = _metrics(
            total_revenue="0", total_burn="0", closing_balance="1000000",
            avg_monthly_revenue="0", avg_monthly_burn="0",
            runway_months=None, avg_mom_growth=None,
            period_months=0, monthly_stats=[],
        )
        out = tmp_root / "empty_metrics.pdf"
        AngelLensReport().generate(_doc(), m, out)
        assert out.exists()


# ─── Integration: full pipeline → report ─────────────────────────────────────

class TestIntegration:
    def _full_run(
        self, bank: str, profile: str, tmp_root: Path, seed: int = 42
    ) -> Path:
        from adapters import DigitalPDFAdapter
        from analysis.categoriser import Categoriser
        from analysis.financial_analyst import FinancialAnalyst
        from pipeline.normaliser import Normaliser
        from tools.synthetic_gen import generate_statement

        pdf, _ = generate_statement(
            bank=bank, profile=profile, output_dir=tmp_root,
            statement_id=f"angel_{bank}_{profile}", flags=[], seed=seed,
        )
        raw = DigitalPDFAdapter().extract(pdf)
        doc = Normaliser().normalise(raw)
        doc, _ = Categoriser(llm_enabled=False).categorise(doc)
        metrics = FinancialAnalyst().analyse(doc)
        out = tmp_root / f"angel_{bank}_{profile}.pdf"
        return AngelLensReport().generate(doc, metrics, out)

    def test_hdfc_healthy_saas_report(self, tmp_root: Path) -> None:
        out = self._full_run("hdfc", "healthy_saas", tmp_root)
        assert out.exists() and out.stat().st_size > 500

    def test_icici_burning_startup_report(self, tmp_root: Path) -> None:
        out = self._full_run("icici", "burning_startup", tmp_root)
        assert out.stat().st_size > 500

    def test_hdfc_services_firm_one_page(self, tmp_root: Path) -> None:
        out = self._full_run("hdfc", "services_firm", tmp_root)
        with pdfplumber.open(out) as pdf:
            assert len(pdf.pages) == 1

    def test_icici_ecommerce_one_page(self, tmp_root: Path) -> None:
        out = self._full_run("icici", "ecommerce", tmp_root)
        with pdfplumber.open(out) as pdf:
            assert len(pdf.pages) == 1

    def test_all_profiles_generate_valid_pdf(self, tmp_root: Path) -> None:
        combos = [
            ("hdfc", "healthy_saas"),
            ("hdfc", "restaurant"),
            ("icici", "services_firm"),
            ("icici", "burning_startup"),
        ]
        for bank, profile in combos:
            out = self._full_run(bank, profile, tmp_root, seed=99)
            assert out.read_bytes()[:5] == b"%PDF-", f"Invalid PDF for {bank}/{profile}"
