"""Company feature table for segmentation (pipeline outputs, no labels).

Each corpus company becomes a StatementDocument from ``features/`` rows with
PREDICTED categories (Phase 4 categoriser, LLM off, cached), then
``FinancialAnalyst`` runs on it exactly as in the pipeline, and
:func:`ml.features.company_features.raw_features` turns both into features.
``labels/`` is never opened here (evaluate.py reads the profile for ARI).
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ml.anomaly.data import company_document, load_rows, predict_categories
from ml.categoriser.data import CorpusGateError, check_gate
from ml.features.company_features import FEATURE_VERSION, FEATURES, raw_features

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
CACHE_DIR = Path("results/segmentation/cache")
PRED_CACHE_DIR = Path("results/anomaly/cache")

__all__ = ["CorpusGateError", "check_gate", "load_config", "segmentation_split", "company_features",
           "categorised_document"]


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)
    return cfg


def segmentation_split(corpus_dir: Path) -> dict[str, str]:
    sp = pd.read_parquet(corpus_dir / "splits" / "segmentation_holdout.parquet")
    return dict(zip(sp.company_id, sp.split, strict=True))


def categorised_document(g: pd.DataFrame) -> Any:
    """A StatementDocument whose categories are the predicted ones in ``g.pred_category``."""
    from schema.canonical import TransactionCategory

    doc = company_document(g)
    cats = dict(zip(g["txn_id"], g["pred_category"], strict=True))
    txns = [t.model_copy(update={"category": TransactionCategory(cats[t.transaction_id])})
            if cats.get(t.transaction_id) else t for t in doc.transactions]
    return doc.model_copy(update={"transactions": txns})


def _company_row(g: pd.DataFrame) -> dict[str, Any]:
    from analysis.financial_analyst import FinancialAnalyst

    doc = categorised_document(g)
    metrics = FinancialAnalyst().analyse(doc)
    return {"company_id": str(g["company_id"].iloc[0]), **raw_features(doc, metrics)}


def company_features(corpus_dir: Path, ident: dict[str, Any], n_jobs: int,
                     cache_dir: Path = CACHE_DIR) -> tuple[pd.DataFrame, str]:
    """One row per company: company_id + raw FEATURES (cached)."""
    rows = load_rows(corpus_dir)
    cats, cat_ver = predict_categories(rows, PRED_CACHE_DIR, ident["corpus_hash"], n_jobs=n_jobs)
    cache = cache_dir / f"company-features-v{FEATURE_VERSION}-{ident['corpus_hash'][:8]}-{cat_ver}.parquet"
    if cache.exists():
        return pd.read_parquet(cache), cat_ver
    rows = rows.assign(pred_category=rows["txn_id"].map(cats))
    groups = [g for _, g in rows.groupby("company_id", sort=True)]
    t0 = time.perf_counter()
    if n_jobs > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            out = list(ex.map(_company_row, groups, chunksize=8))
    else:
        out = [_company_row(g) for g in groups]
    df = pd.DataFrame(out)[["company_id", *FEATURES]]
    log.info("company features built", extra={"companies": len(df), "seconds": round(time.perf_counter() - t0, 1)})
    cache_dir.mkdir(parents=True, exist_ok=True)
    df.to_parquet(cache, index=False)
    return df, cat_ver
