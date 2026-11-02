"""Categoriser features: one code path for training (corpus rows) and serving.

Inputs are only what a bank statement row shows and what ``CanonicalTransaction``
carries: description, debit/credit and date. Deliberately NOT used:

* ``bank`` / ``source_format`` — leaks on the cross-bank split, useless there.
* ``balance`` — account-dependent, says nothing about the category.
* ``ref_no`` — random.
* ``txn_time`` — absent from PDFs and from CanonicalTransaction (train/serve skew).
* ``company_id`` and every label column.

``build_frame`` returns a DataFrame with the columns in :data:`FRAME_COLUMNS`;
``make_column_transformer`` turns it into the sparse design matrix.
"""
from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd
from sklearn.compose import ColumnTransformer
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import OneHotEncoder, StandardScaler

if TYPE_CHECKING:
    from schema.canonical import CanonicalTransaction

FEATURE_VERSION = "1"

# Payment rail, derived from the narration. Same vocabulary as db.models.RAILS
# plus ECS (ECS/ACH/NACH mandates). Order matters: first match wins.
_RAIL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("UPI", re.compile(r"\bUPI\b", re.I)),
    ("RTGS", re.compile(r"\bRTGS\b", re.I)),
    ("NEFT", re.compile(r"\bNEFT\b", re.I)),
    ("IMPS", re.compile(r"\bIMPS\b|\bMMT\b", re.I)),
    ("ECS", re.compile(r"\bN?ECS\b|\bN?ACH\b", re.I)),
    ("CHQ", re.compile(r"\bCHQ\b|\bCHEQUE\b|\bCLG\b", re.I)),
    ("CASH", re.compile(r"\bCASH\b|\bATM\b", re.I)),
    ("CARD", re.compile(r"\bPOS\b|\bCARD\b", re.I)),
]
RAILS = tuple(r for r, _ in _RAIL_PATTERNS) + ("OTHER",)
DOM_BUCKETS = ("d01_05", "d06_10", "d11_20", "d21_25", "d26_31")

FRAME_COLUMNS = (
    "text", "rail", "dom_bucket", "is_credit", "log_amount",
    "round_1000", "round_100", "has_paise",
)

# ── narration normalisation ─────────────────────────────────────────────────
_VPA = re.compile(r"[A-Za-z0-9._\-]+@[A-Za-z0-9._\-]+")  # one handle, not the merchant before its '/'
_IFSC = re.compile(r"\b[A-Z]{4}0[A-Z0-9]{6}\b")
_MASKED_ACCT = re.compile(r"\bX{3,}\d*\b", re.I)
_SEP = re.compile(r"[/\-_.:,;|()\[\]#*]+")
_HAS_DIGIT = re.compile(r"\d")
_WS = re.compile(r"\s+")


def _mask_token(tok: str) -> str:
    """Replace reference-number-like tokens; keep merchant words."""
    digits = sum(c.isdigit() for c in tok)
    if digits == 0:
        return tok
    if digits >= 5 or (len(tok) >= 6 and digits >= 2):
        return "<num>"           # UTRs, UPI refs, hex refs, account numbers, dates
    return tok                   # short tokens like 'inv1', 'q3', 'h1' stay


def normalise_description(desc: str) -> str:
    """Lowercase narration with refs/VPAs/IFSC masked and separators collapsed."""
    s = desc.upper()
    s = _VPA.sub(" <vpa> ", s)
    s = _IFSC.sub(" <ifsc> ", s)
    s = _MASKED_ACCT.sub(" <acct> ", s)
    s = _SEP.sub(" ", s)
    toks = [_mask_token(t) for t in s.split()]
    return _WS.sub(" ", " ".join(toks)).strip().lower()


def rail_of(desc: str) -> str:
    for rail, pat in _RAIL_PATTERNS:
        if pat.search(desc):
            return rail
    return "OTHER"


def dom_bucket(day: int) -> str:
    if day <= 5:
        return "d01_05"
    if day <= 10:
        return "d06_10"
    if day <= 20:
        return "d11_20"
    if day <= 25:
        return "d21_25"
    return "d26_31"


def _row(desc: str, amount: float, is_credit: bool, d: date) -> dict[str, Any]:
    paise = round(amount * 100) % 100
    rupees = round(amount * 100) // 100
    return {
        "text": normalise_description(desc),
        "rail": rail_of(desc),
        "dom_bucket": dom_bucket(d.day),
        "is_credit": int(is_credit),
        "log_amount": math.log1p(max(amount, 0.0)),
        "round_1000": int(paise == 0 and rupees % 1000 == 0),
        "round_100": int(paise == 0 and rupees % 100 == 0),
        "has_paise": int(paise != 0),
    }


def _amount(debit: Any, credit: Any) -> tuple[float, bool]:
    if credit is not None and not (isinstance(credit, float) and math.isnan(credit)):
        return float(Decimal(str(credit))), True
    if debit is not None and not (isinstance(debit, float) and math.isnan(debit)):
        return float(Decimal(str(debit))), False
    return 0.0, False


def build_frame_from_records(
    descriptions: Sequence[str],
    debits: Sequence[Any],
    credits: Sequence[Any],
    dates: Sequence[date],
) -> pd.DataFrame:
    rows = []
    for desc, dr, cr, d in zip(descriptions, debits, credits, dates, strict=True):
        amt, is_cr = _amount(dr, cr)
        rows.append(_row(desc or "", amt, is_cr, d))
    return pd.DataFrame(rows, columns=list(FRAME_COLUMNS))


def build_frame(corpus_rows: pd.DataFrame) -> pd.DataFrame:
    """Features from corpus ``features/transactions.parquet`` rows."""
    dates = [d if isinstance(d, date) else pd.Timestamp(d).date() for d in corpus_rows["date"]]
    return build_frame_from_records(
        corpus_rows["description"].tolist(),
        corpus_rows["debit"].tolist(),
        corpus_rows["credit"].tolist(),
        dates,
    )


def build_frame_from_transactions(txns: Iterable[CanonicalTransaction]) -> pd.DataFrame:
    """Features from pipeline transactions — the serving path."""
    txns = list(txns)
    return build_frame_from_records(
        [t.description for t in txns],
        [t.debit for t in txns],
        [t.credit for t in txns],
        [t.date for t in txns],
    )


def make_column_transformer(
    char_max_features: int | None = 200_000,
    word_max_features: int | None = 50_000,
    min_df: int = 2,
) -> ColumnTransformer:
    """Sparse design matrix: char_wb(3–5) ∪ word(1–2) TF-IDF + structured features."""
    return ColumnTransformer(
        [
            ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), min_df=min_df,
                                     sublinear_tf=True, max_features=char_max_features,
                                     dtype=np.float32), "text"),
            ("word", TfidfVectorizer(analyzer="word", ngram_range=(1, 2), min_df=min_df,
                                     sublinear_tf=True, max_features=word_max_features,
                                     token_pattern=r"(?u)<?\b\w+\b>?",
                                     dtype=np.float32), "text"),
            ("onehot", OneHotEncoder(categories=[list(RAILS), list(DOM_BUCKETS)],
                                     handle_unknown="ignore", dtype=np.float32),
             ["rail", "dom_bucket"]),
            ("amount", StandardScaler(), ["log_amount"]),
            ("flags", "passthrough", ["is_credit", "round_1000", "round_100", "has_paise"]),
        ],
        sparse_threshold=1.0,
    )
