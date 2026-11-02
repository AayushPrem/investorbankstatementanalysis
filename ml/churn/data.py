"""Corpus access for the churn model.

Training reads only:

* ``features/transactions.parquet`` (statement rows);
* the cached PREDICTED categories (Phase 4 categoriser, LLM off; via
  :func:`ml.anomaly.data.predict_categories`);
* ``splits/churn_temporal.parquet`` (which company-cutoffs are train/val/test).

Nothing under ``labels/`` is opened here. The target is computed from the
transactions themselves (:func:`ml.features.churn_features.label_at`).
Evaluation-only label access lives in :mod:`ml.churn.evaluate` and
:mod:`ml.churn.identity_audit`.
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ml.anomaly.data import load_rows, predict_categories
from ml.categoriser.data import CorpusGateError, check_gate
from ml.features.churn_features import FEATURE_VERSION, company_units, revenue_payments

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
CACHE_DIR = Path("results/churn/cache")
PRED_CACHE_DIR = Path("results/anomaly/cache")   # shared predicted-category cache

__all__ = ["CorpusGateError", "check_gate", "load_config", "churn_splits", "corpus_payments",
           "build_units", "load_units"]


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)
    return cfg


def churn_splits(corpus_dir: Path) -> pd.DataFrame:
    sp = pd.read_parquet(corpus_dir / "splits" / "churn_temporal.parquet")
    sp["cutoff_date"] = [d if isinstance(d, date) else pd.Timestamp(d).date() for d in sp["cutoff_date"]]
    return sp


def corpus_payments(corpus_dir: Path, ident: dict[str, Any], n_jobs: int,
                    pred_cache_dir: Path = PRED_CACHE_DIR) -> tuple[pd.DataFrame, str]:
    """Customer payments for every company (predicted REVENUE, non-aggregator)."""
    rows = load_rows(corpus_dir)
    cats, cat_ver = predict_categories(rows, pred_cache_dir, ident["corpus_hash"], n_jobs=n_jobs)
    rows = rows.assign(pred_category=rows["txn_id"].map(cats))
    if rows["pred_category"].isna().all():
        raise RuntimeError("no predicted categories")
    return revenue_payments(rows), cat_ver


def _units_company(args: tuple[str, pd.DataFrame, list[date]]) -> pd.DataFrame:
    cid, pay, cutoffs = args
    u = company_units(pay, cutoffs)
    if not u.empty:
        u.insert(0, "company_id", cid)
    return u


def build_units(payments: pd.DataFrame, splits: pd.DataFrame, n_jobs: int = 1) -> pd.DataFrame:
    """One row per (company, key, cutoff) with features, label ``y`` and ``split``."""
    jobs = []
    by_co = {cid: g for cid, g in payments.groupby("company_id", sort=True)}
    for cid, s in splits.groupby("company_id", sort=True):
        if cid in by_co:
            jobs.append((cid, by_co[cid], sorted(s["cutoff_date"])))
    if n_jobs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            parts = list(ex.map(_units_company, jobs, chunksize=8))
    else:
        parts = [_units_company(j) for j in jobs]
    parts = [p for p in parts if not p.empty]
    if not parts:
        raise RuntimeError("no churn units")
    units = pd.concat(parts, ignore_index=True)
    units = units.merge(splits[["company_id", "cutoff_date", "split"]],
                        on=["company_id", "cutoff_date"], how="left", validate="many_to_one")
    return units


def load_units(corpus_dir: Path, ident: dict[str, Any], n_jobs: int,
               cache_dir: Path = CACHE_DIR) -> tuple[pd.DataFrame, str]:
    """Cached :func:`build_units` over the corpus split file."""
    payments, cat_ver = corpus_payments(corpus_dir, ident, n_jobs)
    cache = cache_dir / f"units-v{FEATURE_VERSION}-{ident['corpus_hash'][:8]}-{ident['splits_hash'][:8]}-{cat_ver}.parquet"
    if cache.exists():
        return pd.read_parquet(cache), cat_ver
    t0 = time.perf_counter()
    units = build_units(payments, churn_splits(corpus_dir), n_jobs)
    log.info("churn units built", extra={"units": len(units), "seconds": round(time.perf_counter() - t0, 1)})
    cache_dir.mkdir(parents=True, exist_ok=True)
    units.to_parquet(cache, index=False)
    return units, cat_ver
