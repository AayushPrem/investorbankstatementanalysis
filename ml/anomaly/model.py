"""The registered anomaly model and the per-account / pooled scoring logic.

``AnomalyModel`` is what ``ml.registry`` pickles. It holds the pooled
IsolationForest, fitted on train companies' standardised view, plus everything
needed to fit a per-account forest at scoring time:

* accounts with ``>= min_history`` rows get a per-account IsolationForest,
  fitted on that account's own raw-view rows. This is transductive over the
  statement being analysed. The features stay causal.
* shorter accounts are scored by the pooled forest.

Raw IsolationForest scores from different forests aren't on one scale, so each
row's score becomes a percentile against its scorer's reference distribution:
the account's own scores for per-account, the pooled training scores for pooled.
Flagging and severity use that percentile.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest

from ml.features.transaction_features import RAW_FEATURES, STD_FEATURES, matrix

SCORER_PER_ACCOUNT = "per_account"
SCORER_POOLED = "pooled"
_DEV_EPS = 1e-6


@dataclass
class ForestParams:
    n_estimators: int = 200
    max_samples: int = 256
    seed: int = 42

    def make(self, n_rows: int) -> IsolationForest:
        return IsolationForest(
            n_estimators=self.n_estimators,
            max_samples=min(self.max_samples, n_rows),
            random_state=self.seed,
            n_jobs=1,               # no nested parallelism (WinError 1450 lesson)
        )


@dataclass
class AnomalyModel:
    pooled: IsolationForest
    pooled_ref_quantiles: np.ndarray            # sorted anomaly scores, q = 0..1
    pooled_feature_median: np.ndarray
    pooled_feature_iqr: np.ndarray
    params: ForestParams
    min_history: int
    feature_version: str
    raw_features: tuple[str, ...] = RAW_FEATURES
    std_features: tuple[str, ...] = STD_FEATURES
    selected: dict[str, Any] = field(default_factory=dict)

    # ── scoring ────────────────────────────────────────────────────────────
    def score_account(self, feats: pd.DataFrame, force: str | None = None) -> pd.DataFrame:
        """Score one account's feature rows.

        Returns ``txn_id, score, percentile, scorer, top_features``, where score
        is the anomaly score (higher means more unusual). ``force`` can be
        ``per_account`` or ``pooled``, for ablations only.
        """
        n = len(feats)
        if n == 0:
            return pd.DataFrame(columns=["txn_id", "score", "percentile", "scorer", "top_features"])
        use_pooled = (n < self.min_history) if force is None else (force == SCORER_POOLED)
        if use_pooled:
            names = self.std_features
            x = matrix(feats, names)
            score = -self.pooled.score_samples(x)
            pct = np.searchsorted(self.pooled_ref_quantiles, score, side="right") \
                / len(self.pooled_ref_quantiles)
            med, iqr = self.pooled_feature_median, self.pooled_feature_iqr
            scorer = SCORER_POOLED
        else:
            names = self.raw_features
            x = matrix(feats, names)
            forest = self.params.make(n).fit(x)
            score = -forest.score_samples(x)
            ref = np.sort(score)
            pct = np.searchsorted(ref, score, side="right") / n
            med = np.median(x, axis=0)
            iqr = np.subtract(*np.percentile(x, [75, 25], axis=0))
            scorer = SCORER_PER_ACCOUNT
        dev = np.abs(x - med) / np.maximum(iqr, _DEV_EPS)
        dev = np.where((iqr <= _DEV_EPS) & (np.abs(x - med) <= _DEV_EPS), 0.0, dev)
        dev = np.minimum(dev, 1e6)
        tops = [top_features(names, x[i], dev[i]) for i in range(n)]
        return pd.DataFrame({
            "txn_id": feats["txn_id"].to_numpy(),
            "score": score.astype(float),
            "percentile": np.clip(pct, 0.0, 1.0).astype(float),
            "scorer": scorer,
            "top_features": tops,
        })


def top_features(names: tuple[str, ...], values: np.ndarray, dev: np.ndarray,
                 k: int = 3) -> list[dict[str, Any]]:
    """Top-k features by deviation from the scorer's reference (median / IQR)."""
    order = sorted(range(len(names)), key=lambda j: (-float(dev[j]), names[j]))[:k]
    return [{"feature": names[j], "value": round(float(values[j]), 4),
             "deviation": round(float(dev[j]), 2)} for j in order]


def fit_pooled(x_std: np.ndarray, params: ForestParams,
               n_quantiles: int = 10001) -> tuple[IsolationForest, np.ndarray, np.ndarray, np.ndarray]:
    forest = params.make(len(x_std)).fit(x_std)
    train_scores = -forest.score_samples(x_std)
    q = np.quantile(train_scores, np.linspace(0.0, 1.0, n_quantiles))
    med = np.median(x_std, axis=0)
    iqr = np.subtract(*np.percentile(x_std, [75, 25], axis=0))
    return forest, q, med, iqr


def score_all(model: AnomalyModel, feats: pd.DataFrame, force: str | None = None) -> pd.DataFrame:
    """Score every account in a feature frame (sequential; callers parallelise)."""
    parts = [model.score_account(g, force=force) for _, g in feats.groupby("account_id", sort=True)]
    if not parts:
        return pd.DataFrame(columns=["txn_id", "score", "percentile", "scorer", "top_features"])
    return pd.concat(parts, ignore_index=True)


def severity_of(pct: float, high_pct: float, medium_pct: float) -> str:
    if pct >= high_pct:
        return "HIGH"
    if pct >= medium_pct:
        return "MEDIUM"
    return "LOW"


def flag_mask(scored: pd.DataFrame, contamination: float, max_flags: int | None) -> np.ndarray:
    """Rows flagged per account: percentile >= 1 - contamination, capped at max_flags."""
    thr = 1.0 - contamination
    flagged = np.zeros(len(scored), dtype=bool)
    acct = scored["account_id"].to_numpy() if "account_id" in scored.columns else np.zeros(len(scored))
    pct = scored["percentile"].to_numpy()
    for a in pd.unique(acct):
        idx = np.flatnonzero(acct == a)
        cand = idx[pct[idx] >= thr - 1e-12]
        if max_flags is not None and len(cand) > max_flags:
            cand = cand[np.argsort(-pct[cand], kind="mergesort")[:max_flags]]
        flagged[cand] = True
    return flagged
