"""Phase 5: anomaly features have no look-ahead and use no labels."""
from __future__ import annotations

import random
from datetime import date, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
import pytest

from ml.features.transaction_features import (
    ALL_FEATURES,
    DAYS_SINCE_FIRST_SEEN,
    INPUT_COLUMNS,
    RAW_FEATURES,
    STD_FEATURES,
    build_features,
    build_from_document,
    counterparty_from_narration,
)

VENDORS = ["NEFT/SBIN0001234/ACME CLOUD PVT LTD/INV{n}", "UPI-ZEN OFFICE SUPPLIES-zen@okaxis-{n}-PAY",
           "RTGS DR-UTIB0001111-ORBIT LOGISTICS LLP-HDFCN{n}", "ECS DR-SALARY-ASHA HEGDE-{n}"]
CUSTOMERS = ["NEFT CR-KKBK0002222-TAPTI EXPORTS LTD-ICICN{n}", "NEFT CR-RAZORPAY SOFTWARE PVT LTD-SETL{n}"]
CATS = ["VENDOR_PAYMENT", "VENDOR_PAYMENT", "VENDOR_PAYMENT", "SALARY"]


def _company(n: int = 400, seed: int = 7, account: str = "acct1") -> pd.DataFrame:
    rng = random.Random(seed)
    d = date(2024, 1, 1)
    rows = []
    for i in range(n):
        d += timedelta(days=rng.choice([0, 0, 1, 1, 2, 3]))
        if rng.random() < 0.6:
            j = rng.randrange(len(VENDORS))
            desc, debit, credit, cat = VENDORS[j].format(n=100000 + i), round(rng.uniform(1e3, 9e4), 2), None, CATS[j]
        else:
            j = rng.randrange(len(CUSTOMERS))
            desc, debit, credit, cat = CUSTOMERS[j].format(n=200000 + i), None, round(rng.uniform(5e3, 2e5), 2), "REVENUE"
        rows.append({"txn_id": f"t{i:05d}", "account_id": account, "date": d, "row_seq": i,
                     "description": desc, "debit": debit, "credit": credit, "pred_category": cat})
    return pd.DataFrame(rows)


def _assert_frames_equal(a: pd.DataFrame, b: pd.DataFrame) -> None:
    pd.testing.assert_frame_equal(a.reset_index(drop=True), b.reset_index(drop=True),
                                  check_exact=True)


@pytest.mark.parametrize("cut", [1, 5, 37, 150, 299, 399])
def test_prefix_invariance(cut: int) -> None:
    """features(rows[:k]) == features(all)[:k], exactly, for every feature."""
    rows = _company()
    full = build_features(rows)
    prefix = build_features(rows.iloc[:cut])
    _assert_frames_equal(prefix, full.iloc[:cut])


def test_appending_future_rows_changes_no_earlier_row() -> None:
    rows = _company(300)
    before = build_features(rows)
    last = rows.date.max()
    future = pd.DataFrame([
        # a huge payment to a brand-new counterparty, then a same-day burst
        {"txn_id": "f0", "account_id": "acct1", "date": last + timedelta(days=1), "row_seq": 300,
         "description": "NEFT/HDFC0009999/NEVER SEEN TRADERS/INV1", "debit": 9_999_999.37,
         "credit": None, "pred_category": "VENDOR_PAYMENT"},
        *[{"txn_id": f"f{i}", "account_id": "acct1", "date": last + timedelta(days=2),
           "row_seq": 300 + i, "description": f"UPI-PERSON {i}-p{i}@ybl-{i}", "debit": 12345.67,
           "credit": None, "pred_category": "OTHER"} for i in range(1, 7)],
        # and more rows for existing counterparties far in the future
        *[{"txn_id": f"g{i}", "account_id": "acct1", "date": last + timedelta(days=30 + i),
           "row_seq": 310 + i, "description": VENDORS[0].format(n=i), "debit": 50.0 * (i + 1),
           "credit": None, "pred_category": "VENDOR_PAYMENT"} for i in range(20)],
    ])
    after = build_features(pd.concat([rows, future], ignore_index=True))
    _assert_frames_equal(after.iloc[: len(before)], before)
    # the appended rows themselves do look unusual
    f0 = after[after.txn_id == "f0"].iloc[0]
    assert f0.cp_first_seen == 1.0
    assert after[after.txn_id == "f6"].iloc[0].velocity_1d == 6.0


def test_same_day_later_row_does_not_change_earlier_row() -> None:
    """Order within a date is row_seq; a later same-day row can't leak backwards."""
    rows = _company(120)
    before = build_features(rows)
    extra = rows.iloc[[-1]].assign(txn_id="same_day", row_seq=10_000, debit=1e7, credit=None)
    after = build_features(pd.concat([rows, extra], ignore_index=True))
    _assert_frames_equal(after.iloc[: len(before)], before)


def test_accounts_are_independent() -> None:
    a, b = _company(200, seed=1, account="A"), _company(200, seed=2, account="B")
    both = build_features(pd.concat([a, b], ignore_index=True))
    _assert_frames_equal(both[both.account_id == "A"], build_features(a))
    _assert_frames_equal(both[both.account_id == "B"], build_features(b))


