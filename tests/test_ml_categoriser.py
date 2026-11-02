"""ml/: feature parity, registry integrity, corpus hash gate, τ selection, and a
tiny real model through Categoriser v3 (Phase 4). No corpus or artifact needed."""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")
pytest.importorskip("joblib")

from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.pipeline import Pipeline  # noqa: E402

from analysis.categoriser import Categoriser  # noqa: E402
from ml import registry  # noqa: E402
from ml.categoriser import MODEL_NAME  # noqa: E402
from ml.categoriser.data import CorpusGateError, check_gate  # noqa: E402
from ml.categoriser.evaluate import choose_taus, llm_calls_per_1k, route  # noqa: E402
from ml.categoriser.infer import CategoriserModel, resolve_model, top_k  # noqa: E402
from ml.features.categoriser import (  # noqa: E402
    FRAME_COLUMNS,
    build_frame,
    build_frame_from_transactions,
    make_column_transformer,
    normalise_description,
    rail_of,
)
from schema.canonical import (  # noqa: E402
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    ValidationStatus,
)

ROWS = [
    ("NEFT CR-HDFC0001234-ACME SOFTWARE PVT LTD-HDFCN52023112345", None, "120000.00", "REVENUE"),
    ("INF/NEFT/SBINN427493140422/ZENITH TECH", None, "88984.50", "REVENUE"),
    ("UPI/CR/9241540953/SWIGGY PAYOUT-C39C4504", None, "7642.90", "REVENUE"),
    ("NEFT DR-ICIC0989684-JYOTI CHAUHAN-SALARY 115702432126", "39000.00", None, "SALARY"),
    ("IMPS-943544725678-PARUL TIWARI-YESB-XXXXXXXX-SAL", "45000.00", None, "SALARY"),
    ("ECS/SALARY/ANKIT IYER/30354556", "52000.00", None, "SALARY"),
    ("RTGS/SBINN240142969601/NARMADA SUPPLIES", "9339.00", None, "VENDOR_PAYMENT"),
    ("MMT/IMPS/712876776200/KONARK COURIERS/INDB", "2533.33", None, "VENDOR_PAYMENT"),
    ("NEFT DR-UTIB0001-WEWORK INDIA-N2341234", "180000.00", None, "VENDOR_PAYMENT"),
]


def _corpus_frame(n_copies: int = 8) -> tuple[pd.DataFrame, np.ndarray]:
    recs, y = [], []
    for k in range(n_copies):
        for desc, dr, cr, cat in ROWS:
            recs.append({"description": desc.replace("2", str(k % 10)), "debit": dr, "credit": cr,
                         "date": date(2024, 4, 1 + k)})
            y.append(cat)
    return pd.DataFrame(recs), np.array(y)


def _txns(df: pd.DataFrame) -> list[CanonicalTransaction]:
    return [CanonicalTransaction(
        transaction_id=f"t{i}", date=r.date, description=r.description,
        debit=None if pd.isna(r.debit) else Decimal(r.debit),
        credit=None if pd.isna(r.credit) else Decimal(r.credit),
        balance=Decimal("0"), source_reference=SourceReference(file_path="x", page=0, row=i, raw_text=""),
    ) for i, r in enumerate(df.itertuples(index=False))]


@pytest.fixture(scope="module")
def tiny_pipeline() -> Pipeline:
    df, y = _corpus_frame()
    pipe = Pipeline([("features", make_column_transformer(min_df=1)),
                     ("clf", LogisticRegression(C=10.0, max_iter=500))])
    pipe.fit(build_frame(df), y)
    return pipe


# ── features ───────────────────────────────────────────────────────────────

def test_normalisation_masks_refs_vpa_ifsc() -> None:
    s = normalise_description("UPI/351334361744/AGASTYA BROKERS/agastya749@okhdfcbank/PAYME")
    assert "351334361744" not in s and "<num>" in s and "<vpa>" in s and "agastya brokers" in s
    s = normalise_description("NEFT CR-HDFC0001234-ACME SOFTWARE PVT LTD")
    assert "<ifsc>" in s and "acme software pvt ltd" in s
    assert normalise_description("IMPS-1-X-YESB-XXXXXXXX") .endswith("<acct>")


