"""Segmentation artifact, clustering candidates and bootstrap (Hennig) stability."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from ml.features.company_features import FEATURES, transform

PHRASES = {
    "rev_growth": "revenue growth",
    "burn_to_revenue": "burn relative to revenue",
    "runway": "runway",
    "customer_hhi": "customer concentration",
    "customer_count": "customer count",
    "revenue_cv": "revenue volatility",
    "founder_share": "founder withdrawals",
    "rail_upi": "UPI share",
    "rail_neft": "NEFT share",
    "rail_rtgs": "RTGS share",
    "rail_imps": "IMPS share",
    "rail_cash": "cash share",
    "aggregator_share": "aggregator-settled revenue",
}


def fit_labels(algo: str, x: np.ndarray, k: int, seed: int, n_init: int = 10) -> tuple[Any, np.ndarray]:
    """Fit one clustering; returns (estimator, labels). Single-threaded estimators only."""
    from sklearn.cluster import AgglomerativeClustering, KMeans
    from sklearn.mixture import GaussianMixture

    if algo == "kmeans":
        est = KMeans(n_clusters=k, n_init=n_init, random_state=seed)
        return est, est.fit_predict(x)
    if algo == "gmm":
        est = GaussianMixture(n_components=k, covariance_type="full", random_state=seed, reg_covar=1e-4)
        est.fit(x)
        return est, est.predict(x)
    if algo == "agglomerative":
        est = AgglomerativeClustering(n_clusters=k, linkage="ward")
        return est, est.fit_predict(x)
    raise ValueError(f"unknown algo {algo!r}")


def bootstrap_jaccard(x: np.ndarray, labels: np.ndarray, refit: Callable[[np.ndarray, int], np.ndarray],
                      n_boot: int, seed: int) -> dict[int, float]:
    """Hennig (2007) clusterboot: mean over resamples of the best Jaccard per original cluster."""
    rng = np.random.default_rng(seed)
    clusters = sorted(set(labels.tolist()))
    acc: dict[int, list[float]] = {c: [] for c in clusters}
    n = len(x)
    for b in range(n_boot):
        idx = rng.integers(0, n, n)
        lab_b = refit(x[idx], b)
        uniq, first = np.unique(idx, return_index=True)
        lb = lab_b[first]
        orig = labels[uniq]
        for c in clusters:
            in_c = orig == c
            if not in_c.any():
                continue
            best = 0.0
            for d in set(lb.tolist()):
                in_d = lb == d
                inter = int((in_c & in_d).sum())
                if inter:
                    best = max(best, inter / int((in_c | in_d).sum()))
            acc[c].append(best)
    return {c: float(np.mean(v)) if v else float("nan") for c, v in acc.items()}


def centroids_of(x: np.ndarray, labels: np.ndarray, k: int) -> np.ndarray:
    return np.vstack([x[labels == c].mean(axis=0) if (labels == c).any() else np.zeros(x.shape[1])
                      for c in range(k)])


def name_cluster(z: np.ndarray, top: int = 2) -> str:
    order = np.argsort(-np.abs(z), kind="mergesort")[:top]
    return ", ".join(f"{'high' if z[i] > 0 else 'low'} {PHRASES[FEATURES[i]]}" for i in order)


@dataclass
class SegmentationModel:
    algo: str
    k: int
    scaler: Any
    estimator: Any
    centroids: np.ndarray                  # scaled space, k x d (from the fit set)
    pca: Any
    stability: dict[int, float]
    names: dict[int, str]
    stable: dict[int, bool]
    feature_version: str
    features: tuple[str, ...] = FEATURES
    selected: dict[str, Any] = field(default_factory=dict)

    def scale(self, frame: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.scaler.transform(transform(frame[list(self.features)])), dtype=float)

    def predict_scaled(self, x: np.ndarray) -> np.ndarray:
        if self.algo in ("kmeans", "gmm"):
            return np.asarray(self.estimator.predict(x), dtype=int)
        d = ((x[:, None, :] - self.centroids[None, :, :]) ** 2).sum(axis=2)
        return np.asarray(d.argmin(axis=1), dtype=int)

    def predict(self, frame: pd.DataFrame) -> np.ndarray:
        return self.predict_scaled(self.scale(frame))

    def project(self, frame: pd.DataFrame) -> np.ndarray:
        return np.asarray(self.pca.transform(self.scale(frame)), dtype=float)
