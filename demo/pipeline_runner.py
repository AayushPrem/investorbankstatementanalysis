"""Testable pipeline logic for the demo — no Streamlit dependency.

Imported by demo/app.py and by the test suite.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Ensure project root is on sys.path regardless of working directory
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from adapters.digital_pdf import DigitalPDFAdapter
from analysis.categoriser import Categoriser
from analysis.financial_analyst import FinancialAnalyst, FinancialMetrics
from pipeline.normaliser import Normaliser
from pipeline.validator import Validator
from reports.angel_lens import AngelLensReport, _verdict
from schema.canonical import StatementDocument


@dataclass
class DemoResult:
    doc: StatementDocument
    metrics: FinancialMetrics
    report_bytes: bytes
    verdict_label: str


def run_pipeline_on_bytes(pdf_bytes: bytes, filename: str) -> DemoResult:
    """Run the full pipeline on raw PDF bytes.

    Writes to a temp file (adapter requires a filesystem path), runs all five
    stages, generates the Angel Lens PDF, and returns a DemoResult.

    This function has no Streamlit dependency and is safe to call from tests.
    """
    file_hash = hashlib.sha1(pdf_bytes).hexdigest()[:8]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pdf_path = tmp_path / f"{file_hash}_{filename}"
        pdf_path.write_bytes(pdf_bytes)

        raw = DigitalPDFAdapter().extract(pdf_path)
        doc = Normaliser().normalise(raw)
        Validator().validate(doc)
        doc, _ = Categoriser(llm_enabled=True).categorise(doc)
        metrics = FinancialAnalyst().analyse(doc)

        report_path = tmp_path / "angel_report.pdf"
        AngelLensReport().generate(doc, metrics, report_path)
        report_bytes = report_path.read_bytes()

    verdict_label, *_ = _verdict(metrics)
    return DemoResult(
        doc=doc, metrics=metrics,
        report_bytes=report_bytes, verdict_label=verdict_label,
    )
