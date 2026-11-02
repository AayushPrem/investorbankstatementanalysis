"""Customer-churn features and labels (Phase 6, docs/ml/churn.md).

Unit: (company, customer key, cutoff). The customer key is the Phase 5
narration counterparty key (:func:`counterparty_from_narration`) of a
REVENUE credit that isn't an aggregator settlement. REVENUE is the
*predicted* category (categoriser v3, LLM off); labels are never read here.

Label (verbatim in the model card): customer makes no payment in
(cutoff, cutoff+90d] given >=2 payments before cutoff.

No look-ahead: :func:`features_at` slices ``date <= cutoff`` *first* and
everything after that line sees only the slice. :func:`label_at` is the only
function that reads rows after the cutoff. tests/test_churn_features.py proves
that changing, deleting or adding rows after the cutoff changes no feature.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, timedelta
from typing import Any

import numpy as np
import pandas as pd

from ml.features.transaction_features import counterparty_from_narration
from pipeline.customer_identity import matched_aggregator

FEATURE_VERSION = "1"
HORIZON_DAYS = 90
ACTIVE_WINDOW_DAYS = 90
MIN_PAYMENTS = 2
LATE_GAP_MULTIPLE = 1.25
LATE_LAST_GAPS = 6
REVENUE = "REVENUE"

FEATURES: tuple[str, ...] = (
    "tenure_days",
    "n_payments",
    "gap_mean",
    "gap_cv",
    "days_since_last",
    "recency_ratio",          # days_since_last / median inter-payment gap
    "amount_slope_last3",     # slope of log amount over the last 3 payments
    "amount_last_vs_mean",    # log(last amount / mean amount)
    "revenue_share",          # key's share of company revenue, trailing 365 days
    "late_ratio",             # share of the last 6 gaps > 1.25 x median gap
    "payments_90d",
    "company_active_keys",    # keys with a payment in the last 90 days (context)
    "company_rev_trend",      # log(revenue last 90d / revenue the 90d before) (context)
)
PAYMENT_COLUMNS = ("company_id", "date", "amount", "key")


def payment_key(description: str) -> str | None:
    """The customer key of a REVENUE credit, or None if it's not a customer payment."""
    if matched_aggregator(description or ""):
        return None
    key = counterparty_from_narration(description or "")
    if key.startswith("agg:") or key == "<none>":
        return None
    return key


def revenue_payments(rows: pd.DataFrame) -> pd.DataFrame:
    """Customer payments from statement rows.

    ``rows`` needs ``company_id, date, description, credit, pred_category``.
    Returns ``company_id, date, amount, key`` sorted by (company, date, key, amount).
    """
    r = rows[(rows["pred_category"] == REVENUE) & rows["credit"].notna()]
    amt = r["credit"].astype(float)
    r = r.assign(amount=amt)[amt > 0]
    keys = r["description"].map(payment_key)
    out = pd.DataFrame({
        "company_id": r["company_id"].to_numpy(),
        "date": [d if isinstance(d, date) else pd.Timestamp(d).date() for d in r["date"]],
        "amount": r["amount"].to_numpy(dtype=float),
        "key": keys.to_numpy(),
    })
    out = out[out["key"].notna()]
    return out.sort_values(["company_id", "date", "key", "amount"], kind="mergesort").reset_index(drop=True)


def _ord(dates: Iterable[date]) -> np.ndarray:
    return np.fromiter((d.toordinal() for d in dates), dtype=np.int64)


def _slope(y: np.ndarray) -> float:
    if len(y) < 2:
        return 0.0
    x = np.arange(len(y), dtype=float)
    x -= x.mean()
    return float((x * (y - y.mean())).sum() / (x * x).sum())


