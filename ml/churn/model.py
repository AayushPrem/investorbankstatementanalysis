"""The churn model artifact: a fitted classifier + a Platt calibrator.

The classifier is fitted with ``class_weight="balanced"``, so its raw
probabilities are inflated for the minority class. A one-feature sigmoid
(Platt) calibrator, fitted on out-of-fold TRAIN scores, maps them back to
probabilities. Serving reports the calibrated value.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

EPS = 1e-6


def signed_log1p(x: np.ndarray) -> np.ndarray:
    """sign(x) * log1p(|x|). Module-level so the sklearn pipeline pickles."""
    return np.asarray(np.sign(x) * np.log1p(np.abs(x)), dtype=float)


def make_estimator(cand: dict[str, Any], seed: int) -> Any:
    """A fresh sklearn estimator for one candidate config (n_jobs=1 everywhere)."""
    from sklearn.ensemble import HistGradientBoostingClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import Pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler

    if cand["algo"] == "lr":
        return Pipeline([
            ("log", FunctionTransformer(signed_log1p)),
            ("scale", StandardScaler()),
            ("clf", LogisticRegression(C=float(cand["C"]), class_weight="balanced", max_iter=2000)),
        ])
    if cand["algo"] == "hgb":
        return HistGradientBoostingClassifier(
            max_depth=cand.get("max_depth"), learning_rate=float(cand["learning_rate"]),
            max_iter=int(cand["max_iter"]), max_leaf_nodes=int(cand["max_leaf_nodes"]),
            class_weight="balanced", early_stopping=False, random_state=seed,
        )
    raise ValueError(f"unknown algo {cand['algo']!r}")


def logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, EPS, 1 - EPS)
    return np.asarray(np.log(p / (1 - p)), dtype=float)


def fit_calibrator(raw: np.ndarray, y: np.ndarray) -> Any:
    from sklearn.linear_model import LogisticRegression

    cal = LogisticRegression(C=1e6, max_iter=1000)
    cal.fit(logit(raw).reshape(-1, 1), y)
    return cal


@dataclass
class ChurnModel:
    estimator: Any
    calibrator: Any
    features: tuple[str, ...]
    feature_version: str
    candidate: dict[str, Any]
    selected: dict[str, Any] = field(default_factory=dict)

    def raw_score(self, frame: pd.DataFrame) -> np.ndarray:
        x = frame[list(self.features)].to_numpy(dtype=float)
        return np.asarray(self.estimator.predict_proba(x)[:, 1], dtype=float)

    def predict_proba(self, frame: pd.DataFrame) -> np.ndarray:
        raw = self.raw_score(frame)
        return np.asarray(self.calibrator.predict_proba(logit(raw).reshape(-1, 1))[:, 1], dtype=float)
