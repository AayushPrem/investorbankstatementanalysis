"""Churn metrics with company-cluster bootstrap confidence intervals."""
from __future__ import annotations

import math
from collections.abc import Callable
from typing import Any

import numpy as np
import pandas as pd


def pr_auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import average_precision_score

    if len(y) == 0 or y.min() == y.max():
        return float("nan")
    return float(average_precision_score(y, s))


def roc_auc(y: np.ndarray, s: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    if len(y) == 0 or y.min() == y.max():
        return float("nan")
    return float(roc_auc_score(y, s))


def prf(y: np.ndarray, pred: np.ndarray) -> dict[str, float]:
    tp = int(((pred == 1) & (y == 1)).sum())
    fp = int(((pred == 1) & (y == 0)).sum())
    fn = int(((pred == 0) & (y == 1)).sum())
    p = tp / (tp + fp) if tp + fp else float("nan")
    r = tp / (tp + fn) if tp + fn else float("nan")
    f1 = 2 * p * r / (p + r) if (p == p and r == r and p + r > 0) else 0.0
    return {"precision": p, "recall": r, "f1": f1, "flagged": tp + fp}


def best_f1_threshold(y: np.ndarray, s: np.ndarray) -> tuple[float, float]:
    """(threshold, f1) maximising F1; ties -> higher threshold."""
    from sklearn.metrics import precision_recall_curve

    p, r, t = precision_recall_curve(y, s)
    f1 = np.where(p[:-1] + r[:-1] > 0, 2 * p[:-1] * r[:-1] / np.maximum(p[:-1] + r[:-1], 1e-12), 0.0)
    best = np.flatnonzero(f1 == f1.max())[-1]
    return float(t[best]), float(f1[best])


def precision_at_top(frame: pd.DataFrame, score: str, frac: float,
                     group: tuple[str, ...] = ("company_id", "cutoff_date")) -> dict[str, float]:
    """Precision of the top ``frac`` units, per statement (micro) and globally."""
    picked = []
    for _, g in frame.groupby(list(group), sort=False):
        k = max(1, math.ceil(frac * len(g)))
        picked.append(g.nlargest(k, score, keep="first")["y"].to_numpy())
    per_stmt = float(np.concatenate(picked).mean()) if picked else float("nan")
    k_all = max(1, math.ceil(frac * len(frame)))
    glob = float(frame.nlargest(k_all, score, keep="first")["y"].mean()) if len(frame) else float("nan")
    return {"p_top_per_statement": per_stmt, "p_top_global": glob}


def brier(y: np.ndarray, p: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2)) if len(y) else float("nan")


def bootstrap_ci(frame: pd.DataFrame, stat: Callable[[pd.DataFrame], float], n: int,
                 seed: int = 0, group: str = "company_id") -> dict[str, Any]:
    """Point estimate + 95% percentile CI, resampling companies with replacement."""
    point = stat(frame)
    frame = frame.reset_index(drop=True)
    idx = [np.asarray(v) for _, v in sorted(frame.groupby(group, sort=True).indices.items())]
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n):
        pick = rng.integers(0, len(idx), len(idx))
        v = stat(frame.iloc[np.concatenate([idx[i] for i in pick])])
        if v == v:
            vals.append(v)
    if not vals:
        return {"value": point, "lo": float("nan"), "hi": float("nan"), "n_boot": 0}
    lo, hi = np.percentile(vals, [2.5, 97.5])
    return {"value": point, "lo": float(lo), "hi": float(hi), "n_boot": len(vals)}


def calibration_table(y: np.ndarray, p: np.ndarray, bins: int) -> list[dict[str, float]]:
    edges = np.linspace(0, 1, bins + 1)
    idx = np.clip(np.digitize(p, edges[1:-1]), 0, bins - 1)
    out = []
    for b in range(bins):
        m = idx == b
        if m.any():
            out.append({"bin": b, "lo": float(edges[b]), "hi": float(edges[b + 1]), "n": int(m.sum()),
                        "mean_pred": float(p[m].mean()), "frac_pos": float(y[m].mean())})
    return out