def features_at(payments: pd.DataFrame, cutoff: date) -> pd.DataFrame:
    """One row per key with >= 2 payments on or before ``cutoff``.

    ``payments`` are one company's rows from :func:`revenue_payments`. Only
    rows with ``date <= cutoff`` are used; the slice is taken before anything
    else is computed.
    """
    hist = payments[payments["date"] <= cutoff]          # ← the only look at the data
    cols = ["key", *FEATURES]
    if hist.empty:
        return pd.DataFrame(columns=cols)
    c = cutoff.toordinal()
    days = _ord(hist["date"])
    amounts = hist["amount"].to_numpy(dtype=float)
    keys = hist["key"].to_numpy()

    in_365 = days > c - 365
    company_rev_365 = float(amounts[in_365].sum())
    rev_90 = float(amounts[days > c - 90].sum())
    rev_prev_90 = float(amounts[(days > c - 180) & (days <= c - 90)].sum())
    trend = float(np.log((rev_90 + 1.0) / (rev_prev_90 + 1.0)))
    active_keys = len(set(keys[days > c - ACTIVE_WINDOW_DAYS]))

    out = []
    order = np.argsort(keys, kind="mergesort")
    keys_s, days_s, amt_s, in365_s = keys[order], days[order], amounts[order], in_365[order]
    bounds = np.flatnonzero(np.r_[True, keys_s[1:] != keys_s[:-1], True])
    for a, b in zip(bounds[:-1], bounds[1:], strict=True):
        n = b - a
        if n < MIN_PAYMENTS:
            continue
        d = days_s[a:b]
        amt = amt_s[a:b]
        o = np.lexsort((amt, d))                      # stable within-day order
        d, amt = d[o], amt[o]
        gaps = np.diff(d).astype(float)
        gap_mean = float(gaps.mean())
        gap_med = float(np.median(gaps))
        gap_cv = float(gaps.std() / gap_mean) if gap_mean > 0 else 0.0
        since = float(c - d[-1])
        last_gaps = gaps[-LATE_LAST_GAPS:]
        late = float((last_gaps > LATE_GAP_MULTIPLE * max(gap_med, 1.0)).mean())
        log_amt = np.log(amt)
        out.append((
            keys_s[a],
            float(c - d[0]),
            float(n),
            gap_mean,
            gap_cv,
            since,
            since / max(gap_med, 1.0),
            _slope(log_amt[-3:]),
            float(log_amt[-1] - np.log(amt.mean())),
            float(amt_s[a:b][in365_s[a:b]].sum() / company_rev_365) if company_rev_365 > 0 else 0.0,
            late,
            float((d > c - 90).sum()),
            float(active_keys),
            trend,
        ))
    return pd.DataFrame(out, columns=cols)


def label_at(payments: pd.DataFrame, keys: Sequence[str], cutoff: date,
             horizon_days: int = HORIZON_DAYS) -> np.ndarray:
    """1 if the key makes no payment in (cutoff, cutoff + horizon], else 0."""
    end = cutoff + timedelta(days=horizon_days)
    window = payments[(payments["date"] > cutoff) & (payments["date"] <= end)]
    paid = set(window["key"])
    return np.array([0 if k in paid else 1 for k in keys], dtype=np.int8)


def company_units(payments: pd.DataFrame, cutoffs: Sequence[date],
                  with_labels: bool = True) -> pd.DataFrame:
    """Features (+ label) for every eligible key at each cutoff, for one company."""
    parts = []
    for cutoff in cutoffs:
        f = features_at(payments, cutoff)
        if f.empty:
            continue
        f.insert(0, "cutoff_date", cutoff)
        if with_labels:
            f["y"] = label_at(payments, f["key"].tolist(), cutoff)
        parts.append(f)
    if not parts:
        return pd.DataFrame()
    return pd.concat(parts, ignore_index=True)


def is_active(frame: pd.DataFrame, window_days: int = ACTIVE_WINDOW_DAYS) -> pd.Series:
    """A unit is active if its last payment is within ``window_days`` of the cutoff."""
    return frame["days_since_last"] <= window_days


def payments_from_document(doc: Any) -> pd.DataFrame:
    """Customer payments from a categorised StatementDocument (serving path)."""
    rows = [
        (doc.account_id, t.date, t.description,
         float(t.credit) if t.credit is not None else None,
         str(getattr(t.category, "value", t.category)) if t.category is not None else None)
        for t in doc.transactions
    ]
    frame = pd.DataFrame(rows, columns=["company_id", "date", "description", "credit", "pred_category"])
    return revenue_payments(frame)
