"""Per-transaction anomaly features with no look-ahead (Phase 5).

Every feature of a row is computed only from rows at or before it in
``(date, row_seq)`` order within its account. "History" means strictly earlier
rows. The builder walks each account once in that order, reads the state
accumulated so far, and only then adds the current row to that state.
There are no whole-frame group transforms and no full-statement statistics.
So a row's features are the same whether or not later rows exist
(``tests/test_transaction_features.py`` checks this).

Inputs are narration, amount, direction, date and the **predicted** category
(Phase 4 categoriser, or the v2 rules when there is no model). Ground-truth
labels (``true_category``, ``counterparty_key``, ``anomaly_*``) are never read.
The counterparty is derived from the narration by
:func:`counterparty_from_narration`.

``txn_time`` is deliberately not used. ``CanonicalTransaction`` and PDF input
have no time column, and the same features must exist at serving time.

Robust z uses median and IQR/1.349 over a sorted list of prior values. IQR is
O(1) on the sorted list, where MAD would be O(n) per row. The scale is floored
at ``SCALE_FLOOR`` on the log-amount scale, so a counterparty with constant
amounts (rent, salary) still produces a finite, large z when the amount jumps.
"""
from __future__ import annotations

import bisect
import math
import re
from collections import deque
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ml.features.categoriser import normalise_description
from pipeline.customer_identity import matched_aggregator

if TYPE_CHECKING:
    from schema.canonical import StatementDocument

FEATURE_VERSION = "1"

# The per-account model sees raw scale features; IsolationForest is invariant to
# monotone per-feature rescaling, so no per-account standardisation is needed.
RAW_FEATURES: tuple[str, ...] = (
    "log_amount", "is_credit", "cp_robust_z", "cat_robust_z", "cp_first_seen",
    "cp_share_mtd_out", "velocity_1d", "velocity_7d", "dow", "month_end", "days_since_cp",
)
# The pooled model compares companies, so the scale-dependent features are
# swapped for causal account-relative versions.
STD_FEATURES: tuple[str, ...] = (
    "acct_amount_z", "is_credit", "cp_robust_z", "cat_robust_z", "cp_first_seen",
    "cp_share_mtd_out", "velocity_1d_rel", "velocity_7d_rel", "dow", "month_end", "days_since_cp",
)
ALL_FEATURES: tuple[str, ...] = tuple(dict.fromkeys(RAW_FEATURES + STD_FEATURES))

MIN_PRIOR_FOR_Z = 3
SCALE_FLOOR = 0.10          # log-amount units (~10% amount change)
Z_CLIP = 20.0
DAYS_SINCE_FIRST_SEEN = 400.0
MONTH_END_DAYS = 3
UNKNOWN_CATEGORY = "UNCATEGORISED"

# ── counterparty from narration ─────────────────────────────────────────────
_VPA = re.compile(r"[A-Za-z0-9._\-]+@[A-Za-z0-9._\-]+")
_TOKEN_OK = re.compile(r"^[a-z][a-z&']*$")
# Rail words, routing/reference words, legal suffixes and IFSC bank prefixes.
_NOISE = frozenset("""
neft rtgs imps upi ib funds transfer dr cr neftcr neftdr rtgscr rtgsdr ach ecs nach mmt
by to from trf tfr txn ref inv payment paym pay netbank mum cms clg chq cheque ft
pvt private ltd limited llp inc corp co the india p2a p2m mb bil onl d
sbin hdfc icic utib kkbk yesb punb barb idfb indb ubin cnrb ioba bkid ucba mahb psib
idib iobb fdrl kvbl rbls aubl csbk dlxb sibl tmbl jaka hsbc scbl citi dbss
""".split())
_MAX_KEY_TOKENS = 3


