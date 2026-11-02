"""Fit company segmentation on the FIT companies; label-free, pre-registered selection.

Usage:
    python -m ml.segmentation.train data/corpus/v1 [--allow-unpinned] [--profile full|ci]
                                                   [--results results/segmentation] [--n-jobs 3]

Steps (rule in ml/segmentation/config.yaml):
1. Hash gate; company features from pipeline outputs (predicted categories).
2. Log transforms, StandardScaler fitted on FIT companies only.
3. K-Means k in k_range: inertia, silhouette, bootstrap Jaccard per cluster.
   k* = best silhouette among k with mean Jaccard >= stable_jaccard.
4. Compare GMM and agglomerative (Ward) at k* and at their own best k.
5. Final = best silhouette among stable candidates (ties -> kmeans). PCA(2).
6. Register; write results/segmentation/selection[_ci].json.

Holdout companies and the generator profile are not touched here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from ml import registry
from ml.features.company_features import FEATURE_VERSION, FEATURES, transform
from ml.segmentation import MODEL_NAME
from ml.segmentation.data import check_gate, company_features, load_config, segmentation_split
from ml.segmentation.model import (
    SegmentationModel,
    bootstrap_jaccard,
    centroids_of,
    fit_labels,
    name_cluster,
)

log = logging.getLogger(__name__)
ALGO_ORDER = {"kmeans": 0, "gmm": 1, "agglomerative": 2}


def _stability(algo: str, x: np.ndarray, labels: np.ndarray, k: int, seed: int,
               n_boot: int, n_init: int) -> dict[int, float]:
    def refit(xb: np.ndarray, b: int) -> np.ndarray:
        return fit_labels(algo, xb, k, seed + 1000 + b, n_init)[1]

    return bootstrap_jaccard(x, labels, refit, n_boot, seed)


def _silhouette(x: np.ndarray, labels: np.ndarray) -> float:
    from sklearn.metrics import silhouette_score

    return float(silhouette_score(x, labels)) if len(set(labels.tolist())) > 1 else float("nan")


def select(x: np.ndarray, cfg: dict[str, Any], k_range: list[int], n_boot: int) -> dict[str, Any]:
    seed, n_init, thr = int(cfg["seed"]), int(cfg["kmeans_n_init"]), float(cfg["stable_jaccard"])
    ks = list(range(k_range[0], k_range[1] + 1))
    per_k: dict[str, list[dict[str, Any]]] = {"kmeans": [], "gmm": [], "agglomerative": []}
    cache: dict[tuple[str, int], dict[str, Any]] = {}

    def run(algo: str, k: int, stability: bool) -> dict[str, Any]:
        key = (algo, k)
        if key not in cache:
            est, lab = fit_labels(algo, x, k, seed, n_init)
            r: dict[str, Any] = {"algo": algo, "k": k, "silhouette": _silhouette(x, lab),
                                 "sizes": np.bincount(lab, minlength=k).tolist(), "_est": est, "_lab": lab}
            if algo == "kmeans":
                r["inertia"] = float(est.inertia_)
            if algo == "gmm":
                r["bic"] = float(est.bic(x))
            cache[key] = r
        r = cache[key]
        if stability and "jaccard" not in r:
            j = _stability(algo, x, r["_lab"], k, seed, n_boot, n_init)
            r["jaccard"] = {int(c): v for c, v in j.items()}
            r["mean_jaccard"] = float(np.nanmean(list(j.values())))
            r["min_jaccard"] = float(np.nanmin(list(j.values())))
        return r

    t0 = time.perf_counter()
    for k in ks:
        per_k["kmeans"].append(run("kmeans", k, True))
        per_k["gmm"].append(run("gmm", k, False))
        per_k["agglomerative"].append(run("agglomerative", k, False))
    km = per_k["kmeans"]
    stable_k = [r for r in km if r["mean_jaccard"] >= thr]
    k_fallback = not stable_k
    k_star = max(stable_k or km, key=lambda r: (r["silhouette"], -r["k"]))["k"]
    gmm_bic_k = min(per_k["gmm"], key=lambda r: (r["bic"], r["k"]))["k"]
    agg_sil_k = max(per_k["agglomerative"], key=lambda r: (r["silhouette"], -r["k"]))["k"]
    cand_keys = sorted({("kmeans", k_star), ("gmm", k_star), ("gmm", gmm_bic_k),
                        ("agglomerative", k_star), ("agglomerative", agg_sil_k)},
                       key=lambda t: (ALGO_ORDER[t[0]], t[1]))
    cands = [run(a, k, True) for a, k in cand_keys]
    stable_c = [c for c in cands if c["mean_jaccard"] >= thr]
    final_fallback = not stable_c
    final = max(stable_c or [cache[("kmeans", k_star)]],
                key=lambda c: (c["silhouette"], -ALGO_ORDER[c["algo"]]))

    def public(r: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in r.items() if not k.startswith("_")}

    return {"k_star": k_star, "k_star_fallback": k_fallback, "gmm_best_bic_k": gmm_bic_k,
            "agglomerative_best_silhouette_k": agg_sil_k,
            "per_k": {a: [public(r) for r in rs] for a, rs in per_k.items()},
            "candidates": [public(c) for c in cands],
            "final": public(final), "final_fallback": final_fallback,
            "_final": final, "seconds": round(time.perf_counter() - t0, 1)}


def params_hash(params: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(params, sort_keys=True, default=str).encode()).hexdigest()[:6]


def train(corpus_dir: Path, *, allow_unpinned: bool, profile: str, results: Path,
          n_jobs: int) -> dict[str, Any]:
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler

    cfg = load_config()
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    feats, cat_ver = company_features(corpus_dir, ident, n_jobs)
    split = feats["company_id"].map(segmentation_split(corpus_dir))
    if split.isna().any():
        raise RuntimeError("companies without a segmentation split")
    fit = feats[split == "fit"].reset_index(drop=True)
    scaler = StandardScaler().fit(transform(fit))
    x = scaler.transform(transform(fit))
    if profile == "ci":
        k_range, n_boot = list(cfg["ci_profile"]["k_range"]), int(cfg["ci_profile"]["bootstrap"])
    else:
        k_range, n_boot = list(cfg["k_range"]), int(cfg["bootstrap"])
    k_range[1] = min(k_range[1], len(fit) - 1)
    sel = select(x, cfg, k_range, n_boot)
    final = sel.pop("_final")
    algo, k, lab = final["algo"], int(final["k"]), final["_lab"]
    cents = centroids_of(x, lab, k)
    thr = float(cfg["stable_jaccard"])
    stability = {int(c): float(v) for c, v in final["jaccard"].items()}
    stable = {c: stability.get(c, 0.0) >= thr for c in range(k)}
    names = {c: (name_cluster(cents[c]) if stable[c] else f"Segment {c} (unstable)") for c in range(k)}
    pca = PCA(n_components=2, random_state=int(cfg["seed"])).fit(x)
    selected = {"algo": algo, "k": k, "stable_jaccard": thr, "n_boot": n_boot,
                "k_star": sel["k_star"], "final_fallback": sel["final_fallback"]}
    model = SegmentationModel(algo=algo, k=k, scaler=scaler, estimator=final["_est"], centroids=cents,
                              pca=pca, stability=stability, names=names, stable=stable,
                              feature_version=FEATURE_VERSION, selected=selected)
    hp = {"feature_version": FEATURE_VERSION, "categoriser": cat_ver, "profile": profile,
          "seed": cfg["seed"], "k_range": k_range, **selected}
    version = f"seg1-{algo}-k{k}-{ident['corpus_hash'][:8]}-{params_hash(hp)}"
    meta = {"algo": algo, "split": "segmentation_holdout", "params": hp, "corpus": ident,
            "category_source": cat_ver, "features": list(FEATURES), "n_fit": int(len(fit)),
            "selection": sel, "names": {str(c): n for c, n in names.items()},
            "stability": {str(c): v for c, v in stability.items()},
            "pca_explained_variance": [float(v) for v in pca.explained_variance_ratio_],
            "synthetic": True}
    path = registry.save(model, name=MODEL_NAME, version=version, metadata=meta)
    results.mkdir(parents=True, exist_ok=True)
    out = {"model_version": version, "path": str(path), "corpus": ident, "selection": sel,
           "names": meta["names"], "stability": meta["stability"],
           "pca_explained_variance": meta["pca_explained_variance"]}
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
    ap.add_argument("--results", type=Path, default=Path("results/segmentation"))
    ap.add_argument("--n-jobs", type=int, default=3)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = train(a.corpus, allow_unpinned=a.allow_unpinned, profile=a.profile,
                results=a.results, n_jobs=a.n_jobs)
    sel = out["selection"]
    print("K-Means per k (fit set):")
    for r in sel["per_k"]["kmeans"]:
        print("  ", {k: _r(r[k]) for k in ("k", "inertia", "silhouette", "mean_jaccard", "min_jaccard")})
    print("GMM per k:", [(r["k"], _r(r["silhouette"]), round(r["bic"], 1)) for r in sel["per_k"]["gmm"]])
    print("Agglomerative per k:", [(r["k"], _r(r["silhouette"])) for r in sel["per_k"]["agglomerative"]])
    print("candidates:")
    for c in sel["candidates"]:
        print("  ", {k: _r(c[k]) for k in ("algo", "k", "silhouette", "mean_jaccard", "min_jaccard")})
    print("final:", {k: _r(sel["final"][k]) for k in ("algo", "k", "silhouette", "mean_jaccard")},
          "k*:", sel["k_star"], "fallbacks:", sel["k_star_fallback"], sel["final_fallback"])
    print("names:", json.dumps(out["names"]))
    print("stability:", {k: round(v, 3) for k, v in out["stability"].items()})
    print(f"SEGMENTATION_MODEL_VERSION={out['model_version']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
