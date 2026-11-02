"""Train the anomaly model: pooled IsolationForest + validation-chosen thresholds.

Usage:
    python -m ml.anomaly.train data/corpus/v1 [--allow-unpinned] [--profile full|ci]
                                              [--results results/anomaly] [--n-jobs 3]

Steps:
1. Hash gate (``ml/anomaly/config.yaml`` pinned_corpora).
2. Predicted categories (Phase 4 categoriser, LLM off) for every row, cached.
3. Causal features per company (``ml/features/transaction_features.py``), cached.
4. Fit the pooled forest on TRAIN companies' standardised view.
5. On VALIDATION companies, choose min_history, contamination and severity
   bands. Labels are joined here and nowhere earlier.
6. Register the model (``ml/registry``) and write ``results/anomaly/selection[_ci].json``.

Test companies are not scored here. ``evaluate.py`` reports on them once.
Parallelism is one level only: a process pool over companies or accounts,
with ``n_jobs=1`` inside every forest.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml import registry
from ml.anomaly import MODEL_NAME
from ml.anomaly.data import (
    check_gate,
    company_splits,
    load_config,
    load_eval_labels,
    load_rows,
    predict_categories,
)
from ml.anomaly.metrics import at_k, average_precision, set_metrics
from ml.anomaly.model import (
    SCORER_PER_ACCOUNT,
    SCORER_POOLED,
    AnomalyModel,
    ForestParams,
    fit_pooled,
    flag_mask,
    score_all,
)
from ml.features.transaction_features import FEATURE_VERSION, STD_FEATURES, build_features, matrix

log = logging.getLogger(__name__)
CACHE_DIR = Path("results/anomaly/cache")


# ── features (cached) ──────────────────────────────────────────────────────

def _features_company(g: pd.DataFrame) -> pd.DataFrame:
    return build_features(g)


def corpus_features(corpus_dir: Path, ident: dict[str, Any], n_jobs: int,
                    cache_dir: Path = CACHE_DIR) -> tuple[pd.DataFrame, str]:
    """(features with company_id, categoriser version used for pred_category)."""
    rows = load_rows(corpus_dir)
    cats, cat_ver = predict_categories(rows, cache_dir, ident["corpus_hash"], n_jobs=n_jobs)
    cache = cache_dir / f"features-v{FEATURE_VERSION}-{ident['corpus_hash'][:8]}-{cat_ver}.parquet"
    if cache.exists():
        return pd.read_parquet(cache), cat_ver
    rows = rows.assign(pred_category=rows.txn_id.map(cats))
    if rows.pred_category.isna().all():
        raise RuntimeError("no predicted categories")
    groups = [g for _, g in rows.groupby("company_id", sort=True)]
    t0 = time.perf_counter()
    if n_jobs > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            parts = list(ex.map(_features_company, groups, chunksize=8))
    else:
        parts = [_features_company(g) for g in groups]
    feats = pd.concat(parts, ignore_index=True)
    feats = feats.merge(rows[["txn_id", "company_id"]], on="txn_id", how="left", validate="one_to_one")
    log.info("features built", extra={"rows": len(feats), "seconds": round(time.perf_counter() - t0, 1)})
    cache_dir.mkdir(parents=True, exist_ok=True)
    feats.to_parquet(cache, index=False)
    return feats, cat_ver


# ── parallel scoring over accounts ─────────────────────────────────────────

def _score_chunk(args: tuple[AnomalyModel, pd.DataFrame, str | None]) -> pd.DataFrame:
    model, feats, force = args
    return score_all(model, feats, force=force)


def score_parallel(model: AnomalyModel, feats: pd.DataFrame, force: str | None,
                   n_jobs: int) -> pd.DataFrame:
    accts = sorted(feats.account_id.unique())
    if n_jobs <= 1 or len(accts) < 2:
        out = score_all(model, feats, force=force)
    else:
        chunks = [accts[i::n_jobs * 4] for i in range(n_jobs * 4)]
        jobs = [(model, feats[feats.account_id.isin(c)], force) for c in chunks if c]
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            out = pd.concat(list(ex.map(_score_chunk, jobs)), ignore_index=True)
    meta = feats[["txn_id", "account_id", "company_id"]]
    return out.merge(meta, on="txn_id", how="left", validate="one_to_one")


def combine(per_acct: pd.DataFrame, pooled: pd.DataFrame, n_rows: pd.Series,
            min_history: int) -> pd.DataFrame:
    """Hybrid scores: per-account if the account has >= min_history rows, else pooled."""
    long_accts = set(n_rows[n_rows >= min_history].index)
    a = per_acct[per_acct.account_id.isin(long_accts)]
    b = pooled[~pooled.account_id.isin(long_accts)]
    return pd.concat([a, b], ignore_index=True)


# ── selection on validation ────────────────────────────────────────────────

def severity_cutoffs(scored: pd.DataFrame, labels: np.ndarray, flagged: np.ndarray,
                     sel: dict[str, Any], contamination: float) -> dict[str, Any]:
    g = sel["severity_grid"]
    grid = np.round(np.arange(1.0 - contamination, g["stop"] + 1e-12, g["step"]), 6)
    pct = scored["percentile"].to_numpy()
    rows = []
    for c in grid:
        m = flagged & (pct >= c)
        n = int(m.sum())
        rows.append((float(c), n, float(labels[m].mean()) if n else float("nan")))

    def pick(target: float, default: float) -> tuple[float, bool]:
        for c, n, p in rows:
            if n >= 5 and p == p and p >= target:
                return c, False
        return default, True

    high, high_fb = pick(sel["target_precision_high"], sel["default_severity_high_pct"])
    med, med_fb = pick(sel["target_precision_medium"], sel["default_severity_medium_pct"])
    if med > high:
        med = high
    return {"severity_high_pct": high, "severity_medium_pct": med,
            "high_is_fallback": high_fb, "medium_is_fallback": med_fb,
            "curve": [{"cutoff": c, "n": n, "precision": p} for i, (c, n, p) in enumerate(rows)
                      if i == 0 or n != rows[i - 1][1]]}


def select_on_val(per_acct: pd.DataFrame, pooled: pd.DataFrame, n_rows: pd.Series,
                  labels: pd.DataFrame, sel: dict[str, Any]) -> dict[str, Any]:
    report: dict[str, Any] = {"min_history": [], "contamination": []}
    best: tuple[Any, ...] | None = None
    for mh in sel["min_history_grid"]:
        s = combine(per_acct, pooled, n_rows, mh).merge(labels[["txn_id", "any"]], on="txn_id")
        m = at_k(s, "percentile", "any", [20])
        ap = average_precision(s.percentile.to_numpy(), s["any"].to_numpy())
        n_pooled = int((s.scorer == SCORER_POOLED).sum())
        report["min_history"].append({"min_history": mh, "micro_p@20": m["p@20_micro"],
                                      "average_precision": ap, "rows_pooled": n_pooled,
                                      "accounts_pooled": int((n_rows < mh).sum())})
        key = (m["p@20_micro"], ap, -mh)
        if best is None or key > best[0]:
            best = (key, mh)
    assert best is not None
    mh = int(best[1])
    s = combine(per_acct, pooled, n_rows, mh).merge(labels[["txn_id", "any"]], on="txn_id")
    y = s["any"].to_numpy()
    best_c: tuple[Any, ...] | None = None
    for c in sel["contamination_grid"]:
        flagged = flag_mask(s, c, sel["max_flags_per_statement"])
        sm = set_metrics(flagged, y)
        report["contamination"].append({"contamination": c, **sm})
        key_c = (round(sm["f1"], 6), -c)
        if best_c is None or key_c > best_c[0]:
            best_c = (key_c, c)
    assert best_c is not None
    c = float(best_c[1])
    flagged = flag_mask(s, c, sel["max_flags_per_statement"])
    sev = severity_cutoffs(s, y, flagged, sel, c)
    report["severity"] = sev
    return {"min_history": mh, "contamination": c,
            "max_flags_per_statement": int(sel["max_flags_per_statement"]),
            "severity_high_pct": sev["severity_high_pct"],
            "severity_medium_pct": sev["severity_medium_pct"],
            "severity_fallback": {"high": sev["high_is_fallback"], "medium": sev["medium_is_fallback"]},
            "report": report}


# ── main ───────────────────────────────────────────────────────────────────

def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()[:6]


def model_version(corpus_hash: str, params: dict[str, Any]) -> str:
    return f"anom1-pooled-company-{corpus_hash[:8]}-{params_hash(params)}"


def train(corpus_dir: Path, *, allow_unpinned: bool, profile: str, results: Path,
          n_jobs: int) -> dict[str, Any]:
    cfg = load_config()
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    t0 = time.perf_counter()
    feats, cat_ver = corpus_features(corpus_dir, ident, n_jobs)
    split = feats.company_id.map(company_splits(corpus_dir))
    if split.isna().any():
        raise RuntimeError("rows without a company split")
    tr, va = feats[split == "train"], feats[split == "val"]

    fcfg = dict(cfg["forest"])
    if profile == "ci":
        fcfg["n_estimators"] = cfg["ci_profile"]["n_estimators"]
    params = ForestParams(n_estimators=fcfg["n_estimators"], max_samples=fcfg["max_samples"],
                          seed=fcfg["seed"])
    forest, q, med, iqr = fit_pooled(matrix(tr, STD_FEATURES), params)
    t_fit = time.perf_counter() - t0

    sel_cfg = cfg["selection"]
    base = AnomalyModel(pooled=forest, pooled_ref_quantiles=q, pooled_feature_median=med,
                        pooled_feature_iqr=iqr, params=params, min_history=0,
                        feature_version=FEATURE_VERSION)
    n_rows = va.groupby("account_id").size()
    selected: dict[str, Any]
    if profile == "ci":
        selected = {"min_history": int(cfg["ci_profile"]["min_history"]),
                    "contamination": float(cfg["ci_profile"]["contamination"]),
                    "max_flags_per_statement": int(sel_cfg["max_flags_per_statement"]),
                    "severity_high_pct": float(sel_cfg["default_severity_high_pct"]),
                    "severity_medium_pct": float(sel_cfg["default_severity_medium_pct"]),
                    "severity_fallback": {"high": True, "medium": True},
                    "report": {"note": "ci profile: no validation search"}}
    else:
        labels = load_eval_labels(corpus_dir).frame
        per_acct = score_parallel(base, va, SCORER_PER_ACCOUNT, n_jobs)
        pooled = score_parallel(base, va, SCORER_POOLED, n_jobs)
        selected = select_on_val(per_acct, pooled, n_rows, labels, sel_cfg)

    model = AnomalyModel(pooled=forest, pooled_ref_quantiles=q, pooled_feature_median=med,
                         pooled_feature_iqr=iqr, params=params,
                         min_history=selected["min_history"], feature_version=FEATURE_VERSION,
                         selected={k: v for k, v in selected.items() if k != "report"})
    hp = {"forest": fcfg, "feature_version": FEATURE_VERSION, "categoriser": cat_ver,
          "profile": profile, **{k: v for k, v in selected.items() if k not in ("report",)}}
    version = model_version(ident["corpus_hash"], hp)
    meta = {"algo": "isolation_forest", "split": "company", "params": hp, "corpus": ident,
            "n_train_rows": int(len(tr)), "n_train_companies": int(tr.company_id.nunique()),
            "n_val_rows": int(len(va)), "n_val_companies": int(va.company_id.nunique()),
            "selected_on_val": selected, "category_source": cat_ver,
            "features_raw": list(model.raw_features), "features_std": list(model.std_features),
            "synthetic": True, "fit_seconds": round(t_fit, 1)}
    path = registry.save(model, name=MODEL_NAME, version=version, metadata=meta)
    results.mkdir(parents=True, exist_ok=True)
    out = {"model_version": version, "path": str(path), "corpus": ident,
           "category_source": cat_ver, "selected": selected}
    name = "selection_ci.json" if profile == "ci" else "selection.json"
    (results / name).write_text(json.dumps(out, indent=2, default=float) + "\n",
                                encoding="utf-8", newline="\n")
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--profile", choices=["full", "ci"], default="full")
    ap.add_argument("--results", type=Path, default=Path("results/anomaly"))
    ap.add_argument("--n-jobs", type=int, default=3)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = train(a.corpus, allow_unpinned=a.allow_unpinned, profile=a.profile,
                results=a.results, n_jobs=a.n_jobs)
    sel = out["selected"]
    print(json.dumps({k: v for k, v in sel.items() if k != "report"}, indent=2, default=float))
    if "report" in sel and isinstance(sel["report"], dict) and "min_history" in sel["report"]:
        print("min_history (val):")
        for r in sel["report"]["min_history"]:
            print("  ", r)
        print("contamination (val):")
        for r in sel["report"]["contamination"]:
            print("  ", {k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()})
    print(f"ANOMALY_MODEL_VERSION={out['model_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
