"""pipeline/run.py: the canonical entry point and adapter auto-selection."""
from __future__ import annotations

import warnings
from io import BytesIO
from pathlib import Path

import pytest

from pipeline import run as run_mod
from pipeline.run import (
    ADAPTER_CSV,
    ADAPTER_DIGITAL_PDF,
    ADAPTER_EXCEL,
    ADAPTER_IMAGE,
    ADAPTER_SCANNED_PDF,
    AnalysisOptions,
    AnalysisResult,
    make_adapter,
    run_analysis,
    select_adapter_name,
)

_GOLD = Path("data/gold")
_HDFC_STEM = "hdfc_healthy_saas_5d2a8b"


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


def _blank_pdf(tmp_path: Path) -> Path:
    from reportlab.pdfgen import canvas

    p = tmp_path / "scan.pdf"
    c = canvas.Canvas(str(p))
    c.rect(50, 50, 100, 100)  # graphics only, no text layer → looks scanned
    c.save()
    return p


def _text_pdf(tmp_path: Path) -> Path:
    from reportlab.pdfgen import canvas

    p = tmp_path / "text.pdf"
    c = canvas.Canvas(str(p))
    c.drawString(50, 800, "HDFC BANK statement")
    c.save()
    return p


# ── Adapter selection ────────────────────────────────────────────────────────

class TestSelectAdapter:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("s.csv", ADAPTER_CSV),
            ("s.CSV", ADAPTER_CSV),
            ("s.xlsx", ADAPTER_EXCEL),
            ("s.xls", ADAPTER_EXCEL),
            ("s.png", ADAPTER_IMAGE),
            ("s.jpeg", ADAPTER_IMAGE),
            ("s.tiff", ADAPTER_IMAGE),
        ],
    )
    def test_by_suffix(self, tmp_path: Path, name: str, expected: str) -> None:
        p = tmp_path / name
        p.write_bytes(b"x")
        assert select_adapter_name(p) == expected

    def test_text_pdf_is_digital(self, tmp_path: Path) -> None:
        assert select_adapter_name(_text_pdf(tmp_path)) == ADAPTER_DIGITAL_PDF

    def test_textless_pdf_is_scanned(self, tmp_path: Path) -> None:
        assert select_adapter_name(_blank_pdf(tmp_path)) == ADAPTER_SCANNED_PDF

    def test_gold_pdf_is_digital(self) -> None:
        assert select_adapter_name(_GOLD / f"{_HDFC_STEM}.pdf") == ADAPTER_DIGITAL_PDF

    def test_unreadable_pdf_falls_to_digital(self, tmp_path: Path) -> None:
        # The digital adapter then raises its usual AdapterError (unchanged behaviour).
        p = tmp_path / "bad.pdf"
        p.write_bytes(b"not a pdf at all")
        assert select_adapter_name(p) == ADAPTER_DIGITAL_PDF

    def test_make_adapter_types(self) -> None:
        from adapters.csv import CSVAdapter
        from adapters.digital_pdf import DigitalPDFAdapter
        from adapters.excel import ExcelAdapter

        assert isinstance(make_adapter(ADAPTER_DIGITAL_PDF), DigitalPDFAdapter)
        assert isinstance(make_adapter(ADAPTER_CSV), CSVAdapter)
        assert isinstance(make_adapter(ADAPTER_EXCEL), ExcelAdapter)
        with pytest.raises(ValueError, match="unknown adapter"):
            make_adapter("fax")


# ── run_analysis ─────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def gold_bytes() -> bytes:
    return (_GOLD / f"{_HDFC_STEM}.pdf").read_bytes()


@pytest.fixture(scope="module")
def analysis(gold_bytes: bytes) -> AnalysisResult:
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("ANTHROPIC_API_KEY", raising=False)
        return run_analysis(gold_bytes, f"{_HDFC_STEM}.pdf")


