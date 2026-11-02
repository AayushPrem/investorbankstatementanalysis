"""Phase 5: anomaly model, registry, resolve fallbacks, flags and pipeline parity."""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from analysis.risk import Severity  # noqa: E402
from ml import registry  # noqa: E402
from ml.anomaly import MODEL_NAME  # noqa: E402
from ml.anomaly.infer import DETECTOR_NAME, AnomalyDetector, resolve_detector, resolve_model  # noqa: E402
from ml.anomaly.metrics import at_k, average_precision, set_metrics  # noqa: E402
from ml.anomaly.model import (  # noqa: E402
    SCORER_PER_ACCOUNT,
    SCORER_POOLED,
    AnomalyModel,
    ForestParams,
    fit_pooled,
    flag_mask,
    severity_of,
)
from ml.features.transaction_features import FEATURE_VERSION, STD_FEATURES, build_features, matrix  # noqa: E402
from schema.canonical import CanonicalTransaction, SourceReference, StatementDocument  # noqa: E402

_GOLD = Path("data/gold")
_HDFC_STEM = "hdfc_healthy_saas_5d2a8b"
VERSION = "anom1-test-000000"


def _rows(n: int, seed: int, account: str) -> pd.DataFrame:
    rng = random.Random(seed)
    d = date(2024, 1, 1)
    out = []
    for i in range(n):
        d += timedelta(days=rng.choice([0, 1, 1, 2]))
        vendor = rng.choice(["ACME CLOUD", "ORBIT LOGISTICS", "ZEN OFFICE"])
        out.append({"txn_id": f"{account}-{i:04d}", "account_id": account, "date": d, "row_seq": i,
                    "description": f"NEFT/SBIN0001234/{vendor} PVT LTD/INV{i}",
                    "debit": round(rng.uniform(9e3, 11e3), 2), "credit": None,
                    "pred_category": "VENDOR_PAYMENT"})
    return pd.DataFrame(out)


@pytest.fixture(scope="module")
def tiny_model() -> AnomalyModel:
    feats = pd.concat([build_features(_rows(300, s, f"a{s}")) for s in range(4)], ignore_index=True)
    params = ForestParams(n_estimators=30, max_samples=128, seed=0)
    forest, q, med, iqr = fit_pooled(matrix(feats, STD_FEATURES), params)
    return AnomalyModel(pooled=forest, pooled_ref_quantiles=q, pooled_feature_median=med,
                        pooled_feature_iqr=iqr, params=params, min_history=150,
                        feature_version=FEATURE_VERSION)


def _doc(n: int = 200, outlier: bool = True) -> StatementDocument:
    rows = _rows(n, 99, "acct")
    txns = []
    for i, r in enumerate(rows.itertuples(index=False)):
        debit = Decimal(str(r.debit))
        desc = r.description
        if outlier and i == n - 5:
            debit, desc = Decimal("987654.32"), "NEFT/HDFC0009999/NEVER SEEN TRADERS/INV1"
        txns.append(CanonicalTransaction(
            transaction_id=r.txn_id, date=r.date, description=desc, debit=debit,
            balance=Decimal("0"), category=r.pred_category,
            source_reference=SourceReference(file_path="t", page=0, row=i, raw_text="")))
    return StatementDocument(account_id="acct", statement_period_start=txns[0].date,
                             statement_period_end=txns[-1].date, source_format="t", transactions=txns)


def _detector(model: AnomalyModel, **kw: float) -> AnomalyDetector:
    args = {"contamination": 0.02, "severity_high_pct": 0.999, "severity_medium_pct": 0.995,
            "max_flags_per_statement": 3}
    args.update(kw)
    return AnomalyDetector(model, VERSION, **args)  # type: ignore[arg-type]


# ── scoring / flags ────────────────────────────────────────────────────────

def test_per_account_vs_pooled_choice(tiny_model: AnomalyModel) -> None:
    long = build_features(_rows(200, 5, "L"))
    short = build_features(_rows(100, 6, "S"))
    assert set(tiny_model.score_account(long).scorer) == {SCORER_PER_ACCOUNT}
    assert set(tiny_model.score_account(short).scorer) == {SCORER_POOLED}
    forced = tiny_model.score_account(long, force=SCORER_POOLED)
    assert set(forced.scorer) == {SCORER_POOLED}
    s = tiny_model.score_account(long)
    assert s.percentile.between(0, 1).all() and len(s) == len(long)
    assert all(len(t) == 3 for t in s.top_features)