def test_hand_computed_values() -> None:
    base = date(2024, 3, 25)
    recs = []
    for i, amt in enumerate([1000.0, 1000.0, 1000.0, 1000.0, 3000.0]):
        recs.append({"txn_id": f"r{i}", "account_id": "x", "date": base + timedelta(days=i),
                     "row_seq": i, "description": "NEFT/SBIN0001234/ACME CLOUD PVT LTD/INV9",
                     "debit": amt, "credit": None, "pred_category": "VENDOR_PAYMENT"})
    recs.append({"txn_id": "r5", "account_id": "x", "date": base + timedelta(days=4), "row_seq": 5,
                 "description": "UPI-NEW PAYEE-np@ybl-1", "debit": 1000.0, "credit": None,
                 "pred_category": "OTHER"})
    f = build_features(pd.DataFrame(recs)).set_index("txn_id")
    # first row: first seen, no z history
    assert f.loc["r0", "cp_first_seen"] == 1.0
    assert f.loc["r0", "days_since_cp"] == DAYS_SINCE_FIRST_SEEN
    assert f.loc["r0", "cp_robust_z"] == 0.0
    assert f.loc["r1", "days_since_cp"] == 1.0
    # 4th identical payment: 3 priors, IQR 0 -> scale floor, z 0
    assert f.loc["r3", "cp_robust_z"] == 0.0
    # 3000 after four 1000s: (log1p(3000)-log1p(1000))/0.10
    exp = (np.log1p(3000.0) - np.log1p(1000.0)) / 0.10
    assert f.loc["r4", "cp_robust_z"] == pytest.approx(exp)
    # 2024-03-25..29: month-end is the last 3 days (29, 30, 31)
    assert f.loc["r3", "month_end"] == 0.0 and f.loc["r4", "month_end"] == 1.0
    # MTD share: r4 is ACME's 5th debit in March; total March debits so far 7000
    assert f.loc["r4", "cp_share_mtd_out"] == pytest.approx(7000.0 / 7000.0)
    assert f.loc["r5", "cp_share_mtd_out"] == pytest.approx(1000.0 / 8000.0)
    # velocity: r4 and r5 share a date
    assert f.loc["r5", "velocity_1d"] == 2.0 and f.loc["r4", "velocity_1d"] == 1.0
    assert f.loc["r5", "velocity_7d"] == 6.0
    assert f.loc["r5", "cp_first_seen"] == 1.0


def test_label_columns_are_ignored() -> None:
    rows = _company(150)
    leaky = rows.assign(true_category="X", counterparty_key="LEAK", anomaly_type="ml_x",
                        anomaly_kind="ml_only", txn_time="01:00:00")
    _assert_frames_equal(build_features(leaky), build_features(rows))
    banned = ("category", "customer", "template", "anomaly", "profile", "label", "time")
    for name in ALL_FEATURES:
        assert not any(b in name for b in banned), name


def test_serving_path_matches_corpus_path() -> None:
    from schema.canonical import CanonicalTransaction, SourceReference, StatementDocument

    rows = _company(120)
    txns = []
    for i, r in enumerate(rows.itertuples(index=False)):
        txns.append(CanonicalTransaction(
            transaction_id=r.txn_id, date=r.date, description=r.description,
            debit=None if r.debit is None or r.debit != r.debit else Decimal(str(r.debit)),
            credit=None if r.credit is None or r.credit != r.credit else Decimal(str(r.credit)),
            balance=Decimal("0"), category=r.pred_category,
            source_reference=SourceReference(file_path="t", page=0, row=i, raw_text="")))
    doc = StatementDocument(account_id="acct1", statement_period_start=rows.date.min(),
                            statement_period_end=rows.date.max(), source_format="t", transactions=txns)
    _assert_frames_equal(build_from_document(doc), build_features(rows))


def test_views_and_inputs() -> None:
    assert set(RAW_FEATURES) | set(STD_FEATURES) == set(ALL_FEATURES)
    assert "txn_time" not in INPUT_COLUMNS
    with pytest.raises(ValueError):
        build_features(pd.DataFrame({"txn_id": []}))


@pytest.mark.parametrize("desc,expected", [
    ("NEFT/SBIN0001234/ACME CLOUD PVT LTD/INV00123", "acme cloud"),
    ("IMPS-549097636898-NALANDA FINSERV PVT LTD-UTIB-XXXXXXXX2644-", "nalanda finserv"),
    ("NEFT CR-KKBK2354812-RAZORPAY SOFTWARE PVT LTD-SETL005142", "agg:razorpay"),
    ("UPI-NARMADA AGRITECH PVT. LTD.-narmada957@paytm-211360141358", "narmada agritech"),
    ("ECS DR-SALARY-JYOTI MISHRA-1929E93B", "salary jyoti mishra"),
    ("", "<none>"),
])
def test_counterparty_from_narration(desc: str, expected: str) -> None:
    assert counterparty_from_narration(desc) == expected