class TestRunAnalysis:
    def test_result_shape(self, analysis: AnalysisResult) -> None:
        assert analysis.adapter_name == ADAPTER_DIGITAL_PDF
        assert analysis.doc.transactions
        assert analysis.narrative
        assert analysis.run_id
        # Phase 5: "ml_anomaly" runs after "risk" only when an anomaly model is
        # configured (tests/test_ml_anomaly.py covers both paths).
        ml = ["ml_anomaly"] if analysis.anomaly_report is not None else []
        assert analysis.stage_names == [
            "ingest", "normalise", "validate", "categorise", "financial_analysis",
            "related_party", "customer_identity", "risk", *ml, "customer_analytics",
            "financial_health", "compliance", "reconciliation", "narrative",
        ]

    def test_matches_demo_runner(self, analysis: AnalysisResult, gold_bytes: bytes) -> None:
        """run_pipeline_on_bytes is run_analysis + rendering: the analysis fields must agree."""
        from demo.pipeline_runner import run_pipeline_on_bytes

        demo = run_pipeline_on_bytes(gold_bytes, f"{_HDFC_STEM}.pdf")
        a, d = analysis, demo
        assert [t.transaction_id for t in a.doc.transactions] == [
            t.transaction_id for t in d.doc.transactions
        ]
        assert [t.category for t in a.doc.transactions] == [t.category for t in d.doc.transactions]
        assert [t.customer_id for t in a.doc.transactions] == [
            t.customer_id for t in d.doc.transactions
        ]
        assert a.metrics == d.metrics
        assert [(f.detector_name, f.severity, f.description) for f in a.risk_report.flags] == [
            (f.detector_name, f.severity, f.description) for f in d.risk_report.flags
        ]
        assert a.risk_report.composite_score == d.risk_report.composite_score
        assert [x.description for x in a.health_report.alerts] == [
            x.description for x in d.health_report.alerts
        ]
        assert [e.rule_id for e in a.compliance_report.exceptions] == [
            e.rule_id for e in d.compliance_report.exceptions
        ]
        assert a.narrative == d.narrative

    def test_forced_adapter_option(self, gold_bytes: bytes) -> None:
        r = run_analysis(
            gold_bytes, f"{_HDFC_STEM}.pdf",
            options=AnalysisOptions(adapter=ADAPTER_DIGITAL_PDF, company_name="Acme"),
        )
        assert r.company_name == "Acme"

    def test_filename_cannot_escape_tempdir(self, gold_bytes: bytes, tmp_path: Path) -> None:
        r = run_analysis(gold_bytes, f"../../{_HDFC_STEM}.pdf")
        assert r.doc.transactions
        plain = run_analysis(gold_bytes, f"{_HDFC_STEM}.pdf")
        assert [t.transaction_id for t in r.doc.transactions] == [
            t.transaction_id for t in plain.doc.transactions
        ]

    def test_bad_bytes_raise(self) -> None:
        from adapters.base import AdapterError

        with pytest.raises(AdapterError):
            run_analysis(b"not a pdf at all", "fake.pdf")

    def test_unsupported_bank_message_unchanged(self) -> None:
        from reportlab.pdfgen import canvas

        buf = BytesIO()
        c = canvas.Canvas(buf)
        c.drawString(50, 800, "State Bank of India")
        c.save()
        with pytest.raises(Exception, match="HDFC and ICICI"):
            run_analysis(buf.getvalue(), "sbi.pdf")

    def test_no_pii_in_pipeline_logs(
        self, gold_bytes: bytes, caplog: pytest.LogCaptureFixture
    ) -> None:
        import logging

        with caplog.at_level(logging.DEBUG):
            r = run_analysis(gold_bytes, f"{_HDFC_STEM}.pdf")
        # Our own loggers only. Third-party loggers (pdfminer dumps the PDF token
        # stream at DEBUG) are pinned to WARNING by configure_logging; that is
        # tested in tests/test_logging_config.py.
        ours = ("pipeline", "analysis", "adapters", "reports", "jurisdictions", "llm", "config", "bsaa")
        text = "\n".join(
            f"{rec.getMessage()} {rec.__dict__}"
            for rec in caplog.records if rec.name.split(".")[0] in ours
        )
        assert "stage done" in text
        # No transaction description and no filename should appear in our log records.
        for t in r.doc.transactions[:50]:
            if len(t.description) > 8:
                assert t.description not in text
        assert _HDFC_STEM not in text


def test_orchestrator_is_deprecated() -> None:
    from pipeline.orchestrator import Orchestrator

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        Orchestrator(llm_enabled=False)
    assert any(issubclass(w.category, DeprecationWarning) for w in caught)


def test_batch_analysis_only_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.batch import BatchInput, BatchOrchestrator

    result = BatchOrchestrator(concurrency=1, render_reports=False).run_batch(
        [BatchInput(path=str(_GOLD / f"{_HDFC_STEM}.pdf"), company_name="Acme")]
    )
    (outcome,) = result.results.values()
    assert isinstance(outcome, AnalysisResult)
    assert outcome.company_name == "Acme"


def test_module_exports() -> None:
    assert run_mod.ADAPTERS == (
        ADAPTER_DIGITAL_PDF, ADAPTER_SCANNED_PDF, ADAPTER_CSV, ADAPTER_EXCEL, ADAPTER_IMAGE,
    )
