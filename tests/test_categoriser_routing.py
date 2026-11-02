"""Categoriser v3 routing: rule → ML ≥ τ_accept → LLM(top-3) → review (Phase 4).

Uses a stub model with fixed probabilities, so every branch and τ boundary
is exercised without a trained artifact. Real-model behaviour is covered in
tests/test_ml_categoriser.py.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any
from unittest.mock import patch

import numpy as np
import pytest

from analysis.categoriser import (
    LLM_REVIEW,
    Categoriser,
    _parse_llm_review_response,
    _rule_classify,
    match_high_precision_rule,
    route_llm_answer,
    route_ml,
    route_without_llm,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
    ValidationStatus,
)

CLASSES = ["FOUNDER_WITHDRAWAL", "OTHER", "REVENUE", "SALARY", "TRANSFER", "VENDOR_PAYMENT"]
HP = (0, 1, 4, 5, 8, 9, 10)


class StubModel:
    """predict_proba keyed by description: {desc: {class: p}}."""

    version = "stub-1"

    def __init__(self, table: dict[str, dict[str, float]]) -> None:
        self.classes = CLASSES
        self.table = table
        self.calls = 0

    def predict_proba_transactions(self, txns: list[CanonicalTransaction]) -> np.ndarray:
        self.calls += 1
        out = np.zeros((len(txns), len(CLASSES)))
        for i, t in enumerate(txns):
            for c, p in self.table[t.description].items():
                out[i, CLASSES.index(c)] = p
        return out


def _txn(desc: str, *, credit: str | None = None, debit: str | None = None, i: int = 0) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=f"t{i}", date=date(2024, 4, 1), description=desc,
        credit=Decimal(credit) if credit else None, debit=Decimal(debit) if debit else None,
        balance=Decimal("100000"),
        source_reference=SourceReference(file_path="x.pdf", page=0, row=i, raw_text=""),
    )


def _doc(txns: list[CanonicalTransaction]) -> StatementDocument:
    return StatementDocument(
        account_id="1", statement_period_start=date(2024, 4, 1), statement_period_end=date(2024, 4, 30),
        source_format="digital_pdf_hdfc", validation_status=ValidationStatus.UNVALIDATED,
        transactions=txns,
    )


ROWS = {
    # description: (direction kwargs, stub probabilities)
    "NEFT DR-ACME-SALARY APR": ({"debit": "50000"}, None),                     # rule 0 (salary)
    "NEFT CR-ZEN CORP-UTR1": ({"credit": "10000"}, {"REVENUE": 0.99, "OTHER": 0.01}),
    "IMPS/RAVI KUMAR/ABC": ({"debit": "9000"}, {"SALARY": 0.70, "VENDOR_PAYMENT": 0.25, "OTHER": 0.05}),
    "MYSTERY DEBIT 42": ({"debit": "777"}, {"VENDOR_PAYMENT": 0.40, "FOUNDER_WITHDRAWAL": 0.35,
                                            "TRANSFER": 0.15, "OTHER": 0.10}),
}


def _build() -> tuple[StatementDocument, StubModel]:
    txns = [_txn(d, i=i, **kw) for i, (d, (kw, _)) in enumerate(ROWS.items())]
    model = StubModel({d: p for d, (_, p) in ROWS.items() if p is not None})
    return _doc(txns), model


def _cat(model: Any, **kw: Any) -> Categoriser:
    return Categoriser(llm_enabled=False, model=model, tau_accept=0.95, tau_review=0.50,
                       high_precision_rules=HP, **kw)


# ── pure routing functions ──────────────────────────────────────────────────

@pytest.mark.parametrize("p,tau,expected", [(0.95, 0.95, True), (0.9499, 0.95, False), (1.0, 0.95, True)])
def test_route_ml_boundary_is_inclusive(p: float, tau: float, expected: bool) -> None:
    assert route_ml(p, tau) is expected


@pytest.mark.parametrize("p,expected", [(0.50, "ml"), (0.4999, "review"), (0.0, "review")])
def test_route_without_llm_boundary(p: float, expected: str) -> None:
    assert route_without_llm(p, 0.50) == expected


def test_route_llm_answer_must_be_in_top3() -> None:
    assert route_llm_answer("SALARY", ["SALARY", "OTHER", "REVENUE"]) == "llm"
    assert route_llm_answer("TAX", ["SALARY", "OTHER", "REVENUE"]) == "review"
    assert route_llm_answer(None, ["SALARY"]) == "review"


def test_parse_review_response() -> None:
    assert _parse_llm_review_response('["SALARY", "REVIEW", "nonsense"]', 3) == [
        TransactionCategory.SALARY, LLM_REVIEW, LLM_REVIEW]
    assert _parse_llm_review_response("not json", 2) == [None, None]
    assert _parse_llm_review_response('["SALARY"]', 2) == [None, None]


def test_high_precision_rule_respects_first_match() -> None:
    # 'SELF TRANSFER' fires rule 7 (not high-precision) first → no rule hit at all,
    # even though a later rule (catch-all) would also match.
    t = _txn("SELF TRANSFER TO OWN ACCOUNT", debit="1000")
    assert match_high_precision_rule(t, HP) is None
    assert match_high_precision_rule(_txn("GST PAYMENT Q1", debit="1"), HP) == (1, TransactionCategory.TAX)


# ── categorise(): no LLM ────────────────────────────────────────────────────

def test_v3_no_llm_routes_each_branch() -> None:
    doc, model = _build()
    _, stats = _cat(model).categorise(doc)
    by = {t.description: t for t in doc.transactions}

    r = by["NEFT DR-ACME-SALARY APR"]
    assert (r.category, r.category_source, r.category_confidence) == (TransactionCategory.SALARY, "rule", 1.0)
    r = by["NEFT CR-ZEN CORP-UTR1"]
    assert (r.category, r.category_source) == (TransactionCategory.REVENUE, "ml")
    assert r.category_confidence == pytest.approx(0.99)
    r = by["IMPS/RAVI KUMAR/ABC"]      # 0.50 ≤ 0.70 < 0.95 → degraded accept
    assert (r.category, r.category_source) == (TransactionCategory.SALARY, "ml")
    r = by["MYSTERY DEBIT 42"]          # 0.40 < τ_review → review, best guess kept
    assert (r.category, r.category_source) == (TransactionCategory.VENDOR_PAYMENT, "review")

    assert (stats.rule_matched, stats.ml_matched, stats.llm_matched, stats.review,
            stats.fallback_other) == (1, 2, 0, 1, 0)
    assert stats.rule_matched + stats.ml_matched + stats.llm_matched + stats.review \
        + stats.fallback_other == stats.total
    assert stats.model_version == "stub-1"
    assert all(t.category is not None for t in doc.transactions)


def test_v3_model_not_called_when_rules_cover_everything() -> None:
    doc = _doc([_txn("SALARY APR", debit="1", i=0), _txn("GST PAYMENT", debit="2", i=1)])
    model = StubModel({})
    _, stats = _cat(model).categorise(doc)
    assert model.calls == 0 and stats.rule_matched == 2


def test_v3_tau_review_equal_tau_accept_sends_band_to_review() -> None:
    doc, model = _build()
    Categoriser(llm_enabled=False, model=model, tau_accept=0.95, tau_review=0.95,
                high_precision_rules=HP).categorise(doc)
    by = {t.description: t.category_source for t in doc.transactions}
    assert by["IMPS/RAVI KUMAR/ABC"] == "review"


def test_invalid_taus_rejected() -> None:
    with pytest.raises(ValueError):
        Categoriser(llm_enabled=False, model=None, tau_accept=0.4, tau_review=0.6)


# ── categorise(): with an LLM (mocked) ──────────────────────────────────────

def _with_llm(model: Any, answers: Any = None, side_effect: Any = None) -> tuple[StatementDocument, Any, Any]:
    doc, _ = _build()
    with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
        cat = Categoriser(llm_enabled=True, model=model, tau_accept=0.95, tau_review=0.50,
                          high_precision_rules=HP)
        with patch.object(cat, "_llm_categorise_batch", return_value=answers,
                          side_effect=side_effect) as m:
            _, stats = cat.categorise(doc)
    return doc, stats, m


def test_llm_gets_only_pending_rows_with_top3_candidates() -> None:
    _, model = _build()
    doc, stats, m = _with_llm(model, answers=[TransactionCategory.VENDOR_PAYMENT,
                                              TransactionCategory.FOUNDER_WITHDRAWAL])
    m.assert_called_once()
    sent, kwargs = m.call_args.args[0], m.call_args.kwargs
    assert [t.description for t in sent] == ["IMPS/RAVI KUMAR/ABC", "MYSTERY DEBIT 42"]
    assert kwargs["candidates"] == [["SALARY", "VENDOR_PAYMENT", "OTHER"],
                                    ["VENDOR_PAYMENT", "FOUNDER_WITHDRAWAL", "TRANSFER"]]
    by = {t.description: t for t in doc.transactions}
    assert (by["IMPS/RAVI KUMAR/ABC"].category, by["IMPS/RAVI KUMAR/ABC"].category_source) == \
        (TransactionCategory.VENDOR_PAYMENT, "llm")
    assert (by["MYSTERY DEBIT 42"].category, by["MYSTERY DEBIT 42"].category_source) == \
        (TransactionCategory.FOUNDER_WITHDRAWAL, "llm")
    assert (stats.rule_matched, stats.ml_matched, stats.llm_matched, stats.review) == (1, 1, 2, 0)


def test_llm_answer_outside_top3_goes_to_review_with_ml_guess() -> None:
    _, model = _build()
    doc, stats, _ = _with_llm(model, answers=[TransactionCategory.TAX, LLM_REVIEW])
    by = {t.description: t for t in doc.transactions}
    assert (by["IMPS/RAVI KUMAR/ABC"].category, by["IMPS/RAVI KUMAR/ABC"].category_source) == \
        (TransactionCategory.SALARY, "review")
    assert by["MYSTERY DEBIT 42"].category_source == "review"
    assert stats.review == 2 and stats.llm_matched == 0


def test_llm_failure_falls_back_to_no_llm_rule() -> None:
    _, model = _build()
    doc, stats, _ = _with_llm(model, side_effect=RuntimeError("API timeout"))
    by = {t.description: t.category_source for t in doc.transactions}
    assert by["IMPS/RAVI KUMAR/ABC"] == "ml" and by["MYSTERY DEBIT 42"] == "review"
    assert (stats.ml_matched, stats.review) == (2, 1)


def test_llm_unparseable_batch_falls_back_to_no_llm_rule() -> None:
    _, model = _build()
    doc, _, _ = _with_llm(model, answers=[None, None])
    by = {t.description: t.category_source for t in doc.transactions}
    assert by["IMPS/RAVI KUMAR/ABC"] == "ml" and by["MYSTERY DEBIT 42"] == "review"


# ── v2 fallback ─────────────────────────────────────────────────────────────

def test_no_model_is_v2_rules_only() -> None:
    doc, _ = _build()
    _, stats = Categoriser(llm_enabled=False, model=None).categorise(doc)
    for t in doc.transactions:
        assert t.category == _rule_classify(t)
        assert t.category_source == "rule"
    assert stats.rule_matched == stats.total and stats.model_version is None


def test_ml_disabled_by_setting_forces_v2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATEGORISER_ML_ENABLED", "false")
    assert Categoriser(llm_enabled=False).model_version is None


def test_missing_model_version_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATEGORISER_MODEL_VERSION", "cat3-does-not-exist")
    assert Categoriser(llm_enabled=False).model_version is None
