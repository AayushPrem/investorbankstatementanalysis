"""Deterministic, leakage-safe split files for the Phase 4-6 models.

    python -m datasets.splits data/corpus/v1 [--seed 42]

Writes ``<corpus>/splits/*.parquet`` and ``splits/manifest.json`` (sha256 per
file, combined ``splits_hash``; no timestamps). Split rules (UPGRADE_GUIDE §1.5, §9):

* ``company.parquet``: one company-level partition (train/val/test about
  70/15/15, GroupShuffleSplit with groups = company). Categoriser, anomaly and
  churn all use this one partition, so no company is in one task's train set and
  another task's test set.
* ``categoriser_cross_bank.parquet``: train/val on HDFC companies, test on ICICI.
* ``categoriser_unseen_vendor.parquet``: per counterparty_key, GroupShuffleSplit
  by counterparty, so test counterparties never appear in train. Generic keys
  (tax, cash, payment aggregators, self transfers, employees) occur in nearly every
  company and are always train.
* ``churn_temporal.parquet``: (company, cutoff) units. A cutoff is a month end
  with >= 6 months of history before it and 90 days after it inside the
  statement. Two calendar thresholds T1 < T2 (60th/80th percentile of eligible
  cutoffs): train units have cutoff + 90d <= T1, val units have T1 < cutoff and
  cutoff + 90d <= T2, test units have cutoff > T2. Companies come from the
  shared partition. Labels are built in Phase 6.
* ``segmentation_holdout.parquet``: 20% of companies held out for stability checks.
"""
from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from sklearn.model_selection import GroupShuffleSplit

from datasets.build_corpus import combined_hash, sha256_file

GENERIC_KEY_PREFIXES = ("TAX ", "EMPLOYEE ")
GENERIC_KEYS = frozenset({
    "RAZORPAY", "CASH", "SWIGGY", "ZOMATO", "FX REMITTANCE", "SELF TRANSFER", "OTHER",
    "RAZORPAY SOFTWARE PVT LTD FEE DEDUCTION",
})
HISTORY_MONTHS = 6
HORIZON_DAYS = 90


def _group_split(ids: list[str], test_size: float, seed: int) -> tuple[list[str], list[str]]:
    """GroupShuffleSplit over unique ids (one row per group). Returns (train, test), sorted."""
    ids = sorted(ids)
    if len(ids) < 2:
        return ids, []
    gss = GroupShuffleSplit(n_splits=1, test_size=test_size, random_state=seed)
    tr, te = next(gss.split(np.zeros(len(ids)), groups=ids))
    return sorted(ids[i] for i in tr), sorted(ids[i] for i in te)


def company_partition(company_ids: list[str], seed: int) -> dict[str, str]:
    train, rest = _group_split(company_ids, 0.30, seed)
    val, test = _group_split(rest, 0.50, seed + 1)
    out = {c: "train" for c in train}
    out.update({c: "val" for c in val})
    out.update({c: "test" for c in test})
    return out


def is_generic_key(key: str | None) -> bool:
    return key is None or key in GENERIC_KEYS or key.startswith(GENERIC_KEY_PREFIXES)


def _month_ends(start: date, end: date) -> list[date]:
    out = []
    y, m = start.year, start.month
    while True:
        nxt = date(y + (m == 12), m % 12 + 1, 1)
        me = nxt - timedelta(days=1)
        if me > end:
            break
        out.append(me)
        y, m = nxt.year, nxt.month
    return out


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def eligible_cutoffs(period_start: date, period_end: date) -> list[date]:
    first_ok = _add_months(date(period_start.year, period_start.month, 1), HISTORY_MONTHS)
    return [c for c in _month_ends(period_start, period_end)
            if c >= first_ok - timedelta(days=1) and c + timedelta(days=HORIZON_DAYS) <= period_end]


def _write(df: pd.DataFrame, path: Path) -> None:
    table = pa.Table.from_pandas(df, preserve_index=False)
    table = table.replace_schema_metadata(None)    # no pandas metadata blob
    pq.write_table(table, path, compression="zstd")


