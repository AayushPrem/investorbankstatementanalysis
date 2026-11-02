"""Metric helpers shared by train.py and evaluate.py."""
from __future__ import annotations

from typing import Any

import numpy as np
from sklearn.metrics import accuracy_score, f1_score, precision_recall_fscore_support

from schema.canonical import TransactionCategory

ALL_CLASSES = [c.value for c in TransactionCategory]


def supported_labels(y_true: np.ndarray) -> list[str]:
    """Classes with ≥1 true row. LOAN_OUT has none in corpus v1 → excluded (n/a)."""
    present = set(np.unique(y_true).tolist())
    return [c for c in ALL_CLASSES if c in present]


def macro_f1(y_true: np.ndarray, y_pred: np.ndarray) -> float:
    return float(f1_score(y_true, y_pred, labels=supported_labels(y_true), average="macro",
                          zero_division=0))


def summary(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, Any]:
    labels = supported_labels(y_true)
    p, r, f, _ = precision_recall_fscore_support(y_true, y_pred, labels=labels,
                                                 average="macro", zero_division=0)
    return {
        "n": int(len(y_true)),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_precision": float(p), "macro_recall": float(r), "macro_f1": float(f),
        "macro_over": labels,
        "not_evaluable": [c for c in ALL_CLASSES if c not in labels],
    }


def per_class(y_true: np.ndarray, y_pred: np.ndarray) -> list[dict[str, Any]]:
    p, r, f, s = precision_recall_fscore_support(y_true, y_pred, labels=ALL_CLASSES,
                                                 zero_division=0)
    out = []
    for i, c in enumerate(ALL_CLASSES):
        sup = int(s[i])
        out.append({"class": c, "support": sup,
                    "precision": float(p[i]) if sup or (y_pred == c).any() else None,
                    "recall": float(r[i]) if sup else None,
                    "f1": float(f[i]) if sup else None})
    return out


def expected_calibration_error(proba: np.ndarray, y_idx: np.ndarray, n_bins: int = 15) -> float:
    """Top-label ECE."""
    conf = proba.max(axis=1)
    correct = (proba.argmax(axis=1) == y_idx).astype(float)
    bins = np.linspace(0, 1, n_bins + 1)
    ece = 0.0
    for lo, hi in zip(bins[:-1], bins[1:], strict=True):
        m = (conf > lo) & (conf <= hi)
        if m.any():
            ece += m.mean() * abs(correct[m].mean() - conf[m].mean())
    return float(ece)
