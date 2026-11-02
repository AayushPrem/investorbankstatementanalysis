"""Phase 6: churn model, selection helpers, serving scorer and pipeline parity."""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from analysis.customer_analytics import ChurnRisk, CustomerAnalyticsAnalyst  # noqa: E402
from db.serialize import to_jsonable  # noqa: E402
from ml import registry  # noqa: E402
from ml.churn import MODEL_NAME  # noqa: E402
from ml.churn.infer import ChurnScorer, resolve_model, resolve_scorer  # noqa: E402
from ml.churn.metrics import best_f1_threshold, bootstrap_ci, pr_auc, precision_at_top, prf  # noqa: E402
from ml.churn.model import ChurnModel, fit_calibrator, make_estimator  # noqa: E402
from ml.churn.train import calibrate_and_threshold, choose_min_history  # noqa: E402
from ml.features.churn_features import FEATURE_VERSION, FEATURES, company_units, is_active  # noqa: E402
from schema.canonical import (  # noqa: E402
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)

_GOLD = Path("data/gold")
_HDFC_STEM = "hdfc_healthy_saas_5d2a8b"
VERSION = "churn1-test-000000"
NAMES = ["ACME SOFTWARE", "ORBIT LOGISTICS", "ZEN RETAIL", "HELIX LABS", "NOVA FOODS", "PIXEL MEDIA",
         "TERRA BUILD", "AQUA LABS"]


def _payments(company: str, seed: int) -> pd.DataFrame:
    """Monthly payers; some stop at a random month (churn), weakly preceded by gaps."""
    rng = random.Random(seed)
    out = []
    for name in NAMES:
        stop = rng.randint(6, 30) if rng.random() < 0.5 else 99
        for m in range(30):
            if m >= stop:
                break
            if m >= stop - 2 and rng.random() < 0.5:
                continue                                        # pre-churn skips
            d = date(2022, 1, 1) + timedelta(days=30 * m + rng.randint(0, 4))
            out.append((company, d, rng.uniform(4e4, 6e4) * (0.8 if m >= stop - 2 else 1), name.lower()))
    return pd.DataFrame(out, columns=["company_id", "date", "amount", "key"])


@pytest.fixture(scope="module")
def units() -> pd.DataFrame:
    cutoffs = [date(2022, 1, 1) + timedelta(days=30 * m) for m in range(6, 26, 2)]
    parts = [company_units(_payments(f"c{i}", i), cutoffs).assign(company_id=f"c{i}") for i in range(16)]
    return pd.concat(parts, ignore_index=True)


@pytest.fixture(scope="module")
def tiny_model(units: pd.DataFrame) -> ChurnModel:
    cand = {"name": "lr_c1", "algo": "lr", "C": 1.0}
    est = make_estimator(cand, 0)
    x, y = units[list(FEATURES)].to_numpy(dtype=float), units["y"].to_numpy()
    est.fit(x, y)
    cal = fit_calibrator(est.predict_proba(x)[:, 1], y)
    return ChurnModel(estimator=est, calibrator=cal, features=FEATURES, feature_version=FEATURE_VERSION,
                      candidate=cand, selected={"decision_threshold": 0.5})


def _scorer(model: ChurnModel, **kw: float) -> ChurnScorer:
    args = {"decision_threshold": 0.5, "band_medium": 0.2, "min_history_payments": 2,
            "active_window_days": 90, "rule_silence_days": 60}
    args.update(kw)
    return ChurnScorer(model, VERSION, **args)  # type: ignore[arg-type]


def _doc() -> StatementDocument:
    """Resolved customers (customer_id set) with narrations whose key is the company name."""
    pay = _payments("acct", 3)
    txns = []
    for i, r in enumerate(pay.sort_values("date", kind="mergesort").itertuples(index=False)):
        txns.append(CanonicalTransaction(
            transaction_id=f"t{i:04d}", date=r.date,
            description=f"NEFT CR-HDFC0001234-{r.key.upper()} PVT LTD-INV{i}",
            credit=Decimal(str(round(r.amount, 2))), balance=Decimal("0"),
            category=TransactionCategory.REVENUE, customer_id="cust_" + r.key.replace(" ", "_"),
            source_reference=SourceReference(file_path="t", page=0, row=i, raw_text="")))
    return StatementDocument(account_id="acct", statement_period_start=txns[0].date,
                             statement_period_end=txns[-1].date, source_format="t", transactions=txns)