def build_splits(corpus_dir: Path, seed: int = 42, quiet: bool = False) -> dict[str, Any]:
    out = corpus_dir / "splits"
    out.mkdir(exist_ok=True)
    feats = pd.read_parquet(corpus_dir / "features" / "companies.parquet")
    labels_co = pd.read_parquet(corpus_dir / "labels" / "companies.parquet",
                                columns=["company_id", "bank"])
    companies = feats.merge(labels_co, on=["company_id", "bank"]).sort_values("company_id")
    ids = companies["company_id"].tolist()

    # 1. shared company partition
    part = company_partition(ids, seed)
    company_df = pd.DataFrame({"company_id": ids, "split": [part[c] for c in ids]})
    _write(company_df, out / "company.parquet")

    # 2. cross-bank
    hdfc = companies.loc[companies["bank"] == "hdfc", "company_id"].tolist()
    icici = companies.loc[companies["bank"] == "icici", "company_id"].tolist()
    h_train, h_val = _group_split(hdfc, 0.15, seed + 2)
    xb = ([(c, "train") for c in h_train] + [(c, "val") for c in h_val]
          + [(c, "test") for c in sorted(icici)])
    xb_df = pd.DataFrame(sorted(xb), columns=["company_id", "split"])
    _write(xb_df, out / "categoriser_cross_bank.parquet")

    # 3. unseen vendor (by counterparty)
    lt = pd.read_parquet(corpus_dir / "labels" / "transactions.parquet",
                         columns=["counterparty_key"])
    counts = lt["counterparty_key"].fillna("OTHER").value_counts()
    keys = sorted(counts.index)
    named = [k for k in keys if not is_generic_key(k)]
    k_train, k_test = _group_split(named, 0.20, seed + 3)
    k_train, k_val = _group_split(k_train, 0.125, seed + 4)
    ksplit = {k: "train" for k in keys}
    ksplit.update({k: "val" for k in k_val})
    ksplit.update({k: "test" for k in k_test})
    uv_df = pd.DataFrame({
        "counterparty_key": keys,
        "split": [ksplit[k] for k in keys],
        "generic": [is_generic_key(k) for k in keys],
        "n_rows": [int(counts[k]) for k in keys],
    })
    _write(uv_df, out / "categoriser_unseen_vendor.parquet")

    # 4. churn temporal
    units = [(r.company_id, c) for r in companies.itertuples()
             for c in eligible_cutoffs(r.period_start, r.period_end)]
    t1 = t2 = None
    churn_rows: list[tuple[str, date, str]] = []
    if units:
        cut_series = pd.Series(sorted(c for _, c in units))
        t1 = cut_series.iloc[int(0.6 * (len(cut_series) - 1))]
        t2 = cut_series.iloc[int(0.8 * (len(cut_series) - 1))]
        h = timedelta(days=HORIZON_DAYS)
        for cid, c in units:
            s = part[cid]
            if s == "train" and c + h <= t1:
                churn_rows.append((cid, c, "train"))
            elif s == "val" and c > t1 and c + h <= t2:
                churn_rows.append((cid, c, "val"))
            elif s == "test" and c > t2:
                churn_rows.append((cid, c, "test"))
    churn_df = pd.DataFrame(sorted(churn_rows), columns=["company_id", "cutoff_date", "split"])
    _write(churn_df, out / "churn_temporal.parquet")

    # 5. segmentation holdout
    fit, hold = _group_split(ids, 0.20, seed + 5)
    seg_df = pd.DataFrame(sorted([(c, "fit") for c in fit] + [(c, "holdout") for c in hold]),
                          columns=["company_id", "split"])
    _write(seg_df, out / "segmentation_holdout.parquet")

    files = {p.name: sha256_file(p) for p in sorted(out.glob("*.parquet"))}
    manifest: dict[str, Any] = {
        "seed": seed,
        "corpus_hash": json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))["corpus_hash"],
        "churn_thresholds": {"t1": str(t1) if t1 else None, "t2": str(t2) if t2 else None,
                             "history_months": HISTORY_MONTHS, "horizon_days": HORIZON_DAYS},
        "sizes": {
            "company": company_df["split"].value_counts().sort_index().to_dict(),
            "categoriser_cross_bank": xb_df["split"].value_counts().sort_index().to_dict(),
            "categoriser_unseen_vendor_keys": uv_df["split"].value_counts().sort_index().to_dict(),
            "churn_temporal_units": churn_df["split"].value_counts().sort_index().to_dict(),
            "segmentation_holdout": seg_df["split"].value_counts().sort_index().to_dict(),
        },
        "files": files,
        "splits_hash": combined_hash(files),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True, default=str) + "\n",
                                       encoding="utf-8")
    if not quiet:
        print(json.dumps({k: manifest[k] for k in ("churn_thresholds", "sizes", "splits_hash")},
                         indent=1, default=str))
    return manifest


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Write deterministic split files for a corpus.")
    p.add_argument("corpus_dir", type=Path, nargs="?", default=Path("data/corpus/v1"))
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args(argv)
    build_splits(a.corpus_dir, a.seed)


if __name__ == "__main__":
    main()
