"""Train the churn model; every choice is made on TRAIN with grouped CV.

Usage:
    python -m ml.churn.train data/corpus/v1 [--allow-unpinned] [--profile full|ci]
                                            [--results results/churn] [--n-jobs 3]

Steps:
1. Hash gate (``ml/churn/config.yaml`` pinned_corpora).
2. Units (company, customer key, cutoff) from predicted-REVENUE payments,
   features <= cutoff, label from (cutoff, cutoff+90d]. No ``labels/`` read.
3. 5-fold GroupKFold (groups = company) on TRAIN units for each candidate.
   Out-of-fold (OOF) scores choose the model (PR-AUC on active units), the
   Platt calibrator, the decision threshold (max F1), the medium band
   (OOF active prevalence) and the serving ``min_history_payments``.
4. Pre-registered validation check: paired company-bootstrap CI of
   (PR-AUC winner - PR-AUC best LR) on val active units; if it lies entirely
   below 0, the LR is used instead. Val changes nothing else.
5. Refit the chosen candidate on all TRAIN units, register it, and write
   ``results/churn/selection[_ci].json``.

Parallelism: the process pool in feature building only; every estimator runs
with one thread of Python work (no nested pools).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml import registry
from ml.churn import MODEL_NAME
from ml.churn.data import check_gate, load_config, load_units
from ml.churn.metrics import best_f1_threshold, bootstrap_ci, pr_auc, roc_auc
from ml.churn.model import ChurnModel, fit_calibrator, make_estimator
from ml.features.churn_features import FEATURE_VERSION, FEATURES, is_active

log = logging.getLogger(__name__)


def oof_scores(units: pd.DataFrame, cand: dict[str, Any], folds: int, seed: int) -> np.ndarray:
    from sklearn.model_selection import GroupKFold

    x = units[list(FEATURES)].to_numpy(dtype=float)
    y = units["y"].to_numpy()
    out = np.full(len(units), np.nan)
    for tr, te in GroupKFold(n_splits=folds).split(x, y, groups=units["company_id"].to_numpy()):
        est = make_estimator(cand, seed)
        est.fit(x[tr], y[tr])
        out[te] = est.predict_proba(x[te])[:, 1]
    return out


def choose_min_history(units: pd.DataFrame, score: np.ndarray, grid: list[int]) -> dict[str, Any]:
    """Smallest m whose OOF bucket [m, next) has model AP > the days-since-last rule's AP."""
    act = is_active(units).to_numpy()
    n = units["n_payments"].to_numpy()
    y = units["y"].to_numpy()
    rule = units["days_since_last"].to_numpy()
    rows = []
    chosen = None
    for i, m in enumerate(grid):
        hi = grid[i + 1] if i + 1 < len(grid) else np.inf
        b = act & (n >= m) & (n < hi)
        ap_m, ap_r = pr_auc(y[b], score[b]), pr_auc(y[b], rule[b])
        rows.append({"bucket": f"[{m},{'inf' if hi == np.inf else int(hi)})", "n": int(b.sum()),
                     "pos_rate": float(y[b].mean()) if b.any() else float("nan"),
                     "model_ap": ap_m, "rule_ap": ap_r})
        if chosen is None and ap_m == ap_m and ap_r == ap_r and ap_m > ap_r:
            chosen = m
    return {"min_history_payments": int(chosen if chosen is not None else grid[-1]),
            "fallback": chosen is None, "buckets": rows}


def calibrate_and_threshold(units: pd.DataFrame, raw: np.ndarray) -> dict[str, Any]:
    act = is_active(units).to_numpy()
    y = units["y"].to_numpy()
    cal = fit_calibrator(raw[act], y[act])
    from ml.churn.model import logit

    p = cal.predict_proba(logit(raw).reshape(-1, 1))[:, 1]
    thr, f1 = best_f1_threshold(y[act], p[act])
    return {"calibrator": cal, "calibrated": p, "decision_threshold": round(thr, 4),
            "oof_f1_at_threshold": f1, "band_medium": round(float(y[act].mean()), 4)}


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:6]