# ── model / selection helpers ──────────────────────────────────────────────

def test_model_scores_are_probabilities_and_learn_something(units: pd.DataFrame, tiny_model: ChurnModel) -> None:
    p = tiny_model.predict_proba(units)
    assert ((p >= 0) & (p <= 1)).all() and len(p) == len(units)
    y = units["y"].to_numpy()
    assert pr_auc(y, p) > y.mean()                       # better than the base rate in-sample


def test_calibration_threshold_and_min_history(units: pd.DataFrame, tiny_model: ChurnModel) -> None:
    raw = tiny_model.raw_score(units)
    cal = calibrate_and_threshold(units, raw)
    assert 0 < cal["decision_threshold"] < 1
    assert cal["band_medium"] == pytest.approx(units.loc[is_active(units), "y"].mean(), abs=1e-4)
    mh = choose_min_history(units, raw, [2, 3, 4, 6])
    assert mh["min_history_payments"] in (2, 3, 4, 6) and len(mh["buckets"]) == 4


def test_metric_helpers() -> None:
    y = np.array([0, 0, 1, 1, 0, 1])
    s = np.array([0.1, 0.2, 0.9, 0.8, 0.3, 0.7])
    assert pr_auc(y, s) == 1.0
    thr, f1 = best_f1_threshold(y, s)
    assert f1 == 1.0 and 0.3 < thr <= 0.7
    assert prf(y, (s >= 0.5).astype(int))["precision"] == 1.0
    f = pd.DataFrame({"company_id": list("aabbcc"), "cutoff_date": 1, "y": y, "s": s})
    top = precision_at_top(f, "s", 0.5)
    assert top["p_top_per_statement"] == pytest.approx(2 / 3) and top["p_top_global"] == 1.0
    ci = bootstrap_ci(f, lambda g: float(g["y"].mean()), 200, 0)
    assert ci["lo"] <= ci["value"] <= ci["hi"]


# ── serving scorer ─────────────────────────────────────────────────────────

def test_scorer_covers_active_customers_only(tiny_model: ChurnModel) -> None:
    doc = _doc()
    risks = _scorer(tiny_model).score(doc)
    last = max(t.date for t in doc.transactions)
    active = {t.customer_id for t in doc.transactions if (last - t.date).days <= 90}
    assert set(risks) == active and risks
    for r in risks.values():
        assert isinstance(r, ChurnRisk)
        assert r.source == "model" and r.model_version == VERSION and 0 <= (r.score or 0) <= 1
        assert r.band in ("high", "medium", "low")


def test_scorer_uses_rule_below_min_history(tiny_model: ChurnModel) -> None:
    risks = _scorer(tiny_model, min_history_payments=10_000).score(_doc())
    assert risks and all(r.source == "rule" and r.score is None and r.model_version is None
                         and r.band in ("elevated", "low") for r in risks.values())


def test_analyst_rule_outputs_identical_with_and_without_scorer(tiny_model: ChurnModel) -> None:
    doc = _doc()
    off = CustomerAnalyticsAnalyst().analyse(doc)
    on = CustomerAnalyticsAnalyst(churn_scorer=_scorer(tiny_model)).analyse(doc)
    assert off.churn_risk is None and on.churn_risk
    j_off, j_on = to_jsonable(off), to_jsonable(on)
    assert "churn_risk" not in j_off                     # omitted while None
    assert j_on.pop("churn_risk") and j_on == j_off      # everything else identical


def test_analyst_survives_scorer_failure() -> None:
    class Boom:
        version = "boom"

        def score(self, doc: object) -> dict[str, ChurnRisk]:
            raise RuntimeError("boom")

    rep = CustomerAnalyticsAnalyst(churn_scorer=Boom()).analyse(_doc())
    assert rep.churn_risk is None and rep.monthly_active_customers


# ── registry / resolve ─────────────────────────────────────────────────────

def _save(model: ChurnModel, root: Path) -> None:
    registry.save(model, name=MODEL_NAME, version=VERSION, metadata={"synthetic": True}, root=root)