def counterparty_from_narration(desc: str) -> str:
    """A counterparty key derived from the narration only.

    Aggregator settlements map to ``agg:<name>``. VPAs are removed first, so a
    handle like ``x@paytm`` doesn't make a vendor look like Paytm. Otherwise the
    key is the first three name-like tokens after masking references, VPAs, IFSC
    codes, rail words, legal suffixes and bank codes (Phase 4
    ``normalise_description`` does the masking).
    """
    agg = matched_aggregator(_VPA.sub(" ", desc or ""))
    if agg:
        return "agg:" + agg.lower()
    toks = [t for t in normalise_description(desc or "").split()
            if not t.startswith("<") and _TOKEN_OK.match(t) and t not in _NOISE]
    return " ".join(toks[:_MAX_KEY_TOKENS]) or "<none>"


# ── causal state helpers ────────────────────────────────────────────────────

def _robust_z(x: float, hist: list[float]) -> float:
    """z of x against a sorted list of prior values (0 if too little history)."""
    n = len(hist)
    if n < MIN_PRIOR_FOR_Z:
        return 0.0
    med = _quantile(hist, 0.5)
    iqr = _quantile(hist, 0.75) - _quantile(hist, 0.25)
    z = (x - med) / max(iqr / 1.349, SCALE_FLOOR)
    return float(max(-Z_CLIP, min(Z_CLIP, z)))


