"""Gold-standard regression test suite.

Discovers all statements in data/gold/, runs the pipeline on each, compares
against the paired ground-truth JSON in data/gold/labels/, and asserts that
accuracy meets the current sprint's targets.

Sprint 1 targets:
  - Extraction    >= 88%   (% of true transactions the pipeline found)
  - Categorisation >= 82%  (% of extracted transactions with correct category)

This test intentionally FAILS until Sprint 1 pipeline components are wired in.
When it passes, Sprint 1 is complete.

To run:
    make regression
    pytest tests/regression/ -v
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Sprint targets
# ─────────────────────────────────────────────────────────────────────────────

CURRENT_SPRINT = 1

TARGETS: dict[int, dict[str, float]] = {
    1: {"extraction": 0.88, "categorisation": 0.82},
    2: {"extraction": 0.88, "categorisation": 0.88, "risk_recall": 0.85, "customer_id": 0.90},
    3: {"extraction": 0.88, "categorisation": 0.88, "risk_recall": 0.85, "customer_id": 0.90,
        "compliance_recall": 0.90, "reconciliation_recall": 0.85},
}

SPRINT_TARGETS = TARGETS[CURRENT_SPRINT]

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

GOLD_DIR = Path("data/gold")
LABELS_DIR = GOLD_DIR / "labels"
RESULTS_DIR = Path("results") / f"sprint{CURRENT_SPRINT}"

# ─────────────────────────────────────────────────────────────────────────────
# Pipeline interface types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ExtractedTransaction:
    transaction_id: str
    date: date | None
    description: str
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal | None
    category: str | None = None
    customer_id: str | None = None
    anomaly_flags: list[str] = field(default_factory=list)


@dataclass
class PipelineOutput:
    """What the pipeline returns for one statement. Stub returns empty."""
    transactions: list[ExtractedTransaction] = field(default_factory=list)
    detected_flag_types: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline stub — swapped for real Orchestrator in Sprint 1 closeout
# ─────────────────────────────────────────────────────────────────────────────

def _convert_orchestrator_result(result: Any) -> PipelineOutput:
    """Map OrchestratorResult → PipelineOutput for the regression harness."""
    txns = [
        ExtractedTransaction(
            transaction_id=t.transaction_id,
            date=t.date,
            description=t.description,
            debit=t.debit,
            credit=t.credit,
            balance=t.balance,
            category=t.category.value if t.category else None,
            customer_id=t.customer_id,
            anomaly_flags=list(t.anomaly_flags),
        )
        for t in result.doc.transactions
    ]
    return PipelineOutput(transactions=txns, detected_flag_types=[])


def run_pipeline(pdf_path: Path) -> PipelineOutput:
    """Run the full analysis pipeline on a PDF and return structured output."""
    from pipeline.orchestrator import Orchestrator
    result = Orchestrator(llm_enabled=False).run(str(pdf_path))
    return _convert_orchestrator_result(result)


# ─────────────────────────────────────────────────────────────────────────────
# Matching logic
# ─────────────────────────────────────────────────────────────────────────────

_AMOUNT_TOLERANCE = Decimal("1")  # ₹1 tolerance for rounding differences


def _parse_amount(s: str | None) -> Decimal | None:
    if not s:
        return None
    try:
        return Decimal(str(s))
    except Exception:
        return None


def _true_amount(true_txn: dict[str, Any]) -> Decimal | None:
    return _parse_amount(true_txn.get("credit")) or _parse_amount(true_txn.get("debit"))


def _extracted_amount(e: ExtractedTransaction) -> Decimal | None:
    return e.credit or e.debit


def _match_transaction(
    true_txn: dict[str, Any],
    extracted: list[ExtractedTransaction],
    used: set[str],
) -> ExtractedTransaction | None:
    """Find the best matching extracted transaction for a true transaction.

    Matches on date + amount within ₹1 tolerance.
    Each extracted transaction is matched at most once (tracked via 'used').
    """
    true_amt = _true_amount(true_txn)
    true_date = true_txn.get("date", "")

    for e in extracted:
        if e.transaction_id in used:
            continue
        e_date = str(e.date) if e.date else ""
        e_amt = _extracted_amount(e)

        date_ok = e_date == true_date
        amt_ok = (
            e_amt is not None
            and true_amt is not None
            and abs(e_amt - true_amt) <= _AMOUNT_TOLERANCE
        )
        if date_ok and amt_ok:
            used.add(e.transaction_id)
            return e
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Per-statement metrics
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class StatementResult:
    statement_id: str
    pdf_path: str
    true_count: int
    extracted_count: int
    matched_count: int
    correct_categories: int
    injected_flag_types: list[str]
    detected_flag_types: list[str]
    error: str | None = None

    @property
    def extraction_accuracy(self) -> float:
        return self.matched_count / self.true_count if self.true_count else 0.0

    @property
    def categorisation_accuracy(self) -> float:
        return self.correct_categories / self.matched_count if self.matched_count else 0.0

    @property
    def risk_recall(self) -> float:
        if not self.injected_flag_types:
            return 1.0  # no flags injected — full credit
        detected = set(self.detected_flag_types)
        expected = set(self.injected_flag_types)
        return len(detected & expected) / len(expected)

    def to_dict(self) -> dict[str, Any]:
        return {
            "statement_id": self.statement_id,
            "pdf_path": self.pdf_path,
            "true_count": self.true_count,
            "extracted_count": self.extracted_count,
            "matched_count": self.matched_count,
            "extraction_accuracy": round(self.extraction_accuracy, 4),
            "categorisation_accuracy": round(self.categorisation_accuracy, 4),
            "injected_flag_types": self.injected_flag_types,
            "detected_flag_types": self.detected_flag_types,
            "risk_recall": round(self.risk_recall, 4),
            "error": self.error,
        }


def _evaluate_statement(pdf_path: Path) -> StatementResult:
    """Run pipeline on one gold statement and compute its metrics."""
    sid = pdf_path.stem
    label_path = LABELS_DIR / f"{sid}.truth.json"

    if not label_path.exists():
        return StatementResult(
            statement_id=sid, pdf_path=str(pdf_path),
            true_count=0, extracted_count=0, matched_count=0,
            correct_categories=0, injected_flag_types=[], detected_flag_types=[],
            error=f"No label file found: {label_path}",
        )

    truth = json.loads(label_path.read_text(encoding="utf-8"))
    true_txns: list[dict[str, Any]] = truth.get("transactions", [])
    injected_flags = [f["flag_type"] for f in truth.get("injected_flags", [])]

    try:
        output = run_pipeline(pdf_path)
    except Exception as exc:
        return StatementResult(
            statement_id=sid, pdf_path=str(pdf_path),
            true_count=len(true_txns), extracted_count=0, matched_count=0,
            correct_categories=0, injected_flag_types=injected_flags,
            detected_flag_types=[], error=str(exc),
        )

    # Match true transactions to extracted ones
    used: set[str] = set()
    matched = 0
    correct_cats = 0
    for t in true_txns:
        hit = _match_transaction(t, output.transactions, used)
        if hit:
            matched += 1
            if hit.category == t.get("category"):
                correct_cats += 1

    return StatementResult(
        statement_id=sid,
        pdf_path=str(pdf_path),
        true_count=len(true_txns),
        extracted_count=len(output.transactions),
        matched_count=matched,
        correct_categories=correct_cats,
        injected_flag_types=injected_flags,
        detected_flag_types=output.detected_flag_types,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate report
# ─────────────────────────────────────────────────────────────────────────────

def _aggregate(results: list[StatementResult]) -> dict[str, Any]:
    total_true = sum(r.true_count for r in results)
    total_matched = sum(r.matched_count for r in results)
    total_correct_cats = sum(r.correct_categories for r in results)

    # Risk: only statements that have injected flags
    flagged = [r for r in results if r.injected_flag_types]
    risk_recall = (
        sum(r.risk_recall for r in flagged) / len(flagged) if flagged else 1.0
    )

    extraction_acc = total_matched / total_true if total_true else 0.0
    cat_acc = total_correct_cats / total_matched if total_matched else 0.0

    passed = (
        extraction_acc >= SPRINT_TARGETS["extraction"]
        and cat_acc >= SPRINT_TARGETS.get("categorisation", 0.0)
    )

    return {
        "sprint": CURRENT_SPRINT,
        "timestamp": datetime.now().isoformat(),
        "targets": SPRINT_TARGETS,
        "summary": {
            "total_statements": len(results),
            "total_true_transactions": total_true,
            "total_extracted": total_matched,
            "extraction_accuracy": round(extraction_acc, 4),
            "categorisation_accuracy": round(cat_acc, 4),
            "risk_recall": round(risk_recall, 4),
            "passed": passed,
        },
        "per_statement": [r.to_dict() for r in results],
    }


def _save_report(report: dict[str, Any]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / "gold_regression.json"
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Session fixture — runs the full regression once per test session
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def regression_report() -> dict[str, Any]:
    """Discover gold PDFs, run pipeline on each, compute and save the report."""
    if not GOLD_DIR.exists():
        pytest.skip(f"Gold directory not found: {GOLD_DIR}")

    pdfs = sorted(GOLD_DIR.glob("*.pdf"))
    if not pdfs:
        pytest.skip(f"No PDFs found in {GOLD_DIR}")

    results = [_evaluate_statement(p) for p in pdfs]
    report = _aggregate(results)
    report_path = _save_report(report)

    # Print summary to terminal so it's visible in CI logs
    s = report["summary"]
    print(f"\n{'─'*60}")
    print(f"  Sprint {CURRENT_SPRINT} Regression — {len(results)} statements")
    print(f"  Extraction:      {s['extraction_accuracy']:.1%}  (target: {SPRINT_TARGETS['extraction']:.0%})")
    print(f"  Categorisation:  {s['categorisation_accuracy']:.1%}  (target: {SPRINT_TARGETS.get('categorisation', 0):.0%})")
    print(f"  Risk recall:     {s['risk_recall']:.1%}")
    print(f"  Report saved:    {report_path}")
    print(f"{'─'*60}\n")

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Tests — these FAIL until the real pipeline is wired in (expected)
# ─────────────────────────────────────────────────────────────────────────────

def test_all_gold_statements_processed(regression_report: dict[str, Any]) -> None:
    """Every PDF in data/gold/ was attempted — no silently skipped files."""
    pdfs_on_disk = len(list(GOLD_DIR.glob("*.pdf")))
    processed = regression_report["summary"]["total_statements"]
    assert processed == pdfs_on_disk, (
        f"Expected {pdfs_on_disk} statements processed, got {processed}"
    )


def test_no_pipeline_errors(regression_report: dict[str, Any]) -> None:
    """Pipeline raised no unhandled exceptions on any gold statement."""
    errors = [
        r for r in regression_report["per_statement"] if r["error"] is not None
    ]
    assert not errors, (
        f"Pipeline errors on {len(errors)} statement(s):\n"
        + "\n".join(f"  {e['statement_id']}: {e['error']}" for e in errors)
    )


def test_extraction_accuracy(regression_report: dict[str, Any]) -> None:
    """Extraction accuracy meets the Sprint 1 target of ≥88%.

    EXPECTED TO FAIL until pipeline is wired in — stub returns 0%.
    """
    target = SPRINT_TARGETS["extraction"]
    actual = regression_report["summary"]["extraction_accuracy"]
    assert actual >= target, (
        f"Extraction accuracy {actual:.1%} is below Sprint {CURRENT_SPRINT} "
        f"target of {target:.0%}. "
        f"Pipeline stub is active — wire in the real Orchestrator to pass this."
    )


def test_categorisation_accuracy(regression_report: dict[str, Any]) -> None:
    """Categorisation accuracy meets the Sprint 1 target of ≥82%.

    EXPECTED TO FAIL until categoriser is wired in — stub returns 0%.
    """
    target = SPRINT_TARGETS.get("categorisation", 0.0)
    actual = regression_report["summary"]["categorisation_accuracy"]
    assert actual >= target, (
        f"Categorisation accuracy {actual:.1%} is below Sprint {CURRENT_SPRINT} "
        f"target of {target:.0%}. "
        f"Pipeline stub is active — wire in the real Orchestrator to pass this."
    )


def test_report_written(regression_report: dict[str, Any]) -> None:
    """Regression report JSON was written to results/sprint1/."""
    report_path = RESULTS_DIR / "gold_regression.json"
    assert report_path.exists(), f"Report not written: {report_path}"
    data = json.loads(report_path.read_text())
    assert "summary" in data
    assert "per_statement" in data
    assert len(data["per_statement"]) > 0
