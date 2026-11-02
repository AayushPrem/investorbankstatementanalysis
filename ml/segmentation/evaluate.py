"""Evaluate a registered segmentation model on the HOLDOUT companies (once).

Usage:
    python -m ml.segmentation.evaluate data/corpus/v1 [--model-version V] [--write-thresholds]
    python -m ml.segmentation.evaluate data/corpus/v1-small --allow-unpinned --dry-run

Reads ``labels/companies.parquet:profile`` for EVALUATION only (adjusted Rand
index, crosstabs). ``bank`` comes from ``features/companies.parquet`` and is
crosstabbed too, to show whether clusters track the bank layout rather than
the business. Writes ``results/segmentation/<model_version>/``.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ml import registry
from ml.features.company_features import FEATURES
from ml.segmentation import MODEL_NAME
from ml.segmentation.data import check_gate, company_features, load_config, segmentation_split
from ml.segmentation.model import SegmentationModel, fit_labels

log = logging.getLogger(__name__)
THRESHOLDS_PATH = Path("config/thresholds.yaml")


def _ari(a: Any, b: Any) -> float:
    from sklearn.metrics import adjusted_rand_score

    return float(adjusted_rand_score(a, b))


def _sil(x: np.ndarray, lab: np.ndarray) -> float:
    from sklearn.metrics import silhouette_score

    return float(silhouette_score(x, lab)) if len(set(lab.tolist())) > 1 else float("nan")


def _plot(fit: pd.DataFrame, hold: pd.DataFrame, out: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    both = pd.concat([fit.assign(part="fit"), hold.assign(part="holdout")], ignore_index=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 5))
    for ax, col, title in ((axes[0], "segment", "by segment (model)"),
                           (axes[1], "profile", "by hidden profile (evaluation only)")):
        for i, (v, g) in enumerate(both.groupby(col, sort=True)):
            ax.scatter(g["pc1"], g["pc2"], s=10, alpha=0.6, label=str(v), color=f"C{i}")
        ax.set(xlabel="PC1", ylabel="PC2", title=title)
        ax.legend(fontsize=8)
    fig.suptitle("Company segmentation, PCA of scaled features (SYNTHETIC corpus)")
    fig.tight_layout()
    fig.savefig(out / "pca.png", dpi=120)
    plt.close(fig)


def evaluate(corpus_dir: Path, *, version: str, allow_unpinned: bool, dry_run: bool, n_jobs: int,
             out_root: Path) -> dict[str, Any]:
    cfg = load_config()
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    model: SegmentationModel = registry.load(MODEL_NAME, version).model
    feats, _ = company_features(corpus_dir, ident, n_jobs)
    split = feats["company_id"].map(segmentation_split(corpus_dir))
    prof = pd.read_parquet(corpus_dir / "labels" / "companies.parquet", columns=["company_id", "profile"])
    bank = pd.read_parquet(corpus_dir / "features" / "companies.parquet", columns=["company_id", "bank"])
    feats = feats.merge(prof, on="company_id").merge(bank, on="company_id")
    feats["split"] = split.to_numpy()
    feats["segment"] = model.predict(feats)
    pcs = model.project(feats)
    feats["pc1"], feats["pc2"] = pcs[:, 0], pcs[:, 1]
    fit, hold = feats[feats.split == "fit"], feats[feats.split == "holdout"]
    x_fit, x_hold = model.scale(fit), model.scale(hold)
    seed = int(cfg["seed"])

    res: dict[str, Any] = {"model_version": version, "corpus": ident, "dry_run": dry_run, "synthetic": True,
                           "algo": model.algo, "k": model.k, "n_fit": int(len(fit)), "n_holdout": int(len(hold))}
    res["silhouette"] = {"fit": _sil(x_fit, fit["segment"].to_numpy()),
                         "holdout": _sil(x_hold, hold["segment"].to_numpy())}
    res["ari_vs_profile"] = {"fit": _ari(fit["profile"], fit["segment"]),
                             "holdout": _ari(hold["profile"], hold["segment"])}
    res["ari_vs_bank"] = {"fit": _ari(fit["bank"], fit["segment"]),
                          "holdout": _ari(hold["bank"], hold["segment"])}
    if len(hold) > model.k:
        _, refit = fit_labels("kmeans", x_hold, model.k, seed, 10)
        res["holdout_transfer_ari"] = _ari(refit, hold["segment"])
    res["stability_jaccard"] = {str(c): v for c, v in model.stability.items()}
    res["names"] = {str(c): n for c, n in model.names.items()}
    # Diagnostic only (selection was fixed in train.py): ARI of K-Means at every k on the fit set.
    k_lo, k_hi = 2, min(10, len(fit) - 1)
    res["kmeans_ari_by_k_fit"] = {k: _ari(fit["profile"], fit_labels("kmeans", x_fit, k, seed, 10)[1])
                                  for k in range(k_lo, k_hi + 1)}
    res["crosstab_profile"] = pd.crosstab(feats["segment"], feats["profile"]).to_dict()
    res["crosstab_bank"] = pd.crosstab(feats["segment"], feats["bank"]).to_dict()
    cents = fit.groupby("segment")[list(FEATURES)].mean()
    res["centroids_raw_units_fit"] = cents.round(4).to_dict(orient="index")
    res["sizes"] = {"fit": fit["segment"].value_counts().sort_index().to_dict(),
                    "holdout": hold["segment"].value_counts().sort_index().to_dict()}

    # latency: assignment of one company (transform + scale + predict + PCA), features excluded
    row = feats.iloc[[0]]
    times = []
    for _ in range(int(cfg["evaluate"]["latency_runs"]) * 20):
        t0 = time.perf_counter()
        model.predict(row)
        model.project(row)
        times.append((time.perf_counter() - t0) * 1000)
    res["latency_ms_per_company_assign_median"] = round(statistics.median(times), 3)

    out = out_root / (f"dryrun-{corpus_dir.name}" if dry_run else version)
    out.mkdir(parents=True, exist_ok=True)
    (out / "metrics.json").write_text(json.dumps(res, indent=2, default=str) + "\n", encoding="utf-8", newline="\n")
    (out / "report.md").write_text(report_md(res), encoding="utf-8", newline="\n")
    cents.to_csv(out / "centroids.csv")
    _plot(fit, hold, out)
    res["out_dir"] = str(out)
    return res


def report_md(r: dict[str, Any]) -> str:
    def f(v: Any) -> str:
        return f"{v:.3f}" if isinstance(v, float) and v == v else str(v)

    lines = [f"# Segmentation evaluation: {r['model_version']}{' (DRY RUN)' if r['dry_run'] else ''}", "",
             f"SYNTHETIC data. {r['algo']} k={r['k']}; fit {r['n_fit']} / holdout {r['n_holdout']} companies.", "",
             "| metric | fit | holdout |", "|---|---|---|",
             f"| silhouette | {f(r['silhouette']['fit'])} | {f(r['silhouette']['holdout'])} |",
             f"| ARI vs hidden profile | {f(r['ari_vs_profile']['fit'])} | {f(r['ari_vs_profile']['holdout'])} |",
             f"| ARI vs bank (feature table) | {f(r['ari_vs_bank']['fit'])} | {f(r['ari_vs_bank']['holdout'])} |", ""]
    if "holdout_transfer_ari" in r:
        lines.append(f"Holdout transfer ARI (predict vs K-Means refit on holdout): {f(r['holdout_transfer_ari'])}")
    lines += ["", "| segment | name | bootstrap Jaccard | fit size | holdout size |", "|---|---|---|---|---|"]
    for c, n in r["names"].items():
        lines.append(f"| {c} | {n} | {f(r['stability_jaccard'][c])} | {r['sizes']['fit'].get(int(c), 0)} | "
                     f"{r['sizes']['holdout'].get(int(c), 0)} |")
    lines += ["", "K-Means ARI vs profile by k (fit; diagnostic, not used for selection): "
              + ", ".join(f"k={k}: {v:.3f}" for k, v in r["kmeans_ari_by_k_fit"].items()), "",
              "Segment x profile (all companies):", "", "```", json.dumps(r["crosstab_profile"], indent=1), "```",
              "Segment x bank:", "", "```", json.dumps(r["crosstab_bank"], indent=1), "```", "",
              "Centroids (fit set, raw units):", "", "```", json.dumps(r["centroids_raw_units_fit"], indent=1), "```", "",
              f"Latency: {r['latency_ms_per_company_assign_median']} ms per company assignment (median).", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--model-version")
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--write-thresholds", action="store_true")
    ap.add_argument("--results", type=Path, default=Path("results/segmentation"))
    ap.add_argument("--n-jobs", type=int, default=3)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    version = a.model_version or json.loads(
        (a.results / ("selection_ci.json" if a.dry_run else "selection.json")).read_text(encoding="utf-8"))["model_version"]
    res = evaluate(a.corpus, version=version, allow_unpinned=a.allow_unpinned, dry_run=a.dry_run,
                   n_jobs=a.n_jobs, out_root=a.results)
    print(report_md(res))
    print(f"results: {res['out_dir']}")
    if a.write_thresholds and not a.dry_run:
        from config.thresholds import update_section

        update_section(THRESHOLDS_PATH, "segmentation", {"model_version": f'"{version}"'})
        print("thresholds.yaml segmentation section updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
