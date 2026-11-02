"""Ranking and set metrics for anomaly evaluation.

precision@k / recall@k are computed per company (the analyst reviews the
top k rows of one statement):

* macro: mean over companies. Recall skips companies with no positive rows.
* micro ("pooled"): Σhits / Σk and Σhits / Σpositives over companies.

Ties in score are broken by txn_id so the result is deterministic.
"""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd


def _ranked(df: pd.DataFrame, score_col: str) -> pd.DataFrame:
    return df.sort_values(["company_id", score_col, "txn_id"],
                          ascending=[True, False, True], kind="mergesort")


def at_k(df: pd.DataFrame, score_col: str, label_col: str, ks: Sequence[int]) -> dict[str, Any]:
    """P@k and R@k (macro + micro) for one label column."""
    r = _ranked(df, score_col)
    out: dict[str, Any] = {"n_companies": int(r.company_id.nunique()),
                           "n_positive": int(r[label_col].sum())}
    pos = r.groupby("company_id", sort=True)[label_col].sum()
    rank = r.groupby("company_id", sort=False).cumcount()
    for k in ks:
        hits = r[rank < k].groupby("company_id", sort=True)[label_col].sum().reindex(pos.index, fill_value=0)
        has_pos = pos > 0
        out[f"p@{k}_macro"] = float((hits / k).mean())
        out[f"r@{k}_macro"] = float((hits[has_pos] / pos[has_pos]).mean()) if has_pos.any() else None
        out[f"p@{k}_micro"] = float(hits.sum() / (k * len(pos)))
        out[f"r@{k}_micro"] = float(hits.sum() / pos.sum()) if pos.sum() else None
        out[f"hits@{k}"] = int(hits.sum())
    return out


def average_precision(scores: np.ndarray, labels: np.ndarray) -> float:
    """AP over a single ranking (all rows pooled)."""
    if labels.sum() == 0:
        return float("nan")
    order = np.argsort(-scores, kind="mergesort")
    y = labels[order].astype(float)
    cum = np.cumsum(y)
    prec = cum / np.arange(1, len(y) + 1)
    return float((prec * y).sum() / y.sum())


def set_metrics(flagged: np.ndarray, labels: np.ndarray) -> dict[str, Any]:
    tp = int((flagged & labels).sum())
    fp = int((flagged & ~labels).sum())
    fn = int((~flagged & labels).sum())
    p = tp / (tp + fp) if tp + fp else 0.0
    rc = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * p * rc / (p + rc) if p + rc else 0.0
    return {"flagged": int(flagged.sum()), "tp": tp, "fp": fp, "fn": fn,
            "precision": p, "recall": rc, "f1": f1}


def top_k_mask(df: pd.DataFrame, score_col: str, k: int) -> pd.Series:
    """Boolean mask (aligned to df.index) of each company's top-k rows."""
    r = _ranked(df, score_col)
    rank = r.groupby("company_id", sort=False).cumcount()
    return pd.Series(rank.to_numpy() < k, index=r.index).reindex(df.index)