@pytest.mark.parametrize("desc,rail", [
    ("UPI/CR/1/X", "UPI"), ("RTGS CR-X", "RTGS"), ("NEFT DR-X", "NEFT"), ("MMT/IMPS/1/X", "IMPS"),
    ("ECS/SALARY/X", "ECS"), ("CHQ DEP 1234", "CHQ"), ("CASH DEP MAIN", "CASH"), ("BANK CHARGES", "OTHER"),
])
def test_rail_of(desc: str, rail: str) -> None:
    assert rail_of(desc) == rail


def test_train_and_serve_frames_identical() -> None:
    df, _ = _corpus_frame(2)
    a, b = build_frame(df), build_frame_from_transactions(_txns(df))
    assert list(a.columns) == list(FRAME_COLUMNS)
    pd.testing.assert_frame_equal(a, b)


def test_no_leaky_columns_in_frame() -> None:
    assert not {"bank", "balance", "ref_no", "txn_time", "company_id"} & set(FRAME_COLUMNS)


# ── registry ───────────────────────────────────────────────────────────────

def test_registry_roundtrip_and_sha_check(tmp_path: Path, tiny_pipeline: Pipeline) -> None:
    d = registry.save(tiny_pipeline, name=MODEL_NAME, version="v-test", root=tmp_path,
                      metadata={"corpus": {"corpus_hash": "abc"}, "split": "company", "params": {"C": 10}})
    meta = json.loads((d / "metadata.json").read_text(encoding="utf-8"))
    for k in ("git_sha", "git_dirty", "created_at", "libraries", "artifact_sha256", "split", "params", "corpus"):
        assert k in meta
    assert meta["libraries"]["sklearn"]
    loaded = registry.load(MODEL_NAME, "v-test", root=tmp_path)
    assert loaded.version == "v-test"

    (d / "model.joblib").write_bytes((d / "model.joblib").read_bytes() + b"tamper")
    with pytest.raises(registry.RegistryError, match="sha256"):
        registry.load(MODEL_NAME, "v-test", root=tmp_path)


@pytest.mark.parametrize("bad", ["../x", "a/b", "..", "", "c:evil"])
def test_registry_rejects_path_components(tmp_path: Path, bad: str) -> None:
    with pytest.raises(registry.RegistryError):
        registry.model_dir(MODEL_NAME, bad, tmp_path)


def test_resolve_model_from_settings(tmp_path: Path, tiny_pipeline: Pipeline,
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    registry.save(tiny_pipeline, name=MODEL_NAME, version="v-env", root=tmp_path, metadata={})
    monkeypatch.setenv("CATEGORISER_MODEL_VERSION", "v-env")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    m = resolve_model()
    assert m is not None and m.version == "v-env"
    monkeypatch.setenv("CATEGORISER_MODEL_VERSION", "v-missing")
    assert resolve_model() is None


# ── corpus gate ────────────────────────────────────────────────────────────

def _fake_corpus(tmp_path: Path, name: str, ch: str, sh: str) -> Path:
    d = tmp_path / name
    (d / "splits").mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"corpus_hash": ch, "corpus_version": "v1"}))
    (d / "splits" / "manifest.json").write_text(json.dumps({"splits_hash": sh}))
    return d


def test_gate_refuses_hash_mismatch(tmp_path: Path) -> None:
    cfg = {"pinned_corpora": {"v1": {"corpus_hash": "aa", "splits_hash": "bb"}}}
    assert check_gate(_fake_corpus(tmp_path, "v1", "aa", "bb"), cfg)["pinned"] is True
    with pytest.raises(CorpusGateError, match="corpus_hash"):
        check_gate(_fake_corpus(tmp_path / "x", "v1", "zz", "bb"), cfg)
    with pytest.raises(CorpusGateError, match="splits_hash"):
        check_gate(_fake_corpus(tmp_path / "y", "v1", "aa", "zz"), cfg)


