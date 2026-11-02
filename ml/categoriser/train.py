"""Train and select categoriser models on the leakage-safe corpus splits.

    python -m ml.categoriser.train data/corpus/v1 [--split company|cross_bank|unseen_vendor|all]
                                   [--models lr,svc_cal,lgbm,minilm] [--allow-unpinned] [--profile ci]

Per split: fit each candidate on TRAIN, pick hyper-parameters and the winning
algorithm on VALIDATION macro-F1, and register the winner in ml/registry. Test
metrics for every candidate are computed once and written for the comparison
table. They are never used for any choice.

``--profile ci`` trains a single LR (C=1) on the company split, for CI's
small corpus, and prints ``CATEGORISER_MODEL_VERSION=<version>``.

Every number this writes is on SYNTHETIC data (generator v2.0.0, corpus v1).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import scipy.sparse as sp
from sklearn.base import clone
from sklearn.calibration import CalibratedClassifierCV
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.svm import LinearSVC

from ml import registry
from ml.categoriser import MODEL_NAME
from ml.categoriser.data import CorpusData, load_config, load_corpus
from ml.categoriser.metrics import macro_f1, summary
from ml.features.categoriser import FEATURE_VERSION, build_frame, make_column_transformer

log = logging.getLogger(__name__)
RESULTS = Path("results/categoriser")
SPLITS = ("company", "cross_bank", "unseen_vendor")


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:6]


def model_version(algo: str, split: str, corpus_hash: str, params: dict[str, Any]) -> str:
    return f"cat3-{algo}-{split}-{corpus_hash[:8]}-{params_hash(params)}"


def cap_per_class(y: np.ndarray, idx: np.ndarray, cap: int | None, seed: int) -> np.ndarray:
    if not cap:
        return idx
    rng = np.random.default_rng(seed)
    keep = []
    for c in np.unique(y[idx]):
        ci = idx[y[idx] == c]
        keep.append(ci if len(ci) <= cap else np.sort(rng.choice(ci, cap, replace=False)))
    return np.sort(np.concatenate(keep))


# ── candidate fitters: (X_train, y_train, X_val, y_val) → fitted clf, params ─

# Worker budget for one process (set from --n-jobs). Grid points are fitted in
# parallel (joblib/loky processes; large arrays are memory-mapped, not copied).
N_JOBS = 4


def _fit_one(make: Any, C: float, Xtr: Any, ytr: np.ndarray, Xva: Any, yva: np.ndarray,
             ) -> tuple[Any, float, float, bool]:
    import warnings

    from sklearn.exceptions import ConvergenceWarning
    t = time.perf_counter()
    clf = make(C)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always", ConvergenceWarning)
        clf.fit(Xtr, ytr)
    converged = not any(issubclass(x.category, ConvergenceWarning) for x in w)
    f1 = macro_f1(yva, np.asarray(clf.predict(Xva)))
    return clf, f1, round(time.perf_counter() - t, 1), converged


def _grid(name: str, make: Any, grid: list[float], Xtr: Any, ytr: np.ndarray, Xva: Any,
          yva: np.ndarray) -> tuple[Any, float, list[dict[str, Any]]]:
    from joblib import Parallel, delayed
    res = Parallel(n_jobs=min(N_JOBS, len(grid)))(
        delayed(_fit_one)(make, C, Xtr, ytr, Xva, yva) for C in grid)
    trials, best, best_f1, best_C = [], None, -1.0, None
    for C, (clf, f1, secs, conv) in zip(grid, res, strict=True):
        trials.append({"C": C, "val_macro_f1": f1, "fit_s": secs, "converged": conv})
        log.info("  %s C=%s val macro-F1=%.4f (%ss%s)", name, C, f1, secs, "" if conv else ", not converged")
        if f1 > best_f1:   # ties → the smaller (more regularised) C, listed first
            best, best_f1, best_C = clf, f1, C
    assert best is not None and best_C is not None
    if best_C == max(grid) and len(grid) > 1:
        log.warning("  %s: best C=%s is the top of the grid", name, best_C)
    return best, best_C, trials


class _LRFactory:
    def __init__(self, max_iter: int) -> None:
        self.max_iter = max_iter

    def __call__(self, C: float) -> Any:
        return LogisticRegression(C=C, class_weight="balanced", max_iter=self.max_iter)


class _SVCFactory:
    def __init__(self, cv: int) -> None:
        self.cv = cv

    def __call__(self, C: float) -> Any:
        return CalibratedClassifierCV(LinearSVC(C=C, class_weight="balanced"),
                                      method="sigmoid", cv=self.cv)
        # Folds run sequentially: the grid is already parallel, and nesting
        # (processes × grid × folds) exhausted Windows resources (WinError 1450).


def fit_lr(cfg: dict[str, Any], Xtr: Any, ytr: np.ndarray, Xva: Any, yva: np.ndarray,
           grid: list[float] | None = None) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    best, C, trials = _grid("lr", _LRFactory(cfg["max_iter"]), grid or cfg["C_grid"],
                            Xtr, ytr, Xva, yva)
    return best, {"C": C, "class_weight": "balanced", "max_iter": cfg["max_iter"]}, trials


def fit_svc_cal(cfg: dict[str, Any], Xtr: Any, ytr: np.ndarray, Xva: Any, yva: np.ndarray,
                ) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    # Stratified calibration folds need ≥ cv rows per class; shrink cv for tiny classes.
    min_count = int(pd.Series(ytr).value_counts().min())
    cv = max(2, min(cfg["calibration_cv"], min_count))
    if min_count < 2:
        raise ValueError("svc_cal needs ≥ 2 training rows per class")
    best, C, trials = _grid("svc_cal", _SVCFactory(cv), cfg["C_grid"], Xtr, ytr, Xva, yva)
    return best, {"C": C, "class_weight": "balanced", "calibration": "sigmoid", "cv": cv}, trials


def fit_lgbm(cfg: dict[str, Any], Xtr: Any, ytr: np.ndarray, Xva: Any, yva: np.ndarray,
             ) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    import lightgbm as lgb
    t = time.perf_counter()
    clf = lgb.LGBMClassifier(
        n_estimators=cfg["n_estimators"], learning_rate=cfg["learning_rate"],
        num_leaves=cfg["num_leaves"], class_weight="balanced", random_state=42,
        n_jobs=N_JOBS, verbose=-1,
    )
    # Validation rows of classes unseen in train would break early stopping.
    seen = np.isin(yva, np.unique(ytr))
    clf.fit(Xtr.astype(np.float32), ytr, eval_set=[(Xva[seen].astype(np.float32), yva[seen])],
            callbacks=[lgb.early_stopping(cfg["early_stopping_rounds"], verbose=False)])
    f1 = macro_f1(yva, np.asarray(clf.predict(Xva.astype(np.float32))))
    log.info("  lgbm best_iter=%s val macro-F1=%.4f", clf.best_iteration_, f1)
    params = {k: cfg[k] for k in ("n_estimators", "learning_rate", "num_leaves")}
    params["best_iteration"] = int(clf.best_iteration_ or cfg["n_estimators"])
    return clf, params, [{"val_macro_f1": f1, "fit_s": round(time.perf_counter() - t, 1)}]


# ── MiniLM sentence embeddings (evaluation only; never registered) ─────────

def minilm_embeddings(texts: pd.Series, model_name: str, cache: Path) -> np.ndarray:
    """Embeddings of the NORMALISED narration (refs/VPAs masked): 137k unique
    strings instead of 590k raw, and no reference-number noise."""
    uniq = pd.Series(texts.unique())
    key = hashlib.sha256("\n".join(uniq.tolist()).encode()).hexdigest()[:16]
    path = cache / f"minilm_{key}.npy"
    if path.exists():
        emb = np.load(path)
    else:
        import torch
        from sentence_transformers import SentenceTransformer
        torch.set_num_threads(N_JOBS)
        st = SentenceTransformer(model_name, device="cpu")
        t = time.perf_counter()
        emb = st.encode(uniq.tolist(), batch_size=512, show_progress_bar=False,
                        normalize_embeddings=True).astype(np.float32)
        log.info("  minilm encoded %d unique texts in %.0fs", len(uniq), time.perf_counter() - t)
        cache.mkdir(parents=True, exist_ok=True)
        np.save(path, emb)
    pos = pd.Series(np.arange(len(uniq)), index=uniq)
    out: np.ndarray = emb[pos.loc[texts].to_numpy()]
    return out


# ── one split ───────────────────────────────────────────────────────────────

def train_split(
    split: str, data: CorpusData, frame: pd.DataFrame, corpus_dir: Path, cfg: dict[str, Any],
    algos: list[str], profile: str,
) -> dict[str, Any]:
    masks = data.split_masks(corpus_dir, cfg["splits"][split])
    y = data.y
    seed = cfg["train"]["seed"]
    tr_idx = cap_per_class(y, np.flatnonzero(masks["train"]), cfg["train"]["max_rows_per_class"], seed)
    va_idx, te_idx = np.flatnonzero(masks["val"]), np.flatnonzero(masks["test"])
    log.info("[%s] train=%d val=%d test=%d", split, len(tr_idx), len(va_idx), len(te_idx))

    fc = cfg["features"]
    t = time.perf_counter()
    ct = make_column_transformer(fc["char_max_features"], fc["word_max_features"], fc["min_df"])
    Xtr = ct.fit_transform(frame.iloc[tr_idx]).tocsr()
    Xva = ct.transform(frame.iloc[va_idx]).tocsr()
    Xte = ct.transform(frame.iloc[te_idx]).tocsr()
    vec_s = time.perf_counter() - t
    log.info("[%s] features %d (%.0fs)", split, Xtr.shape[1], vec_s)
    ytr, yva, yte = y[tr_idx], y[va_idx], y[te_idx]

    fitters = {"lr": fit_lr, "svc_cal": fit_svc_cal, "lgbm": fit_lgbm}
    candidates: dict[str, dict[str, Any]] = {}
    fitted: dict[str, Any] = {}
    for algo in algos:
        if algo == "minilm":
            continue
        log.info("[%s] fitting %s", split, algo)
        grid = [1.0] if profile == "ci" else None
        if algo == "lr":
            clf, params, trials = fit_lr(cfg["models"]["lr"], Xtr, ytr, Xva, yva, grid)
        else:
            clf, params, trials = fitters[algo](cfg["models"][algo], Xtr, ytr, Xva, yva)
        Xv = Xva.astype(np.float32) if algo == "lgbm" else Xva
        Xt = Xte.astype(np.float32) if algo == "lgbm" else Xte
        candidates[algo] = {
            "params": params, "trials": trials,
            "val": summary(yva, clf.predict(Xv)),
            "test": summary(yte, clf.predict(Xt)),     # reported only, never used to choose
        }
        fitted[algo] = clf

    if "minilm" in algos and split in cfg["models"]["minilm"]["splits"] and "lr" in fitted:
        log.info("[%s] MiniLM embeddings + LR(C=%s)", split, fitted["lr"].C)
        emb = minilm_embeddings(frame["text"],
                                cfg["models"]["minilm"]["model_name"], Path(".cache/ml"))
        Etr, Eva, Ete = (sp.csr_matrix(emb[i]) for i in (tr_idx, va_idx, te_idx))
        clf = clone(fitted["lr"])
        clf.fit(sp.hstack([Xtr, Etr]).tocsr(), ytr)
        candidates["lr+minilm"] = {
            "params": {**candidates["lr"]["params"], "embeddings": cfg["models"]["minilm"]["model_name"]},
            "val": summary(yva, clf.predict(sp.hstack([Xva, Eva]).tocsr())),
            "test": summary(yte, clf.predict(sp.hstack([Xte, Ete]).tocsr())),
            "note": "evaluation only; needs the embedding model at inference, not registered",
        }

    # Winner on VALIDATION macro-F1 among registrable candidates.
    chosen = max(fitted, key=lambda a: candidates[a]["val"]["macro_f1"])
    out_versions = {}
    for algo, clf in fitted.items():
        params = {"algo": algo, **candidates[algo]["params"], "features": fc,
                  "feature_version": FEATURE_VERSION, "max_rows_per_class": cfg["train"]["max_rows_per_class"],
                  "seed": seed}
        ver = model_version(algo, split, data.identity["corpus_hash"], params)
        pipe = Pipeline([("features", ct), ("clf", clf)])
        registry.save(pipe, name=MODEL_NAME, version=ver, metadata={
            "algo": algo, "split": split, "params": params,
            "corpus": data.identity,
            "n_train": int(len(tr_idx)), "n_val": int(len(va_idx)), "n_test": int(len(te_idx)),
            "classes": [str(c) for c in clf.classes_],
            "metrics": {"val": candidates[algo]["val"], "test": candidates[algo]["test"]},
            "selected_on_val": algo == chosen,
            "synthetic": "generator v2.0.0, corpus " + str(data.identity.get("corpus_version")),
        })
        out_versions[algo] = ver
        log.info("[%s] registered %s", split, ver)
    return {
        "split": split, "n_train": int(len(tr_idx)), "n_val": int(len(va_idx)),
        "n_test": int(len(te_idx)), "n_features": int(Xtr.shape[1]),
        "vectorise_s": round(vec_s, 1), "candidates": candidates,
        "chosen_on_val": chosen, "versions": out_versions,
    }


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Train categoriser models (SYNTHETIC corpus).")
    ap.add_argument("corpus_dir", type=Path)
    ap.add_argument("--split", default="all", choices=[*SPLITS, "all"])
    ap.add_argument("--models", default="lr,svc_cal,lgbm,minilm")
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--profile", default="full", choices=["full", "ci"])
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--n-jobs", type=int, default=4,
                    help="parallel workers in this process (grid points, CV folds, LightGBM, torch)")
    a = ap.parse_args(argv)
    global N_JOBS
    N_JOBS = a.n_jobs
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = load_config()
    data = load_corpus(a.corpus_dir, cfg, a.allow_unpinned)
    log.info("corpus %s hash=%s splits=%s pinned=%s", data.identity["corpus_dir"],
             data.identity["corpus_hash"][:12], data.identity["splits_hash"][:12],
             data.identity["pinned"])
    t = time.perf_counter()
    frame = build_frame(data.rows)
    log.info("feature frame %d rows (%.0fs)", len(frame), time.perf_counter() - t)

    if a.profile == "ci":
        splits, algos = ["company"], ["lr"]
    else:
        splits = list(SPLITS) if a.split == "all" else [a.split]
        algos = [m.strip() for m in a.models.split(",") if m.strip()]

    a.results.mkdir(parents=True, exist_ok=True)
    for split in splits:
        res = train_split(split, data, frame, a.corpus_dir, cfg, algos, a.profile)
        res["corpus"] = data.identity
        name = f"selection_{split}{'_ci' if a.profile == 'ci' else ''}.json"
        (a.results / name).write_text(json.dumps(res, indent=2, default=str) + "\n",
                                      encoding="utf-8", newline="\n")
        log.info("[%s] chosen on val: %s", split, res["chosen_on_val"])
        if a.profile == "ci":
            print(f"CATEGORISER_MODEL_VERSION={res['versions']['lr']}")


if __name__ == "__main__":
    main()