def model_version(name: str, corpus_hash: str, params: dict[str, Any]) -> str:
    return f"churn1-{name}-{corpus_hash[:8]}-{params_hash(params)}"


def val_check(tr: pd.DataFrame, va: pd.DataFrame, win: dict[str, Any], lr: dict[str, Any],
              seed: int, n_boot: int) -> dict[str, Any]:
    va = va[is_active(va)].reset_index(drop=True)
    if va.empty or va["y"].nunique() < 2:
        return {"applied": False, "reason": "no usable validation units", "switch_to_lr": False}
    x_tr, y_tr = tr[list(FEATURES)].to_numpy(dtype=float), tr["y"].to_numpy()
    frame = va[["company_id", "y"]].copy()
    for tag, cand in (("win", win), ("lr", lr)):
        est = make_estimator(cand, seed)
        est.fit(x_tr, y_tr)
        frame[tag] = est.predict_proba(va[list(FEATURES)].to_numpy(dtype=float))[:, 1]

    def ap(col: str) -> Callable[[pd.DataFrame], float]:
        return lambda f: pr_auc(f["y"].to_numpy(), f[col].to_numpy())

    def diff(f: pd.DataFrame) -> float:
        return pr_auc(f["y"].to_numpy(), f["win"].to_numpy()) - pr_auc(f["y"].to_numpy(), f["lr"].to_numpy())

    d = bootstrap_ci(frame, diff, n_boot, seed)
    switch = win["name"] != lr["name"] and d["hi"] < 0
    return {"applied": True, "n_units": int(len(frame)), "n_companies": int(frame.company_id.nunique()),
            "pos_rate": float(frame.y.mean()),
            "winner": {"name": win["name"], **bootstrap_ci(frame, ap("win"), n_boot, seed)},
            "best_lr": {"name": lr["name"], **bootstrap_ci(frame, ap("lr"), n_boot, seed)},
            "diff_win_minus_lr": d, "switch_to_lr": bool(switch)}


