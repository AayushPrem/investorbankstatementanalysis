"""Company-level features for segmentation (Phase 6, docs/ml/segmentation.md).

Computed from pipeline outputs only: a StatementDocument with PREDICTED
categories and the FinancialAnalyst's metrics. The same function serves
training (corpus companies) and the Network Lens (uploaded statements). No
label column is read anywhere.

Customer concentration and count use the narration counterparty key (the
churn identity, docs/ml/churn.md), not the resolver's customer_id, which
fragments corpus narrations.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from typing import Any

import numpy as np
import pandas as pd

from ml.features.categoriser import rail_of
from ml.features.churn_features import payment_key
from pipeline.customer_identity import matched_aggregator

FEATURE_VERSION = "1"
RUNWAY_CAP_MONTHS = 60.0
BURN_RATIO_CAP = 100.0
GROWTH_CLIP = 10.0
RAIL_FEATURES = ("UPI", "NEFT", "RTGS", "IMPS", "CASH")

FEATURES: tuple[str, ...] = (
    "rev_growth",          # avg MoM revenue growth (signed log1p after clipping)
    "burn_to_revenue",     # avg monthly burn / avg monthly revenue (log1p)
    "runway",              # months, capped at 60; profitable -> cap (log1p)
    "customer_hhi",        # revenue HHI over narration-key customers (0..1)
    "customer_count",      # keys with >= 2 payments (log1p)
    "revenue_cv",          # CV of monthly revenue (log1p)
    "founder_share",       # predicted FOUNDER_WITHDRAWAL debits / all debits
    *(f"rail_{r.lower()}" for r in RAIL_FEATURES),   # share of rows per rail
    "aggregator_share",    # aggregator-settled share of REVENUE credits
)
LOG_FEATURES = frozenset({"burn_to_revenue", "runway", "customer_count", "revenue_cv"})


def _cat(t: Any) -> str | None:
    return None if t.category is None else str(getattr(t.category, "value", t.category))


def raw_features(doc: Any, metrics: Any) -> dict[str, float]:
    """Untransformed feature values for one company (interpretable units)."""
    rev = float(metrics.avg_monthly_revenue)
    burn = float(metrics.avg_monthly_burn)
    growth = float(metrics.avg_mom_growth) if metrics.avg_mom_growth is not None else 0.0
    runway = RUNWAY_CAP_MONTHS if metrics.runway_months is None else \
        min(max(float(metrics.runway_months), 0.0), RUNWAY_CAP_MONTHS)
    monthly = [float(m.revenue) for m in metrics.monthly_stats]
    mean_m = statistics.fmean(monthly) if monthly else 0.0
    cv = statistics.pstdev(monthly) / mean_m if mean_m > 0 and len(monthly) > 1 else 0.0

    key_rev: dict[str, float] = {}
    key_n: Counter[str] = Counter()
    rev_total = agg_total = debit_total = founder_total = 0.0
    rails: Counter[str] = Counter()
    for t in doc.transactions:
        rails[rail_of(t.description or "")] += 1
        cat = _cat(t)
        if t.debit is not None:
            d = float(t.debit)
            debit_total += d
            if cat == "FOUNDER_WITHDRAWAL":
                founder_total += d
        if cat == "REVENUE" and t.credit is not None and float(t.credit) > 0:
            c = float(t.credit)
            rev_total += c
            if matched_aggregator(t.description or ""):
                agg_total += c
                continue
            k = payment_key(t.description or "")
            if k is not None:
                key_rev[k] = key_rev.get(k, 0.0) + c
                key_n[k] += 1
    cust_total = sum(key_rev.values())
    hhi = sum((v / cust_total) ** 2 for v in key_rev.values()) if cust_total > 0 else 0.0
    n_rows = max(len(doc.transactions), 1)
    out = {
        "rev_growth": growth,
        "burn_to_revenue": min(burn / rev, BURN_RATIO_CAP) if rev > 0 else BURN_RATIO_CAP,
        "runway": runway,
        "customer_hhi": hhi,
        "customer_count": float(sum(1 for n in key_n.values() if n >= 2)),
        "revenue_cv": cv,
        "founder_share": founder_total / debit_total if debit_total > 0 else 0.0,
        **{f"rail_{r.lower()}": rails[r] / n_rows for r in RAIL_FEATURES},
        "aggregator_share": agg_total / rev_total if rev_total > 0 else 0.0,
    }
    return {k: (0.0 if not math.isfinite(v) else float(v)) for k, v in out.items()}


def transform(frame: pd.DataFrame) -> np.ndarray:
    """Log transforms before scaling (the scaler is fitted in ml/segmentation)."""
    cols = []
    for f in FEATURES:
        x = frame[f].to_numpy(dtype=float)
        if f == "rev_growth":
            x = np.clip(x, -GROWTH_CLIP, GROWTH_CLIP)
            x = np.sign(x) * np.log1p(np.abs(x))
        elif f in LOG_FEATURES:
            x = np.log1p(np.maximum(x, 0.0))
        cols.append(x)
    return np.column_stack(cols)