def test_detector_flags_outlier_with_investigate_wording(tiny_model: AnomalyModel) -> None:
    rep = _detector(tiny_model).detect(_doc())
    assert 0 < len(rep.flags) <= 3                      # capped by max_flags_per_statement
    assert rep.flags[0].triggering_transaction_ids == ["acct-0195"]   # the injected outlier ranks first
    assert rep.scorer_counts == {SCORER_PER_ACCOUNT: 200}
    for f in rep.flags:
        assert f.kind == "ml" and f.detector_name == DETECTOR_NAME
        assert isinstance(f.severity, Severity)
        assert "investigate" in f.description.lower()
        assert "fraud" not in f.description.lower() and "fraud" not in str(f.evidence).lower()
        assert f.evidence["scorer"] == SCORER_PER_ACCOUNT and f.evidence["model_version"] == VERSION
        assert {"feature", "value", "deviation"} <= set(f.evidence["top_features"][0])
    pcts = [f.evidence["percentile"] for f in rep.flags]
    assert pcts == sorted(pcts, reverse=True)


def test_detector_is_deterministic(tiny_model: AnomalyModel) -> None:
    a = _detector(tiny_model).detect(_doc())
    b = _detector(tiny_model).detect(_doc())
    assert [(f.triggering_transaction_ids, f.score, f.severity) for f in a.flags] == \
           [(f.triggering_transaction_ids, f.score, f.severity) for f in b.flags]


def test_empty_document(tiny_model: AnomalyModel) -> None:
    doc = StatementDocument(account_id="e", statement_period_start=date(2024, 1, 1),
                            statement_period_end=date(2024, 1, 1), source_format="t", transactions=[])
    assert _detector(tiny_model).detect(doc).flags == []


def test_flag_mask_and_severity() -> None:
    df = pd.DataFrame({"account_id": ["a"] * 10 + ["b"] * 10,
                       "percentile": list(np.linspace(0.1, 1.0, 10)) * 2})
    m = flag_mask(df, contamination=0.25, max_flags=None)
    assert m.sum() == 6                                  # pct >= 0.75 -> 3 per account
    assert flag_mask(df, contamination=0.25, max_flags=1).sum() == 2
    assert severity_of(0.9995, 0.999, 0.995) == "HIGH"
    assert severity_of(0.996, 0.999, 0.995) == "MEDIUM"
    assert severity_of(0.99, 0.999, 0.995) == "LOW"


# ── metrics ────────────────────────────────────────────────────────────────

def test_at_k_macro_and_micro() -> None:
    df = pd.DataFrame({"company_id": ["c1"] * 4 + ["c2"] * 4, "txn_id": list("abcdefgh"),
                       "s": [4, 3, 2, 1, 4, 3, 2, 1],
                       "y": [True, False, False, True, False, False, False, False]})
    m = at_k(df, "s", "y", [2])
    assert m["p@2_macro"] == pytest.approx((0.5 + 0.0) / 2)
    assert m["p@2_micro"] == pytest.approx(1 / 4)
    assert m["r@2_macro"] == pytest.approx(0.5)          # c2 has no positives -> skipped
    assert m["r@2_micro"] == pytest.approx(0.5)
    assert average_precision(np.array([3.0, 2.0, 1.0]), np.array([True, False, True])) == \
        pytest.approx((1.0 + 2 / 3) / 2)
    sm = set_metrics(np.array([True, True, False]), np.array([True, False, True]))
    assert (sm["tp"], sm["fp"], sm["fn"]) == (1, 1, 1)


# ── registry + resolve fallbacks ───────────────────────────────────────────

def _save(model: AnomalyModel, root: Path) -> None:
    registry.save(model, name=MODEL_NAME, version=VERSION, metadata={"synthetic": True}, root=root)


def test_registry_round_trip_and_tamper(tiny_model: AnomalyModel, tmp_path: Path) -> None:
    _save(tiny_model, tmp_path)
    loaded = registry.load(MODEL_NAME, VERSION, root=tmp_path)
    assert isinstance(loaded.model, AnomalyModel)
    art = registry.model_dir(MODEL_NAME, VERSION, tmp_path) / registry.MODEL_FILE
    art.write_bytes(art.read_bytes() + b"x")
    with pytest.raises(registry.RegistryError):
        registry.load(MODEL_NAME, VERSION, root=tmp_path)