def train(corpus_dir: Path, *, allow_unpinned: bool, profile: str, results: Path,
          n_jobs: int) -> dict[str, Any]:
    cfg = load_config()
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    units, cat_ver = load_units(corpus_dir, ident, n_jobs)
    tr = units[units["split"] == "train"].reset_index(drop=True)
    va = units[units["split"] == "val"].reset_index(drop=True)
    if tr.empty or tr["y"].nunique() < 2:
        raise RuntimeError("not enough training units")
    seed = int(cfg["cv"]["seed"])
    if profile == "ci":
        cands, folds = [cfg["ci_profile"]["candidate"]], int(cfg["ci_profile"]["folds"])
    else:
        cands, folds = list(cfg["candidates"]), int(cfg["cv"]["folds"])
    folds = max(2, min(folds, tr["company_id"].nunique()))

    act = is_active(tr).to_numpy()
    y = tr["y"].to_numpy()
    cv_rows, oof = [], {}
    for cand in cands:
        t0 = time.perf_counter()
        s = oof_scores(tr, cand, folds, seed)
        oof[cand["name"]] = s
        cv_rows.append({"name": cand["name"], "algo": cand["algo"],
                        "pr_auc_active": pr_auc(y[act], s[act]), "pr_auc_all": pr_auc(y, s),
                        "roc_auc_active": roc_auc(y[act], s[act]),
                        "seconds": round(time.perf_counter() - t0, 1)})
        log.info("cv candidate done", extra={"candidate": cand["name"]})
    by_name = {c["name"]: c for c in cands}
    win = max(cv_rows, key=lambda r: (r["pr_auc_active"], r["algo"] == "lr"))["name"]
    lr_rows = [r for r in cv_rows if r["algo"] == "lr"]
    best_lr = max(lr_rows, key=lambda r: r["pr_auc_active"])["name"] if lr_rows else win

    check: dict[str, Any] = {"applied": False, "reason": "ci profile", "switch_to_lr": False}
    if profile != "ci":
        check = val_check(tr, va, by_name[win], by_name[best_lr], seed,
                          int(cfg["selection"]["val_check_bootstrap"]))
    chosen = best_lr if check.get("switch_to_lr") else win
    cand = by_name[chosen]

    cal = calibrate_and_threshold(tr, oof[chosen])
    mh = choose_min_history(tr, oof[chosen], list(cfg["selection"]["min_history_grid"]))

    t0 = time.perf_counter()
    est = make_estimator(cand, seed)
    est.fit(tr[list(FEATURES)].to_numpy(dtype=float), y)
    fit_s = time.perf_counter() - t0

    selected = {"candidate": chosen, "decision_threshold": cal["decision_threshold"],
                "band_medium": cal["band_medium"],
                "min_history_payments": mh["min_history_payments"],
                "rule_silence_days": int(cfg["selection"]["rule_silence_days"]),
                "oof_f1_at_threshold": cal["oof_f1_at_threshold"]}
    model = ChurnModel(estimator=est, calibrator=cal["calibrator"], features=FEATURES,
                       feature_version=FEATURE_VERSION, candidate=dict(cand), selected=selected)
    hp = {"candidate": cand, "folds": folds, "seed": seed, "feature_version": FEATURE_VERSION,
          "categoriser": cat_ver, "profile": profile, **selected}
    version = model_version(chosen, ident["corpus_hash"], hp)
    sizes = {s: {"units": int((units.split == s).sum()),
                 "active_units": int(((units.split == s) & is_active(units)).sum()),
                 "companies": int(units.loc[units.split == s, "company_id"].nunique()),
                 "company_cutoffs": int(units.loc[units.split == s, ["company_id", "cutoff_date"]]
                                        .drop_duplicates().shape[0]),
                 "pos_rate": float(units.loc[units.split == s, "y"].mean()) if (units.split == s).any() else None,
                 "pos_rate_active": float(units.loc[(units.split == s) & is_active(units), "y"].mean())
                 if ((units.split == s) & is_active(units)).any() else None}
             for s in ("train", "val", "test")}
    meta = {"algo": cand["algo"], "split": "churn_temporal", "params": hp, "corpus": ident,
            "category_source": cat_ver, "identity": "narration_counterparty_key",
            "label": "customer makes no payment in (cutoff, cutoff+90d] given >=2 payments before cutoff",
            "features": list(FEATURES), "sizes": sizes, "cv": cv_rows, "val_check": check,
            "min_history_buckets": mh["buckets"], "selected": selected,
            "synthetic": True, "fit_seconds": round(fit_s, 1)}
    path = registry.save(model, name=MODEL_NAME, version=version, metadata=meta)
    results.mkdir(parents=True, exist_ok=True)
    out = {"model_version": version, "path": str(path), "corpus": ident, "category_source": cat_ver,
           "sizes": sizes, "cv": cv_rows, "val_check": check, "min_history_buckets": mh["buckets"],
           "selected": selected}
    name = "selection_ci.json" if profile == "ci" else "selection.json"
    (results / name).write_text(json.dumps(out, indent=2, default=float) + "\n",
                                encoding="utf-8", newline="\n")
    return out


def _r(v: Any) -> Any:
    return round(v, 4) if isinstance(v, float) else v


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--profile", choices=["full", "ci"], default="full")
    ap.add_argument("--results", type=Path, default=Path("results/churn"))
    ap.add_argument("--n-jobs", type=int, default=3)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = train(a.corpus, allow_unpinned=a.allow_unpinned, profile=a.profile,
                results=a.results, n_jobs=a.n_jobs)
    print("sizes:")
    for s, v in out["sizes"].items():
        print("  ", s, {k: _r(x) for k, x in v.items()})
    print("grouped CV on train (OOF):")
    for r in out["cv"]:
        print("  ", {k: _r(v) for k, v in r.items()})
    print("val check:", json.dumps(out["val_check"], default=float))
    print("min_history buckets (OOF, active):")
    for r in out["min_history_buckets"]:
        print("  ", {k: _r(v) for k, v in r.items()})
    print("selected:", json.dumps(out["selected"], default=float))
    print(f"CHURN_MODEL_VERSION={out['model_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
