"""Gold-standard regression test suite.

Discovers all statements in data/gold/, runs the REAL Sprint 1-3 pipeline
(demo/pipeline_runner.run_pipeline_on_bytes — the same code path the demo UI
and batch processing use) on each, compares against the paired ground-truth
JSON in data/gold/labels/, and asserts accuracy against fixed targets.

Metrics measured and asserted:
  - extraction        — % of true transactions the pipeline found
  - categorisation     — % of matched transactions with the correct category
  - risk_recall         — % of injected non-compliance risk flags detected
                          (structuring, round_tripping, founder_extraction,
                          customer_churn, revenue_concentration, related_party_leakage)
  - compliance_recall    — % of injected compliance flags detected
                          (compliance_269st, compliance_gst_gap, compliance_tds_gap,
                          compliance_pmla_cash, compliance_rpt_concentration,
                          compliance_cash_loan)
  - customer_id_accuracy  — pairwise clustering accuracy of resolved customer_id
                            against the true customer_id recorded per REVENUE txn
                            (Rand-index-style: for every pair of matched revenue
                            transactions with a known true customer, correct if
                            "resolved same cluster" agrees with "true same customer")

NOT measured — reconciliation_recall: there is no ground-truth "declared
claims" fixture (the CompanyClaims a company would submit, paired with a known
intentional mismatch) anywhere in the gold set or the synthetic generator.
Rather than fabricate a number, this is explicitly reported as unmeasurable
(see test_reconciliation_recall_not_yet_measured). Designing and adding such
fixtures is an open item, not done here.

To run:
    make regression
    pytest tests/regression/ -v
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from itertools import combinations
from pathlib import Path
from typing import Any

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# Accuracy targets
# ─────────────────────────────────────────────────────────────────────────────
# Every metric below is backed by an implemented Sprint 1-3 agent, so all of
# them are checked on every run — there is no more sprint-staged subset.

TARGETS: dict[str, float] = {
    "extraction": 0.88,
    "categorisation": 0.88,
    "risk_recall": 0.85,
    "compliance_recall": 0.90,
    "customer_id": 0.90,
}

# ─────────────────────────────────────────────────────────────────────────────
# Paths
# ─────────────────────────────────────────────────────────────────────────────

GOLD_DIR = Path("data/gold")
LABELS_DIR = GOLD_DIR / "labels"
# Kept at its original Sprint-1 location — tools/generate_deck.py reads this
# exact path for the system-tour deck.
RESULTS_DIR = Path("results") / "sprint1"

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
    transactions: list[ExtractedTransaction] = field(default_factory=list)
    detected_flag_types: set[str] = field(default_factory=set)


# ─────────────────────────────────────────────────────────────────────────────
# Flag-type ground-truth mapping
# ─────────────────────────────────────────────────────────────────────────────
# The synthetic generator's injected_flags (data/gold/labels/*.truth.json)
# record ground truth using its own flag_type vocabulary, which doesn't map
# 1:1 onto any single report field: some are RiskAnalyst detector names,
# "customer_churn" is a CustomerAnalyticsReport signal (RiskAnalyst has no
# churn detector), "related_party_leakage" / "compliance_rpt_concentration"
# are related-party TAGGING signals (RelatedPartyTagger, not a "flag" at
# all), and the compliance_* types are ComplianceReport rule_ids.

_RISK_DETECTOR_TO_FLAG_TYPE: dict[str, str] = {
    "structuring": "structuring",
    "round_tripping": "round_tripping",
    "founder_over_extraction": "founder_extraction",
    "customer_concentration": "revenue_concentration",
}

_COMPLIANCE_RULE_TO_FLAG_TYPE: dict[str, str] = {
    "IND_269ST": "compliance_269st",
    "IND_GST_CONSISTENCY": "compliance_gst_gap",
    "IND_TDS_PATTERN": "compliance_tds_gap",
    "IND_PMLA_HVC": "compliance_pmla_cash",
    "IND_RPT_CONCENTRATION": "compliance_rpt_concentration",
    "IND_269SS_269T": "compliance_cash_loan",
}

# Both of these flag types require the injected entity to be supplied as a
# known affiliate before RelatedPartyTagger will tag anything at all — see
# _affiliates_for_statement(). Detection signal for either is simply "did any
# transaction end up tagged is_related_party=True".
_RELATED_PARTY_FLAG_TYPES = {"related_party_leakage", "compliance_rpt_concentration"}

# Wave 4.3 hard-case flag types that DO have a real "was this detected?"
# signal outside the risk/compliance detector set above.
_OTHER_DETECTABLE_FLAG_TYPES = {"aggregator_dominated_revenue"}

# Wave 4.3 hard-case flag types with NO detection concept at all — they are
# structural/robustness scenarios (self-transfers, a missing month, FX-style
# narrations), not risk signals a detector is meant to catch. Recall would be
# meaningless for these, so they're excluded from risk_recall/compliance_recall
# entirely rather than silently counted as permanent misses.
_NOT_RECALL_ELIGIBLE = {"inter_account_transfer", "missing_month_gap", "fx_inflow"}

_RECALL_ELIGIBLE_FLAG_TYPES = (
    set(_RISK_DETECTOR_TO_FLAG_TYPE.values())
    | set(_COMPLIANCE_RULE_TO_FLAG_TYPE.values())
    | _RELATED_PARTY_FLAG_TYPES
    | _OTHER_DETECTABLE_FLAG_TYPES
    | {"customer_churn"}
)


def _affiliates_for_statement(truth: dict[str, Any]) -> list[Any]:
    """Reconstruct the affiliate list a due-diligence user would supply, from
    the injector's own ground truth (the 'related_entity' it planted)."""
    from pipeline.related_party import Affiliate

    names: set[str] = set()
    for f in truth.get("injected_flags", []):
        if f.get("flag_type") in _RELATED_PARTY_FLAG_TYPES:
            entity = f.get("related_entity")
            if entity:
                names.add(entity)
    return [Affiliate(name=n, relationship="disclosed_affiliate") for n in sorted(names)]


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline entry point — the REAL Sprint 1-3 pipeline, not a stub
# ─────────────────────────────────────────────────────────────────────────────