def test_resolve_paths(tiny_model: ChurnModel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CHURN_ML_ENABLED", "true")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("CHURN_MODEL_VERSION", "churn1-does-not-exist")
    assert resolve_model() is None
    monkeypatch.setenv("CHURN_MODEL_VERSION", "unset")
    assert resolve_model() is None
    _save(tiny_model, tmp_path)
    monkeypatch.setenv("CHURN_MODEL_VERSION", VERSION)
    sc = resolve_scorer()
    assert sc is not None and sc.version == VERSION
    monkeypatch.setenv("CHURN_ML_ENABLED", "false")
    assert resolve_model() is None and resolve_scorer() is None
    monkeypatch.setenv("CHURN_ML_ENABLED", "true")
    art = registry.model_dir(MODEL_NAME, VERSION, tmp_path) / registry.MODEL_FILE
    art.write_bytes(art.read_bytes()[:-1] + b"\x00")
    resolve_model.__globals__["_load_cached"].cache_clear()
    assert resolve_model() is None                       # sha256 mismatch -> None


# ── training data path never opens labels/ ─────────────────────────────────

def test_training_units_never_read_labels(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ml.anomaly.data import categoriser_version
    from ml.churn import data as churn_data

    corpus = tmp_path / "c"
    (corpus / "features").mkdir(parents=True)
    (corpus / "splits").mkdir()
    pay = _payments("c1", 1)
    rows = pd.DataFrame({
        "txn_id": [f"x{i}" for i in range(len(pay))], "company_id": "c1", "account_id": "a1",
        "date": pay["date"], "row_seq": range(len(pay)),
        "description": [f"NEFT CR-HDFC0001234-{k.upper()} LTD-INV{i}" for i, k in enumerate(pay["key"])],
        "debit": None, "credit": pay["amount"].round(2), "balance": 0.0})
    rows.to_parquet(corpus / "features" / "transactions.parquet", index=False)
    pd.DataFrame({"company_id": ["c1", "c1"], "cutoff_date": [date(2022, 9, 30), date(2023, 3, 31)],
                  "split": ["train", "test"]}).to_parquet(corpus / "splits" / "churn_temporal.parquet")
    cache = tmp_path / "pred"
    cache.mkdir()
    pd.DataFrame({"txn_id": rows["txn_id"], "pred_category": "REVENUE"}).to_parquet(
        cache / f"pred_categories-deadbeef-{categoriser_version()}.parquet", index=False)

    real = pd.read_parquet

    def guarded(path: object, *a: object, **k: object) -> pd.DataFrame:
        assert "labels" not in Path(str(path)).parts, f"training path read {path}"
        return real(path, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(pd, "read_parquet", guarded)
    payments, _ = churn_data.corpus_payments(corpus, {"corpus_hash": "deadbeef" * 8}, 1, pred_cache_dir=cache)
    units = churn_data.build_units(payments, churn_data.churn_splits(corpus))
    assert set(units["split"]) == {"train", "test"} and set(units["y"]) <= {0, 1}


# ── pipeline parity: churn_risk is additive; rule churn events unchanged ───

@pytest.mark.skipif(not (_GOLD / f"{_HDFC_STEM}.pdf").exists(), reason="gold set not generated")
def test_pipeline_churn_risk_changes_nothing_else(tiny_model: ChurnModel,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline import run as run_mod
    from pipeline.versioning import summarise_result

    data = (_GOLD / f"{_HDFC_STEM}.pdf").read_bytes()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(run_mod, "resolve_churn_scorer", lambda: None)
    off = run_mod.run_analysis(data, f"{_HDFC_STEM}.pdf")
    monkeypatch.setattr(run_mod, "resolve_churn_scorer", lambda: _scorer(tiny_model))
    on = run_mod.run_analysis(data, f"{_HDFC_STEM}.pdf")

    assert off.customer_analytics.churn_risk is None
    assert on.customer_analytics.churn_risk is not None
    assert on.stage_names == off.stage_names
    j_on, j_off = to_jsonable(on.customer_analytics), to_jsonable(off.customer_analytics)
    assert "churn_risk" not in j_off
    j_on.pop("churn_risk")
    assert j_on == j_off                                 # rule churn events etc. identical
    assert [(f.detector_name, f.triggering_transaction_ids) for f in on.risk_report.flags] == \
           [(f.detector_name, f.triggering_transaction_ids) for f in off.risk_report.flags]
    assert on.narrative == off.narrative
    assert to_jsonable(on.health_report) == to_jsonable(off.health_report)
    s_on, s_off = summarise_result(on), summarise_result(off)
    assert "churn_risk_count" not in s_off and "churn_model_version" not in s_off
    assert s_on.pop("churn_risk_count") == len(on.customer_analytics.churn_risk)
    s_on.pop("churn_model_version")
    assert s_on == s_off