def test_gate_unpinned_needs_flag(tmp_path: Path) -> None:
    d = _fake_corpus(tmp_path, "v1-small", "aa", "bb")
    with pytest.raises(CorpusGateError, match="allow-unpinned"):
        check_gate(d, {"pinned_corpora": {}})
    assert check_gate(d, {"pinned_corpora": {}}, allow_unpinned=True)["pinned"] is False


# ── τ selection / routing simulation ──────────────────────────────────────

RCFG = {"target_accept_precision": 0.97, "target_band_precision": 0.90,
        "tau_grid": {"start": 0.30, "stop": 0.99, "step": 0.01}}


def test_choose_taus_lowest_tau_meeting_targets() -> None:
    # 100 rows at p=0.95 all correct; 10 rows at p=0.6 with 9 correct; 10 at 0.35, 2 correct.
    p = np.array([0.95] * 100 + [0.6] * 10 + [0.35] * 10)
    c = np.array([True] * 100 + [True] * 9 + [False] + [True] * 2 + [False] * 8)
    t = choose_taus(p, c, RCFG)
    # precision at τ≤0.35 = 111/120 = 0.925 < 0.97; at τ ∈ (0.35, 0.6]: 109/110 = 0.991 ≥ 0.97
    assert t["tau_accept"] == pytest.approx(0.36)
    assert t["tau_review"] == pytest.approx(0.36)  # band [τ_r, 0.36) only holds the 0.35 rows (20%)


def test_choose_taus_band() -> None:
    p = np.array([0.99] * 100 + [0.5] * 10)
    c = np.array([True] * 100 + [True] * 9 + [False])
    t = choose_taus(p, c, {**RCFG, "target_accept_precision": 0.995})
    assert t["tau_accept"] == pytest.approx(0.51)
    assert t["tau_review"] == pytest.approx(0.0)   # band [0, 0.51) precision 0.9 ≥ 0.9


def test_route_and_llm_calls() -> None:
    src = route(np.array([True, False, False, False]), np.array([0.1, 0.99, 0.6, 0.2]), 0.9, 0.5)
    assert src.tolist() == ["rule", "ml", "ml_degraded", "review"]
    calls = llm_calls_per_1k(src, np.array(["a", "a", "a", "b"]), batch=50)
    assert calls["llm_rows_per_1k"] == pytest.approx(500.0)
    assert calls["llm_api_calls_per_1k"] == pytest.approx(500.0)   # 2 calls (a, b) per 4 rows


# ── a real (tiny) model through the categoriser ─────────────────────────────

def test_top_k_order() -> None:
    assert top_k(np.array([0.1, 0.6, 0.3]), ["A", "B", "C"], 2) == [("B", 0.6), ("C", 0.3)]


def test_v3_with_real_model_is_deterministic(tiny_pipeline: Pipeline) -> None:
    model = CategoriserModel(tiny_pipeline, {"model_version": "tiny"})
    df, y = _corpus_frame(1)

    def run() -> list[tuple[str, str, float]]:
        txns = _txns(df)
        doc = StatementDocument(account_id="1", statement_period_start=date(2024, 4, 1),
                                statement_period_end=date(2024, 4, 30), source_format="t",
                                validation_status=ValidationStatus.UNVALIDATED, transactions=txns)
        _, stats = Categoriser(llm_enabled=False, model=model, tau_accept=0.5, tau_review=0.2,
                               high_precision_rules=()).categorise(doc)
        assert stats.model_version == "tiny"
        return [(str(t.category), str(t.category_source), round(float(t.category_confidence or 0), 6))
                for t in doc.transactions]

    first, second = run(), run()
    assert first == second
    assert [c for c, _, _ in first] == y.tolist()
