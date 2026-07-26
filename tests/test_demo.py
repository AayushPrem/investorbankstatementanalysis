"""Tests for the demo pipeline runner (Step 1.12).

Tests cover demo/pipeline_runner.py which is the Streamlit-free core of the
demo.  We deliberately do NOT import demo.app — that module calls
st.set_page_config() at import time and requires a running Streamlit server.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from demo.pipeline_runner import DemoResult, run_pipeline_on_bytes


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("demo_test")


def _gold_pdf_bytes(stem: str) -> bytes:
    """Read a gold-set PDF by stem (e.g. 'hdfc_healthy_saas_5d2a8b')."""
    path = Path("data/gold") / f"{stem}.pdf"
    return path.read_bytes()


# Use two gold PDFs so we cover both banks
_HDFC_STEM  = "hdfc_healthy_saas_5d2a8b"
_ICICI_STEM = "icici_burning_startup_4eb7a0"


@pytest.fixture(scope="module")
def hdfc_result() -> DemoResult:
    return run_pipeline_on_bytes(
        _gold_pdf_bytes(_HDFC_STEM), f"{_HDFC_STEM}.pdf"
    )


@pytest.fixture(scope="module")
def icici_result() -> DemoResult:
    return run_pipeline_on_bytes(
        _gold_pdf_bytes(_ICICI_STEM), f"{_ICICI_STEM}.pdf"
    )


# ─── DemoResult structure ─────────────────────────────────────────────────────

class TestDemoResultStructure:
    def test_returns_demo_result(self, hdfc_result: DemoResult) -> None:
        assert isinstance(hdfc_result, DemoResult)

    def test_has_doc(self, hdfc_result: DemoResult) -> None:
        from schema.canonical import StatementDocument
        assert isinstance(hdfc_result.doc, StatementDocument)

    def test_has_metrics(self, hdfc_result: DemoResult) -> None:
        from analysis.financial_analyst import FinancialMetrics
        assert isinstance(hdfc_result.metrics, FinancialMetrics)

    def test_has_report_bytes(self, hdfc_result: DemoResult) -> None:
        assert isinstance(hdfc_result.report_bytes, bytes)
        assert len(hdfc_result.report_bytes) > 500

    def test_report_is_valid_pdf(self, hdfc_result: DemoResult) -> None:
        assert hdfc_result.report_bytes[:5] == b"%PDF-"

    def test_has_narrative(self, hdfc_result: DemoResult) -> None:
        """Wave 4.5 — no verdict_label anymore; every result gets a summary
        narrative instead (no INVESTABLE/MONITOR/CAUTION rendered anywhere)."""
        assert isinstance(hdfc_result.narrative, str)
        assert len(hdfc_result.narrative) > 20

    def test_icici_also_has_narrative(self, icici_result: DemoResult) -> None:
        assert isinstance(icici_result.narrative, str)
        assert len(icici_result.narrative) > 20


# ─── Pipeline correctness via DemoResult ─────────────────────────────────────

class TestPipelineCorrectness:
    def test_transactions_extracted(self, hdfc_result: DemoResult) -> None:
        assert len(hdfc_result.doc.transactions) > 0

    def test_all_transactions_categorised(self, hdfc_result: DemoResult) -> None:
        uncategorised = [
            t for t in hdfc_result.doc.transactions if t.category is None
        ]
        assert uncategorised == []

    def test_positive_revenue(self, hdfc_result: DemoResult) -> None:
        assert hdfc_result.metrics.total_revenue > Decimal("0")

    def test_positive_burn(self, hdfc_result: DemoResult) -> None:
        assert hdfc_result.metrics.total_burn > Decimal("0")

    def test_period_months_set(self, hdfc_result: DemoResult) -> None:
        assert hdfc_result.metrics.period_months >= 2

    def test_monthly_stats_present(self, hdfc_result: DemoResult) -> None:
        assert len(hdfc_result.metrics.monthly_stats) >= 2

    def test_icici_transactions_extracted(self, icici_result: DemoResult) -> None:
        assert len(icici_result.doc.transactions) > 0

    def test_icici_all_categorised(self, icici_result: DemoResult) -> None:
        uncategorised = [
            t for t in icici_result.doc.transactions if t.category is None
        ]
        assert uncategorised == []

    def test_healthy_saas_has_positive_revenue_trend_in_narrative(self, hdfc_result: DemoResult) -> None:
        # healthy_saas grows steadily — narrative should mention growth, not decline
        assert "declining" not in hdfc_result.narrative.lower()

    def test_burning_startup_flagged_in_health_or_risk(self, icici_result: DemoResult) -> None:
        # burning_startup is cash-flow negative with short runway — this must
        # surface as either a risk flag or a financial health alert somewhere,
        # not necessarily via any single specific one.
        assert icici_result.risk_report.flags or icici_result.health_report.alerts


# ─── Different file hash → re-run isolation ───────────────────────────────────

class TestRunIsolation:
    def test_different_pdfs_give_different_results(self) -> None:
        hdfc  = run_pipeline_on_bytes(_gold_pdf_bytes(_HDFC_STEM),  f"{_HDFC_STEM}.pdf")
        icici = run_pipeline_on_bytes(_gold_pdf_bytes(_ICICI_STEM), f"{_ICICI_STEM}.pdf")
        # Different banks → different account IDs
        assert hdfc.doc.account_id != icici.doc.account_id

    def test_same_bytes_returns_consistent_result(self) -> None:
        """Running on the same PDF twice gives the same summary narrative."""
        b = _gold_pdf_bytes(_HDFC_STEM)
        r1 = run_pipeline_on_bytes(b, "a.pdf")
        r2 = run_pipeline_on_bytes(b, "b.pdf")
        assert r1.narrative == r2.narrative
        assert r1.metrics.total_revenue == r2.metrics.total_revenue

    def test_temp_files_cleaned_up(self) -> None:
        """No temp files should leak after run_pipeline_on_bytes."""
        import tempfile
        before = set(Path(tempfile.gettempdir()).glob("tmp*"))
        run_pipeline_on_bytes(_gold_pdf_bytes(_HDFC_STEM), "test.pdf")
        after = set(Path(tempfile.gettempdir()).glob("tmp*"))
        # Any new temp dirs should be gone (TemporaryDirectory cleans up on exit)
        new_dirs = after - before
        assert not any(d.exists() for d in new_dirs)


# ─── Error handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_non_pdf_raises(self) -> None:
        with pytest.raises(Exception):
            run_pipeline_on_bytes(b"not a pdf at all", "fake.pdf")

    def test_empty_bytes_raises(self) -> None:
        with pytest.raises(Exception):
            run_pipeline_on_bytes(b"", "empty.pdf")

    def test_unsupported_bank_raises_and_produces_no_report(self) -> None:
        """Wave 4.2 — uploading a non-HDFC/ICICI statement must halt with a
        clear error, never fall through to an empty/misleading report."""
        from io import BytesIO

        from reportlab.lib.pagesizes import A4
        from reportlab.pdfgen import canvas

        buf = BytesIO()
        c = canvas.Canvas(buf, pagesize=A4)
        c.drawString(50, 800, "State Bank of India")
        c.drawString(50, 780, "Account Statement")
        c.save()

        with pytest.raises(Exception, match="HDFC and ICICI"):
            run_pipeline_on_bytes(buf.getvalue(), "sbi_statement.pdf")


# ─── All gold PDFs smoke test ─────────────────────────────────────────────────

class TestAllGoldPDFs:
    @pytest.mark.parametrize("pdf_path", sorted(Path("data/gold").glob("*.pdf")))
    def test_pipeline_runs_on_gold_pdf(self, pdf_path: Path) -> None:
        """Every gold PDF should produce a valid DemoResult without crashing."""
        result = run_pipeline_on_bytes(pdf_path.read_bytes(), pdf_path.name)
        assert isinstance(result.narrative, str) and len(result.narrative) > 0
        assert result.report_bytes[:5] == b"%PDF-"
        assert len(result.doc.transactions) > 0
