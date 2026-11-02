"""Phase 6: company features, clustering selection, stability and Network Lens integration."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("sklearn")

from ml import registry  # noqa: E402
from ml.features.company_features import FEATURES, raw_features, transform  # noqa: E402
from ml.segmentation import MODEL_NAME  # noqa: E402
from ml.segmentation.infer import assign, resolve_model  # noqa: E402
from ml.segmentation.model import (  # noqa: E402
    SegmentationModel,
    bootstrap_jaccard,
    centroids_of,
    fit_labels,
    name_cluster,
)
from ml.segmentation.train import select  # noqa: E402
from schema.canonical import (  # noqa: E402
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)

VERSION = "seg1-test-000000"
CFG = {"seed": 0, "kmeans_n_init": 5, "stable_jaccard": 0.75}
CLIENTS = ["ACME SOFTWARE", "ORBIT LOGISTICS", "ZEN RETAIL", "HELIX LABS", "NOVA FOODS"]


def _blobs(n: int = 60, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    centres = np.zeros((3, len(FEATURES)))
    centres[1, 0], centres[2, 1] = 8.0, 8.0
    return np.vstack([c + rng.normal(0, 0.5, (n, len(FEATURES))) for c in centres])


def _doc(aggregator: bool) -> StatementDocument:
    txns = []
    d0 = date(2024, 1, 1)
    for i in range(120):
        if i % 3 == 0:
            desc = "RAZORPAY SETTLEMENT RZP1" if aggregator else f"NEFT CR-HDFC0001-{CLIENTS[i % 5]} LTD-INV{i}"
            txns.append(dict(description=desc, credit=Decimal("50000.00"), cat=TransactionCategory.REVENUE))
        elif i % 3 == 1:
            txns.append(dict(description=f"UPI/1234/VENDOR {i % 4}/x@ybl", debit=Decimal("12000.50"),
                             cat=TransactionCategory.VENDOR_PAYMENT))
        else:
            txns.append(dict(description="IMPS-FOUNDER DRAW", debit=Decimal("3000.00"),
                             cat=TransactionCategory.FOUNDER_WITHDRAWAL))
    out = [CanonicalTransaction(transaction_id=f"t{i}", date=d0 + timedelta(days=3 * i),
                                description=t["description"], credit=t.get("credit"), debit=t.get("debit"),
                                balance=Decimal("100000"), category=t["cat"],
                                source_reference=SourceReference(file_path="t", page=0, row=i, raw_text=""))
           for i, t in enumerate(txns)]
    return StatementDocument(account_id="a", statement_period_start=out[0].date,
                             statement_period_end=out[-1].date, source_format="t", transactions=out)


def test_raw_features_from_pipeline_outputs() -> None:
    from analysis.financial_analyst import FinancialAnalyst

    b2b, agg = _doc(False), _doc(True)
    fb = raw_features(b2b, FinancialAnalyst().analyse(b2b))
    fa = raw_features(agg, FinancialAnalyst().analyse(agg))
    assert set(fb) == set(FEATURES) and all(np.isfinite(list(fb.values())))
    assert fb["aggregator_share"] == 0.0 and fa["aggregator_share"] == 1.0
    assert fb["customer_count"] == 5.0 and fa["customer_count"] == 0.0
    assert fb["customer_hhi"] == pytest.approx(0.2, abs=0.02)
    assert 0 < fb["founder_share"] < 1
    x = transform(pd.DataFrame([fb, fa]))
    assert x.shape == (2, len(FEATURES)) and np.isfinite(x).all()


def test_stability_high_for_real_clusters_low_for_noise() -> None:
    x = _blobs()
    _, lab = fit_labels("kmeans", x, 3, 0)
    j = bootstrap_jaccard(x, lab, lambda xb, b: fit_labels("kmeans", xb, 3, b)[1], 20, 0)
    assert min(j.values()) > 0.95
    noise = np.random.default_rng(1).normal(size=(180, len(FEATURES)))
    _, lab_n = fit_labels("kmeans", noise, 6, 0)
    jn = bootstrap_jaccard(noise, lab_n, lambda xb, b: fit_labels("kmeans", xb, 6, b)[1], 20, 0)
    assert np.mean(list(jn.values())) < min(j.values())


def test_selection_recovers_k_without_labels() -> None:
    sel = select(_blobs(), CFG, [2, 5], n_boot=10)
    assert sel["k_star"] == 3 and not sel["final_fallback"]
    assert sel["final"]["k"] == 3 and sel["final"]["mean_jaccard"] >= 0.75
    assert {c["algo"] for c in sel["candidates"]} == {"kmeans", "gmm", "agglomerative"}


def _tiny_model() -> SegmentationModel:
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    rng = np.random.default_rng(0)
    rows = pd.DataFrame(np.abs(rng.normal(0.3, 0.2, (90, len(FEATURES)))), columns=list(FEATURES))
    rows.loc[30:59, "aggregator_share"] = 0.95
    rows.loc[60:, "rail_cash"] = 0.9
    scaler = StandardScaler().fit(transform(rows))
    x = scaler.transform(transform(rows))
    est, lab = fit_labels("kmeans", x, 3, 0)
    cents = centroids_of(x, lab, 3)
    return SegmentationModel(algo="kmeans", k=3, scaler=scaler, estimator=est, centroids=cents,
                             pca=PCA(2).fit(x), stability={0: 1.0, 1: 1.0, 2: 0.5},
                             names={0: name_cluster(cents[0]), 1: name_cluster(cents[1]), 2: "Segment 2 (unstable)"},
                             stable={0: True, 1: True, 2: False}, feature_version="1")


def test_assign_peers_and_agglomerative_nearest_centroid() -> None:
    m = _tiny_model()
    rows = [dict.fromkeys(FEATURES, 0.3) | {"aggregator_share": a} for a in (0.0, 0.05, 0.95, 0.9)]
    out = assign(m, rows, n_peers=2)
    assert len(out) == 4
    for i, a in enumerate(out):
        assert i not in [j for j, _ in a.peers] and len(a.peers) == 2
        assert [d for _, d in a.peers] == sorted(d for _, d in a.peers)
    assert out[0].peers[0][0] == 1 and out[2].peers[0][0] == 3
    assert out[0].segment != out[2].segment
    agg = SegmentationModel(**{**m.__dict__, "algo": "agglomerative"})
    np.testing.assert_array_equal(agg.predict(pd.DataFrame(rows)), m.predict(pd.DataFrame(rows)))


def test_network_lens_segments_only_with_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openpyxl import load_workbook

    from analysis.financial_analyst import FinancialAnalyst
    from reports import network_lens as nl
    from tests.test_network_lens import _customer_analytics, _risk_report

    docs = {"Acme": _doc(False), "Beta": _doc(True), "Gamma": _doc(False)}

    def summaries() -> list[nl.CompanySummary]:
        return [nl.build_company_summary(n, d, FinancialAnalyst().analyse(d), _risk_report(),
                                         _customer_analytics()) for n, d in docs.items()]

    monkeypatch.setenv("SEGMENTATION_ML_ENABLED", "false")
    off = summaries()
    xo, _ = nl.NetworkLensReport().generate(off, llm_enabled=False)
    assert all(s.segment_features is None and s.segment is None for s in off)
    assert "Segments" not in load_workbook(BytesIO(xo)).sheetnames

    registry.save(_tiny_model(), name=MODEL_NAME, version=VERSION, metadata={"synthetic": True}, root=tmp_path)
    monkeypatch.setenv("SEGMENTATION_ML_ENABLED", "true")
    monkeypatch.setenv("MODELS_DIR", str(tmp_path))
    monkeypatch.setenv("SEGMENTATION_MODEL_VERSION", VERSION)
    assert resolve_model() is not None
    on = summaries()
    xn, pdf = nl.NetworkLensReport().generate(on, llm_enabled=False)
    wb = load_workbook(BytesIO(xn))
    assert wb.sheetnames[-1] == "Segments" and wb.sheetnames[:-1] == load_workbook(BytesIO(xo)).sheetnames
    assert all(s.segment is not None and len(s.segment.peers) == 2 for s in on)
    assert on[0].segment and on[0].segment.peers[0] == "Gamma"          # the other B2B company
    assert pdf.startswith(b"%PDF")
    # comparison sheet content is identical with and without segments
    rows = lambda b: [[c.value for c in r] for r in load_workbook(BytesIO(b))["Comparison"].iter_rows()]  # noqa: E731
    assert rows(xn) == rows(xo)