def _convert_demo_result(result: Any) -> PipelineOutput:
    """Map DemoResult -> PipelineOutput for the regression harness."""
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

    detected: set[str] = set()
    for flag in result.risk_report.flags:
        mapped = _RISK_DETECTOR_TO_FLAG_TYPE.get(flag.detector_name)
        if mapped:
            detected.add(mapped)
    if result.customer_analytics.churn_events:
        detected.add("customer_churn")
    if result.customer_analytics.is_aggregator_dominated:
        detected.add("aggregator_dominated_revenue")
    if any(t.is_related_party for t in result.doc.transactions):
        detected.add("related_party_leakage")
        detected.add("compliance_rpt_concentration")
    for exc in result.compliance_report.exceptions:
        mapped = _COMPLIANCE_RULE_TO_FLAG_TYPE.get(exc.rule_id)
        if mapped:
            detected.add(mapped)

    return PipelineOutput(transactions=txns, detected_flag_types=detected)


def run_pipeline(pdf_path: Path, truth: dict[str, Any]) -> PipelineOutput:
    """Run the full Sprint 1-3 pipeline on a PDF and return structured output.

    Uses demo/pipeline_runner.run_pipeline_on_bytes — the same function the
    demo UI and batch processing call — so this exercises every implemented
    agent (adapter, normaliser, validator, categoriser, related-party tagger,
    customer identity resolver, risk analyst, customer analytics, compliance,
    reconciliation, and all four report generators), not a Sprint-1-only stub.
    """
    from demo.pipeline_runner import run_pipeline_on_bytes

    affiliates = _affiliates_for_statement(truth)
    result = run_pipeline_on_bytes(pdf_path.read_bytes(), pdf_path.name, affiliates=affiliates)
    return _convert_demo_result(result)


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
    customer_id_pairs_total: int = 0
    customer_id_pairs_correct: int = 0
    error: str | None = None

    @property
    def extraction_accuracy(self) -> float:
        return self.matched_count / self.true_count if self.true_count else 0.0

    @property
    def categorisation_accuracy(self) -> float:
        return self.correct_categories / self.matched_count if self.matched_count else 0.0

    def _recall(self, *, compliance: bool) -> float | None:
        expected = {
            f for f in self.injected_flag_types
            if f.startswith("compliance_") == compliance
            and f in _RECALL_ELIGIBLE_FLAG_TYPES
        }
        if not expected:
            return None  # not applicable — this statement injected none of this kind
        detected = set(self.detected_flag_types)
        return len(detected & expected) / len(expected)

    @property
    def risk_recall(self) -> float | None:
        return self._recall(compliance=False)

    @property
    def compliance_recall(self) -> float | None:
        return self._recall(compliance=True)

    @property
    def customer_id_accuracy(self) -> float | None:
        if self.customer_id_pairs_total == 0:
            return None
        return self.customer_id_pairs_correct / self.customer_id_pairs_total

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
            "risk_recall": self.risk_recall,
            "compliance_recall": self.compliance_recall,
            "customer_id_accuracy": self.customer_id_accuracy,
            "customer_id_pairs_evaluated": self.customer_id_pairs_total,
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
        output = run_pipeline(pdf_path, truth)
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
    # (resolved_customer_id, true_customer_id) for every matched REVENUE txn
    # that has a known true customer — feeds the pairwise clustering check.
    customer_pairs: list[tuple[str | None, str]] = []
    for t in true_txns:
        hit = _match_transaction(t, output.transactions, used)
        if hit:
            matched += 1
            if hit.category == t.get("category"):
                correct_cats += 1
            true_cid = t.get("customer_id")
            if true_cid:
                customer_pairs.append((hit.customer_id, true_cid))

    pairs_total = 0
    pairs_correct = 0
    for (resolved_a, true_a), (resolved_b, true_b) in combinations(customer_pairs, 2):
        pairs_total += 1
        if (resolved_a == resolved_b) == (true_a == true_b):
            pairs_correct += 1

    return StatementResult(
        statement_id=sid,
        pdf_path=str(pdf_path),
        true_count=len(true_txns),
        extracted_count=len(output.transactions),
        matched_count=matched,
        correct_categories=correct_cats,
        injected_flag_types=injected_flags,
        detected_flag_types=sorted(output.detected_flag_types),
        customer_id_pairs_total=pairs_total,
        customer_id_pairs_correct=pairs_correct,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Aggregate report
# ─────────────────────────────────────────────────────────────────────────────

def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _aggregate(results: list[StatementResult]) -> dict[str, Any]:
    total_true = sum(r.true_count for r in results)
    total_matched = sum(r.matched_count for r in results)
    total_correct_cats = sum(r.correct_categories for r in results)

    extraction_acc = total_matched / total_true if total_true else 0.0
    cat_acc = total_correct_cats / total_matched if total_matched else 0.0

    risk_recall = _mean([r.risk_recall for r in results if r.risk_recall is not None])
    risk_recall_n = sum(1 for r in results if r.risk_recall is not None)

    compliance_recall = _mean([r.compliance_recall for r in results if r.compliance_recall is not None])
    compliance_recall_n = sum(1 for r in results if r.compliance_recall is not None)

    total_cid_pairs = sum(r.customer_id_pairs_total for r in results)
    total_cid_correct = sum(r.customer_id_pairs_correct for r in results)
    customer_id_acc = total_cid_correct / total_cid_pairs if total_cid_pairs else None

    return {
        "timestamp": datetime.now().isoformat(),
        "targets": TARGETS,
        "summary": {
            "total_statements": len(results),
            "total_true_transactions": total_true,
            "total_matched": total_matched,
            "extraction_accuracy": round(extraction_acc, 4),
            "categorisation_accuracy": round(cat_acc, 4),
            "risk_recall": round(risk_recall, 4) if risk_recall is not None else None,
            "risk_recall_statements_evaluated": risk_recall_n,
            "compliance_recall": round(compliance_recall, 4) if compliance_recall is not None else None,
            "compliance_recall_statements_evaluated": compliance_recall_n,
            "customer_id_accuracy": round(customer_id_acc, 4) if customer_id_acc is not None else None,
            "customer_id_pairs_evaluated": total_cid_pairs,
            "reconciliation_recall": None,
            "reconciliation_recall_note": (
                "Not measured — no ground-truth 'declared claims' fixtures exist in the "
                "gold set yet (no CompanyClaims paired with a known intentional mismatch). "
                "This is an open item, not a computed 0%."
            ),
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
    """Discover gold PDFs, run the real pipeline on each, compute and save the report."""
    if not GOLD_DIR.exists():
        pytest.skip(f"Gold directory not found: {GOLD_DIR}")

    pdfs = sorted(GOLD_DIR.glob("*.pdf"))
    if not pdfs:
        pytest.skip(f"No PDFs found in {GOLD_DIR}")

    # Force deterministic rule-only mode — the regression suite must be
    # reproducible without API calls or cost.
    saved_key = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        results = [_evaluate_statement(p) for p in pdfs]
    finally:
        if saved_key is not None:
            os.environ["ANTHROPIC_API_KEY"] = saved_key

    report = _aggregate(results)
    report_path = _save_report(report)

    # Print summary to terminal so it's visible in CI logs
    s = report["summary"]
    def _fmt(v: float | None) -> str:
        return f"{v:.1%}" if v is not None else "N/A"

    print(f"\n{'─'*60}")
    print(f"  Gold-Set Regression — {len(results)} statements")
    print(f"  Extraction:         {_fmt(s['extraction_accuracy'])}  (target: {TARGETS['extraction']:.0%})")
    print(f"  Categorisation:     {_fmt(s['categorisation_accuracy'])}  (target: {TARGETS['categorisation']:.0%})")
    print(f"  Risk recall:        {_fmt(s['risk_recall'])}  (target: {TARGETS['risk_recall']:.0%}, "
          f"n={s['risk_recall_statements_evaluated']})")
    print(f"  Compliance recall:  {_fmt(s['compliance_recall'])}  (target: {TARGETS['compliance_recall']:.0%}, "
          f"n={s['compliance_recall_statements_evaluated']})")
    print(f"  Customer ID acc.:   {_fmt(s['customer_id_accuracy'])}  (target: {TARGETS['customer_id']:.0%}, "
          f"pairs={s['customer_id_pairs_evaluated']})")
    print(f"  Reconciliation:     NOT MEASURED — {s['reconciliation_recall_note']}")
    print(f"  Report saved:       {report_path}")
    print(f"{'─'*60}\n")

    return report


# ─────────────────────────────────────────────────────────────────────────────
# Tests
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
    """Extraction accuracy meets target."""
    target = TARGETS["extraction"]
    actual = regression_report["summary"]["extraction_accuracy"]
    assert actual >= target, (
        f"Extraction accuracy {actual:.1%} is below target of {target:.0%}."
    )


def test_categorisation_accuracy(regression_report: dict[str, Any]) -> None:
    """Categorisation accuracy meets target."""
    target = TARGETS["categorisation"]
    actual = regression_report["summary"]["categorisation_accuracy"]
    assert actual >= target, (
        f"Categorisation accuracy {actual:.1%} is below target of {target:.0%}."
    )


def test_risk_recall(regression_report: dict[str, Any]) -> None:
    """Risk-flag recall meets target. A metric that couldn't be computed at
    all (no statement injected a risk flag) is a hard failure, not a silent
    pass — that would mean the corpus regressed to having no risk coverage."""
    actual = regression_report["summary"]["risk_recall"]
    if actual is None:
        pytest.fail(
            "risk_recall could not be computed — no gold statement has any "
            "non-compliance risk flag injected. This is a regression in gold-set "
            "coverage, not a metric of 0%."
        )
    target = TARGETS["risk_recall"]
    assert actual >= target, f"Risk recall {actual:.1%} is below target of {target:.0%}."


def test_compliance_recall(regression_report: dict[str, Any]) -> None:
    """Compliance-flag recall meets target. Same fail-loud rule as risk_recall."""
    actual = regression_report["summary"]["compliance_recall"]
    if actual is None:
        pytest.fail(
            "compliance_recall could not be computed — no gold statement has any "
            "compliance flag injected. This is a regression in gold-set coverage, "
            "not a metric of 0%."
        )
    target = TARGETS["compliance_recall"]
    assert actual >= target, f"Compliance recall {actual:.1%} is below target of {target:.0%}."


def test_customer_id_accuracy(regression_report: dict[str, Any]) -> None:
    """Customer-identity resolution pairwise clustering accuracy meets target."""
    actual = regression_report["summary"]["customer_id_accuracy"]
    if actual is None:
        pytest.fail(
            "customer_id_accuracy could not be computed — no matched REVENUE "
            "transaction pairs with a known true customer_id were found."
        )
    target = TARGETS["customer_id"]
    assert actual >= target, f"Customer ID accuracy {actual:.1%} is below target of {target:.0%}."


def test_reconciliation_recall_not_yet_measured(regression_report: dict[str, Any]) -> None:
    """Documents the known gap explicitly rather than letting it silently
    regress into a fabricated pass/fail number. If this starts failing because
    reconciliation_recall is no longer None, declared-claims ground-truth
    fixtures now exist — update this test (and add a real assertion + target)
    instead of just deleting it."""
    assert regression_report["summary"]["reconciliation_recall"] is None, (
        "reconciliation_recall is no longer None — ground-truth fixtures appear to "
        "exist now. Replace this test with a real assertion against TARGETS."
    )


def test_report_written(regression_report: dict[str, Any]) -> None:
    """Regression report JSON was written."""
    report_path = RESULTS_DIR / "gold_regression.json"
    assert report_path.exists(), f"Report not written: {report_path}"
    data = json.loads(report_path.read_text())
    assert "summary" in data
    assert "per_statement" in data
    assert len(data["per_statement"]) > 0