def _quantile(sorted_vals: list[float], q: float) -> float:
    """Linear-interpolated quantile of an already sorted list (numpy 'linear')."""
    pos = (len(sorted_vals) - 1) * q
    lo = math.floor(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def _days_in_month(d: date) -> int:
    nxt = date(d.year + (d.month == 12), d.month % 12 + 1, 1)
    return (nxt - timedelta(days=1)).day


@dataclass
class _Account:
    cp_hist: dict[str, list[float]]
    cat_hist: dict[tuple[str, int], list[float]]
    dir_hist: dict[int, list[float]]
    cp_last: dict[str, date]
    window: deque[date]
    month: tuple[int, int] | None
    mtd_cp_out: dict[str, float]
    mtd_out: float
    n_rows: int
    n_days: int
    last_day: date | None

    @classmethod
    def new(cls) -> _Account:
        return cls({}, {}, {0: [], 1: []}, {}, deque(), None, {}, 0.0, 0, 0, None)


def _features_for_row(st: _Account, d: date, amount: float, is_credit: int,
                      cp: str, cat: str) -> dict[str, float]:
    """Read state (history + this row where the definition says so), then update."""
    la = math.log1p(max(amount, 0.0))

    # month-to-date outflow share (includes this row)
    ym = (d.year, d.month)
    if st.month != ym:
        st.month, st.mtd_cp_out, st.mtd_out = ym, {}, 0.0
    if not is_credit:
        st.mtd_cp_out[cp] = st.mtd_cp_out.get(cp, 0.0) + amount
        st.mtd_out += amount
        share = st.mtd_cp_out[cp] / st.mtd_out if st.mtd_out > 0 else 0.0
    else:
        share = 0.0

    # velocity: rows in [d-0, d] and [d-6, d] up to and including this row
    st.window.append(d)
    while st.window and st.window[0] < d - timedelta(days=6):
        st.window.popleft()
    v7 = len(st.window)
    v1 = sum(1 for x in st.window if x == d)

    # expanding mean rows per active day (includes this row)
    st.n_rows += 1
    if st.last_day != d:
        st.n_days += 1
        st.last_day = d
    mean_per_day = st.n_rows / st.n_days

    cp_prior = st.cp_hist.get(cp, [])
    cat_key = (cat, is_credit)
    last = st.cp_last.get(cp)
    feats = {
        "log_amount": la,
        "is_credit": float(is_credit),
        "cp_robust_z": _robust_z(la, cp_prior),
        "cat_robust_z": _robust_z(la, st.cat_hist.get(cat_key, [])),
        "cp_first_seen": 0.0 if cp_prior else 1.0,
        "cp_share_mtd_out": share,
        "velocity_1d": float(v1),
        "velocity_7d": float(v7),
        "dow": float(d.weekday()),
        "month_end": float(d.day > _days_in_month(d) - MONTH_END_DAYS),
        "days_since_cp": float((d - last).days) if last is not None else DAYS_SINCE_FIRST_SEEN,
        "acct_amount_z": _robust_z(la, st.dir_hist[is_credit]),
        "velocity_1d_rel": v1 / mean_per_day,
        "velocity_7d_rel": v7 / (7.0 * mean_per_day),
    }

    # update history with this row (after reading it)
    bisect.insort(st.cp_hist.setdefault(cp, []), la)
    bisect.insort(st.cat_hist.setdefault(cat_key, []), la)
    bisect.insort(st.dir_hist[is_credit], la)
    st.cp_last[cp] = d
    return feats


# ── public builders ─────────────────────────────────────────────────────────

INPUT_COLUMNS = ("txn_id", "account_id", "date", "row_seq", "description",
                 "debit", "credit", "pred_category")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    if v is pd.NA or v is pd.NaT:
        return None
    return float(Decimal(str(v)))


def build_features(rows: pd.DataFrame) -> pd.DataFrame:
    """Feature frame for ``rows`` (columns ``INPUT_COLUMNS``; other columns ignored).

    Returns one row per input row, ordered by (account_id, date, row_seq), with
    ``txn_id, account_id, date, counterparty`` and every name in ``ALL_FEATURES``.
    """
    missing = [c for c in INPUT_COLUMNS if c not in rows.columns]
    if missing:
        raise ValueError(f"missing input columns: {missing}")
    df = rows[list(INPUT_COLUMNS)].sort_values(
        ["account_id", "date", "row_seq"], kind="mergesort").reset_index(drop=True)
    out: list[dict[str, Any]] = []
    states: dict[str, _Account] = {}
    for r in df.itertuples(index=False):
        d = r.date if isinstance(r.date, date) else pd.Timestamp(r.date).date()
        credit, debit = _num(r.credit), _num(r.debit)
        if credit is not None:
            amount, is_credit = credit, 1
        else:
            amount, is_credit = (debit or 0.0), 0
        cp = counterparty_from_narration(r.description or "")
        cat = str(r.pred_category) if r.pred_category is not None and r.pred_category == r.pred_category \
            else UNKNOWN_CATEGORY
        st = states.setdefault(r.account_id, _Account.new())
        feats = _features_for_row(st, d, amount, is_credit, cp, cat)
        out.append({"txn_id": r.txn_id, "account_id": r.account_id, "date": d,
                    "counterparty": cp, **feats})
    cols = ["txn_id", "account_id", "date", "counterparty", *ALL_FEATURES]
    return pd.DataFrame(out, columns=cols)


def rows_from_document(doc: StatementDocument) -> pd.DataFrame:
    """Serving path: a document's transactions as ``INPUT_COLUMNS`` rows.

    Statement order is the row order (``row_seq``); the category is whatever the
    categorise stage set (predicted, never a label).
    """
    recs = []
    for i, t in enumerate(doc.transactions):
        cat = getattr(t.category, "value", t.category)
        recs.append({
            "txn_id": t.transaction_id, "account_id": doc.account_id, "date": t.date,
            "row_seq": i, "description": t.description,
            "debit": None if t.debit is None else float(t.debit),
            "credit": None if t.credit is None else float(t.credit),
            "pred_category": None if cat is None else str(cat),
        })
    return pd.DataFrame(recs, columns=list(INPUT_COLUMNS))


def build_from_document(doc: StatementDocument) -> pd.DataFrame:
    return build_features(rows_from_document(doc))


def matrix(feats: pd.DataFrame, names: Sequence[str]) -> np.ndarray:
    out: np.ndarray = feats.loc[:, list(names)].to_numpy(dtype=np.float64)
    return out


def feature_names(view: str) -> tuple[str, ...]:
    if view == "raw":
        return RAW_FEATURES
    if view == "std":
        return STD_FEATURES
    raise ValueError(f"unknown feature view {view!r}")


def iter_accounts(feats: pd.DataFrame) -> Iterable[tuple[str, pd.DataFrame]]:
    for acct, g in feats.groupby("account_id", sort=True):
        yield str(acct), g
