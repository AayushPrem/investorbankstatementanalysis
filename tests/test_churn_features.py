"""Phase 6: churn features use only data <= cutoff (no look-ahead), and the label window."""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import pandas as pd
import pytest

from ml.features.churn_features import (
    FEATURES,
    company_units,
    features_at,
    label_at,
    payment_key,
    payments_from_document,
    revenue_payments,
)
from schema.canonical import CanonicalTransaction, SourceReference, StatementDocument

CUTOFF = date(2024, 6, 30)
NAMES = ["ACME SOFTWARE PVT LTD", "ORBIT LOGISTICS LLP", "ZEN RETAIL LIMITED", "HELIX LABS"]


def _rows(seed: int = 0, start: date = date(2023, 7, 1), end: date = date(2024, 12, 31)) -> pd.DataFrame:
    rng = random.Random(seed)
    out = []
    d = start
    i = 0
    while d <= end:
        for j, name in enumerate(NAMES):
            if rng.random() < 0.8 - 0.15 * j:
                out.append({"company_id": "c1", "date": d + timedelta(days=rng.randint(0, 5)),
                            "description": f"NEFT CR-HDFC000{j}123-{name}-INV{i:04d}",
                            "credit": round(rng.uniform(4e4, 6e4), 2), "pred_category": "REVENUE"})
                i += 1
        out.append({"company_id": "c1", "date": d + timedelta(days=3),
                    "description": "RAZORPAY SETTLEMENT RZP123", "credit": 9999.0,
                    "pred_category": "REVENUE"})
        out.append({"company_id": "c1", "date": d + timedelta(days=7),
                    "description": "NEFT DR-AWS INDIA-INV9", "credit": None, "pred_category": "VENDOR_PAYMENT"})
        d = (d.replace(day=1) + timedelta(days=32)).replace(day=1)
    return pd.DataFrame(out)


def _feats(rows: pd.DataFrame) -> pd.DataFrame:
    return features_at(revenue_payments(rows), CUTOFF).sort_values("key").reset_index(drop=True)


def test_payment_key_excludes_aggregators_and_masks_references() -> None:
    assert payment_key("RAZORPAY SETTLEMENT RZP123") is None
    a = payment_key("NEFT CR-HDFC0001234-ACME SOFTWARE PVT LTD-INV0001")
    b = payment_key("NEFT CR-ICIC0009999-ACME SOFTWARE PRIVATE LIMITED-INV0777")
    assert a == b == "acme software"


def test_features_ignore_everything_after_cutoff() -> None:
    rows = _rows()
    base = _feats(rows)
    assert not base.empty and list(base.columns) == ["key", *FEATURES]
    after = rows["date"] > CUTOFF
    assert after.any()

    changed = rows.copy()
    changed.loc[after, "credit"] = changed.loc[after, "credit"] * 7          # amounts after cutoff
    pd.testing.assert_frame_equal(_feats(changed), base)

    deleted = rows[~after]                                                     # rows after cutoff removed
    pd.testing.assert_frame_equal(_feats(deleted), base)

    extra = pd.DataFrame([{"company_id": "c1", "date": CUTOFF + timedelta(days=k),
                           "description": f"NEFT CR-HDFC0009123-BRAND NEW CORP-INV{k}",
                           "credit": 1e6, "pred_category": "REVENUE"} for k in (1, 10, 40)])
    added = pd.concat([rows, extra], ignore_index=True)                        # new rows after cutoff
    pd.testing.assert_frame_equal(_feats(added), base)


def test_features_do_react_to_rows_before_cutoff() -> None:
    """Guards against a vacuous no-look-ahead test: pre-cutoff changes must show up."""
    rows = _rows()
    base = _feats(rows)
    before = rows.index[(rows["date"] <= CUTOFF) & rows["description"].str.contains("ACME")][-1]
    changed = rows.copy()
    changed.loc[before, "credit"] = changed.loc[before, "credit"] * 3
    f = _feats(changed)
    acme = f["key"] == "acme software"
    assert not f.loc[acme, "amount_last_vs_mean"].equals(base.loc[acme, "amount_last_vs_mean"])


def test_label_window_is_cutoff_exclusive_horizon_inclusive() -> None:
    pay = pd.DataFrame({"company_id": "c", "key": "k", "amount": 1.0,
                        "date": [date(2024, 1, 1), date(2024, 3, 1)]})
    assert label_at(pay.assign(date=[CUTOFF, CUTOFF + timedelta(days=90)]), ["k"], CUTOFF)[0] == 0
    assert label_at(pay.assign(date=[CUTOFF, CUTOFF + timedelta(days=91)]), ["k"], CUTOFF)[0] == 1
    assert label_at(pay.assign(date=[CUTOFF - timedelta(days=5), CUTOFF]), ["k"], CUTOFF)[0] == 1


def test_units_need_two_payments_before_cutoff() -> None:
    pay = revenue_payments(_rows())
    units = company_units(pay, [CUTOFF])
    hist = pay[pay["date"] <= CUTOFF].groupby("key").size()
    assert set(units["key"]) == set(hist[hist >= 2].index)
    assert units["n_payments"].min() >= 2 and set(units["y"]) <= {0, 1}


def _doc(rows: pd.DataFrame) -> StatementDocument:
    txns = []
    for i, r in enumerate(rows.sort_values("date", kind="mergesort").itertuples(index=False)):
        credit = None if r.credit is None or r.credit != r.credit else Decimal(str(r.credit))
        txns.append(CanonicalTransaction(
            transaction_id=f"t{i:05d}", date=r.date, description=r.description, credit=credit,
            debit=None if credit is not None else Decimal("1234.50"), balance=Decimal("0"),
            source_reference=SourceReference(file_path="t", page=0, row=i, raw_text="")))
    return StatementDocument(account_id="c1", statement_period_start=txns[0].date,
                             statement_period_end=txns[-1].date, source_format="t", transactions=txns)


def test_end_to_end_with_real_categoriser_has_no_look_ahead() -> None:
    """Rows after the cutoff can't change the categories (and so the features) before it."""
    from analysis.categoriser import Categoriser

    rows = _rows()
    upto = rows[rows["date"] <= CUTOFF]
    cat = Categoriser(llm_enabled=False)
    short_doc, _ = cat.categorise(_doc(upto))
    full_doc, _ = cat.categorise(_doc(rows))
    f_short = features_at(payments_from_document(short_doc), CUTOFF).sort_values("key").reset_index(drop=True)
    f_full = features_at(payments_from_document(full_doc), CUTOFF).sort_values("key").reset_index(drop=True)
    if f_short.empty:
        pytest.skip("categoriser labelled no synthetic credit as REVENUE")
    pd.testing.assert_frame_equal(f_full, f_short)