def test_resolve_none_paths(tiny_model: AnomalyModel, tmp_path: Path,
                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("ANOMALY_MODEL_VERSION", "anom1-does-not-exist")
    assert resolve_model() is None                       # missing artifact
    monkeypatch.setenv("ANOMALY_MODEL_VERSION", "unset")
    assert resolve_model() is None                       # unset
    _save(tiny_model, tmp_path)
    monkeypatch.setenv("ANOMALY_MODEL_VERSION", VERSION)
    res = resolve_model()
    assert res is not None and res[1] == VERSION
    det = resolve_detector()
    assert det is not None and det.version == VERSION
    monkeypatch.setenv("ANOMALY_ML_ENABLED", "false")
    assert resolve_model() is None and resolve_detector() is None


def test_resolve_sha_mismatch_returns_none(tiny_model: AnomalyModel, tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "tampered"
    _save(tiny_model, root)
    art = registry.model_dir(MODEL_NAME, VERSION, root) / registry.MODEL_FILE
    art.write_bytes(art.read_bytes()[:-1] + b"\x00")
    monkeypatch.setenv("MODELS_DIR", str(root))
    monkeypatch.setenv("ANOMALY_MODEL_VERSION", VERSION)
    assert resolve_model() is None


# ── pipeline: ML flags are a separate stream; no-model path unchanged ──────

@pytest.mark.skipif(not (_GOLD / f"{_HDFC_STEM}.pdf").exists(), reason="gold set not generated")
def test_pipeline_ml_stage_changes_nothing_else(tiny_model: AnomalyModel, tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from pipeline.run import run_analysis
    from pipeline.versioning import summarise_result
    from reports.workbench_lens import (
        ML_SECTION_TITLE,
        WorkbenchAnalysisResult,
        WorkbenchLensReport,
    )

    data = (_GOLD / f"{_HDFC_STEM}.pdf").read_bytes()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANOMALY_ML_ENABLED", "false")
    off = run_analysis(data, f"{_HDFC_STEM}.pdf")
    assert off.anomaly_report is None and "ml_anomaly" not in off.stage_names
    assert "ml_flag_count" not in summarise_result(off)

    _save(tiny_model, tmp_path)
    monkeypatch.setenv("ANOMALY_ML_ENABLED", "true")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("ANOMALY_MODEL_VERSION", VERSION)
    on = run_analysis(data, f"{_HDFC_STEM}.pdf")
    assert on.anomaly_report is not None and on.anomaly_report.flags
    assert on.stage_names.index("ml_anomaly") == on.stage_names.index("risk") + 1
    assert [s for s in on.stage_names if s != "ml_anomaly"] == off.stage_names

    # rule outputs, score and narrative are identical with and without ML flags
    key = [(f.detector_name, f.severity, f.description, f.triggering_transaction_ids)
           for f in off.risk_report.flags]
    assert [(f.detector_name, f.severity, f.description, f.triggering_transaction_ids)
            for f in on.risk_report.flags] == key
    assert on.risk_report.composite_score == off.risk_report.composite_score
    assert on.narrative == off.narrative
    assert [e.rule_id for e in on.compliance_report.exceptions] == \
           [e.rule_id for e in off.compliance_report.exceptions]
    s_on, s_off = summarise_result(on), summarise_result(off)
    assert s_on.pop("ml_flag_count") == len(on.anomaly_report.flags)
    assert s_on.pop("ml_model_version") == VERSION
    assert s_on == s_off
    ids = {t.transaction_id for t in on.doc.transactions}
    assert all(set(f.triggering_transaction_ids) <= ids for f in on.anomaly_report.flags)

    # workbench lens: ML sheet only when there are ML flags
    def wb(r: object, ar: object) -> list[str]:
        from io import BytesIO

        from openpyxl import load_workbook

        res = WorkbenchAnalysisResult(doc=r.doc, metrics=r.metrics, risk_report=r.risk_report,  # type: ignore[attr-defined]
                                      customer_analytics=r.customer_analytics,  # type: ignore[attr-defined]
                                      compliance_report=r.compliance_report,  # type: ignore[attr-defined]
                                      reconciliation_report=r.reconciliation_report,  # type: ignore[attr-defined]
                                      anomaly_report=ar)
        xlsx, _ = WorkbenchLensReport().generate(res)
        return load_workbook(BytesIO(xlsx)).sheetnames

    assert ML_SECTION_TITLE in wb(on, on.anomaly_report)
    assert ML_SECTION_TITLE not in wb(off, None)


def test_detector_failure_does_not_break_pipeline(monkeypatch: pytest.MonkeyPatch) -> None:
    """A crashing ML stage yields no ML flags; it never raises out of run_analysis."""
    if not (_GOLD / f"{_HDFC_STEM}.pdf").exists():
        pytest.skip("gold set not generated")
    from pipeline import run as run_mod

    class Boom:
        version = "boom"

        def detect(self, doc: object) -> None:
            raise RuntimeError("boom")

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(run_mod, "resolve_anomaly_detector", lambda: Boom())
    r = run_mod.run_analysis((_GOLD / f"{_HDFC_STEM}.pdf").read_bytes(), f"{_HDFC_STEM}.pdf")
    assert r.anomaly_report is None and "ml_anomaly" in r.stage_names
