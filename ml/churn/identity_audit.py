"""EVALUATION ONLY: how well each customer identity recovers the generator's customers.

    python -m ml.churn.identity_audit data/corpus/v1

Reads ``labels/transactions.parquet:true_customer_id`` and
``labels/companies.parquet:profile``. Nothing here feeds training, selection or
thresholds; it explains the identity decision in docs/ml/churn.md.

Rows audited: predicted-REVENUE, non-aggregator credits of the three profiles
with customer-level churn, restricted to rows that have a true customer id.

* purity: row-weighted share of each derived id's rows that belong to its
  majority true customer (1.0 = no merges).
* completeness: row-weighted share of each true customer's rows that sit in
  its majority derived id (1.0 = no splits).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd

from ml.anomaly.data import load_rows, predict_categories
from ml.churn.data import PRED_CACHE_DIR, check_gate, load_config
from ml.features.churn_features import payment_key

CHURN_PROFILES = ("healthy_saas", "burning_startup", "services_firm")


def resolver_id(description: str) -> str:
    """CustomerIdentityResolver stage 1+2 (embeddings off, as in the pipeline default)."""
    from pipeline.customer_identity import clean_counterparty

    return clean_counterparty(description) or description.lower().strip()


def eval_frame(corpus_dir: Path, ident: dict[str, Any], n_jobs: int) -> pd.DataFrame:
    """Predicted-REVENUE credit rows with derived ids, true id and profile (evaluation only)."""
    from pipeline.customer_identity import matched_aggregator

    rows = load_rows(corpus_dir)
    cats, _ = predict_categories(rows, PRED_CACHE_DIR, ident["corpus_hash"], n_jobs=n_jobs)
    lab = pd.read_parquet(corpus_dir / "labels" / "transactions.parquet",
                          columns=["txn_id", "true_customer_id", "true_category"])
    prof = pd.read_parquet(corpus_dir / "labels" / "companies.parquet", columns=["company_id", "profile"])
    r = rows.assign(pred_category=rows["txn_id"].map(cats))
    r = r[(r["pred_category"] == "REVENUE") & r["credit"].notna()]
    r = r.merge(lab, on="txn_id", how="left", validate="one_to_one").merge(prof, on="company_id", how="left")
    r = r[~r["description"].map(lambda s: matched_aggregator(s or "") is not None)].copy()
    r["narration_key"] = r["description"].map(payment_key)
    r["resolver_id"] = r["description"].map(resolver_id)
    return r


def quality(frame: pd.DataFrame, col: str) -> dict[str, Any]:
    r = frame[frame["true_customer_id"].notna() & frame[col].notna()]
    by_id = r.groupby(["company_id", col])["true_customer_id"]
    size = by_id.size()
    purity = (by_id.agg(lambda s: s.value_counts().iloc[0]) .sum()) / size.sum()
    by_true = r.groupby(["company_id", "true_customer_id"])[col]
    complete = by_true.agg(lambda s: s.value_counts().iloc[0]).sum() / by_true.size().sum()
    return {
        "identity": col, "rows": int(len(r)),
        "purity": round(float(purity), 4), "completeness": round(float(complete), 4),
        "ids_per_company_median": float(r.groupby("company_id")[col].nunique().median()),
        "true_per_company_median": float(r.groupby("company_id")["true_customer_id"].nunique().median()),
        "true_split_over_ids": round(float((by_true.nunique() > 1).mean()), 4),
        "ids_merging_true": round(float((by_id.nunique() > 1).mean()), 4),
        "ids_single_row": round(float((size == 1).mean()), 4),
    }


def audit(frame: pd.DataFrame) -> dict[str, Any]:
    f = frame[frame["profile"].isin(CHURN_PROFILES)]
    pred_rev_true = float((f["true_category"] == "REVENUE").mean()) if len(f) else float("nan")
    return {"profiles": list(CHURN_PROFILES),
            "pred_revenue_rows_that_are_true_revenue": round(pred_rev_true, 4),
            "resolver": quality(f, "resolver_id"),
            "narration_key": quality(f, "narration_key")}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--n-jobs", type=int, default=3)
    a = ap.parse_args(argv)
    ident = check_gate(a.corpus, load_config(), a.allow_unpinned)
    print(json.dumps(audit(eval_frame(a.corpus, ident, a.n_jobs)), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
