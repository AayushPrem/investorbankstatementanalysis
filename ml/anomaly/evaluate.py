"""Evaluate the anomaly model once on held-out TEST companies.

Usage:
    python -m ml.anomaly.evaluate data/corpus/v1 [--allow-unpinned] [--results results/anomaly]
                                                 [--write-thresholds] [--n-jobs 3]

Reads ``results/anomaly/selection.json`` (written by ``train.py``). Thresholds
are frozen from validation. Methods compared on the same test rows:

* ``iforest``: per-account / pooled hybrid (the production scorer)
* ``iforest_all_pooled`` and ``iforest_all_per_account``: ablations
* ``zscore_baseline``: |robust z| of log amount against the account's prior
  same-direction rows (causal)
* ``rules``: the existing rule detectors plus compliance rules, run with
  predicted categories and the pipeline's own enrichment (LLM off). They form
  a set, so they get set precision/recall rather than @k.

Outputs go to ``results/anomaly/<model_version>/``: ``metrics.json, at_k.csv,
by_type.csv, per_company.csv, overlap.csv, severity.csv, latency.json, report.md``.
SYNTHETIC data only.
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

import pandas as pd

from ml import registry
from ml.anomaly import MODEL_NAME
from ml.anomaly.data import (
    KINDS,
    check_gate,
    company_document,
    company_splits,
    load_config,
    load_eval_labels,
    load_rows,
    rule_flags,
)
from ml.anomaly.infer import AnomalyDetector
from ml.anomaly.metrics import at_k, average_precision, set_metrics, top_k_mask
from ml.anomaly.model import SCORER_PER_ACCOUNT, SCORER_POOLED, AnomalyModel, flag_mask, severity_of
from ml.anomaly.train import corpus_features, score_parallel

log = logging.getLogger(__name__)


def _labelled(scored: pd.DataFrame, labels: pd.DataFrame) -> pd.DataFrame:
    return scored.merge(labels.drop(columns=["company_id"]), on="txn_id", how="left", validate="one_to_one")


def latency(model: AnomalyModel, version: str, sel: dict[str, Any], rows: pd.DataFrame,
            companies: list[str], runs: int) -> dict[str, Any]:
    """End-to-end ms per 1k rows: features + per-account fit/score (or pooled) + flags."""
    from analysis.categoriser import Categoriser

    det = AnomalyDetector(model, version, contamination=sel["contamination"],
                          severity_high_pct=sel["severity_high_pct"],
                          severity_medium_pct=sel["severity_medium_pct"],
                          max_flags_per_statement=sel["max_flags_per_statement"])
    docs = []
    for cid in companies:
        doc = company_document(rows[rows.company_id == cid])
        doc, _ = Categoriser(llm_enabled=False).categorise(doc)   # not timed
        docs.append(doc)
    n = sum(len(d.transactions) for d in docs)
    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        for d in docs:
            det.detect(d)
        times.append(time.perf_counter() - t0)
    med = statistics.median(times)
    return {"companies": len(docs), "rows": n, "runs": runs,
            "ms_per_1k_rows": round(med / n * 1000 * 1000, 1),
            "note": "single process, CPU; features + IsolationForest fit/score + flag building; "
                    "categorisation not included; median of runs"}


def counterparty_diagnostic(feats: pd.DataFrame, corpus_dir: Path) -> dict[str, Any]:
    """How the narration-derived key compares with the label key (evaluation only)."""
    lab = pd.read_parquet(corpus_dir / "labels" / "transactions.parquet", columns=["txn_id", "counterparty_key"])
    d = feats[["txn_id", "company_id", "counterparty"]].merge(lab, on="txn_id")
    d["counterparty_key"] = d.counterparty_key.fillna("<na>")
    g = d.groupby(["company_id", "counterparty"]).counterparty_key.agg(lambda x: x.value_counts().iloc[0])
    w = d.groupby(["company_id", "counterparty"]).size()
    h = d.groupby(["company_id", "counterparty_key"]).counterparty.agg(lambda x: x.value_counts().iloc[0])
    w2 = d.groupby(["company_id", "counterparty_key"]).size()
    return {"purity": round(float(g.sum() / w.sum()), 4),
            "completeness": round(float(h.sum() / w2.sum()), 4),
            "derived_keys_per_company": round(float(w.groupby(level=0).size().mean()), 1),
            "label_keys_per_company": round(float(w2.groupby(level=0).size().mean()), 1)}


def evaluate(corpus_dir: Path, *, allow_unpinned: bool, results: Path, n_jobs: int,
             selection_file: str = "selection.json") -> dict[str, Any]:
    cfg = load_config()
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    selection = json.loads((results / selection_file).read_text(encoding="utf-8"))
    version = selection["model_version"]
    loaded = registry.load(MODEL_NAME, version)
    model: AnomalyModel = loaded.model
    sel = loaded.metadata["selected_on_val"]
    if loaded.metadata["corpus"]["corpus_hash"] != ident["corpus_hash"]:
        raise RuntimeError("model was trained on a different corpus")
    ks = list(cfg["evaluate"]["ks"])

    feats, cat_ver = corpus_features(corpus_dir, ident, n_jobs)
    split = feats.company_id.map(company_splits(corpus_dir))
    te = feats[split == "test"].reset_index(drop=True)
    test_ids = sorted(te.company_id.unique())
    labels = load_eval_labels(corpus_dir)
    lab = labels.frame
    ml_types = list(labels.ml_types)
    label_cols = ["any", *KINDS, *ml_types]

    # ── scores ─────────────────────────────────────────────────────────────
    t0 = time.perf_counter()
    hybrid = _labelled(score_parallel(model, te, None, n_jobs), lab)
    pooled = _labelled(score_parallel(model, te, SCORER_POOLED, n_jobs), lab)
    per_acct = _labelled(score_parallel(model, te, SCORER_PER_ACCOUNT, n_jobs), lab)
    base = te[["txn_id", "account_id", "company_id"]].assign(score=te.acct_amount_z.abs().to_numpy())
    base = _labelled(base, lab)
    t_score = time.perf_counter() - t0
    methods = {"iforest": ("percentile", hybrid), "iforest_all_pooled": ("percentile", pooled),
               "iforest_all_per_account": ("percentile", per_acct), "zscore_baseline": ("score", base)}

    # ── @k ─────────────────────────────────────────────────────────────────
    at_rows = []
    for mname, (col, df) in methods.items():
        for lc in label_cols:
            m = at_k(df, col, lc, ks)
            m["average_precision"] = average_precision(df[col].to_numpy(), df[lc].to_numpy())
            at_rows.append({"method": mname, "label": lc, **m})
    prevalence = {lc: float(hybrid[lc].mean()) for lc in label_cols}
    for lc in label_cols:     # random ranking: expected P@k = prevalence
        r = {"method": "random_expected", "label": lc, "n_positive": int(hybrid[lc].sum())}
        for k in ks:
            r[f"p@{k}_macro"] = r[f"p@{k}_micro"] = prevalence[lc]
        at_rows.append(r)
    at_df = pd.DataFrame(at_rows)

    # per company (hybrid, any label)
    pc = []
    for cid, g in hybrid.groupby("company_id"):
        row: dict[str, Any] = {"company_id": cid, "rows": len(g), "positives": int(g["any"].sum()),
                               "scorer": g.scorer.iloc[0]}
        for k in ks:
            top = g.sort_values(["percentile", "txn_id"], ascending=[False, True]).head(k)
            row[f"hits@{k}"] = int(top["any"].sum())
        pc.append(row)
    per_company = pd.DataFrame(pc)

    # ── operating point, rules, overlap ────────────────────────────────────
    hybrid["ml_flagged"] = flag_mask(hybrid, sel["contamination"], sel["max_flags_per_statement"])
    rules = rule_flags(load_rows(corpus_dir), test_ids, n_jobs=n_jobs)
    hybrid = hybrid.merge(rules[["txn_id", "risk_flagged", "compliance_flagged"]], on="txn_id", how="left")
    hybrid[["risk_flagged", "compliance_flagged"]] = hybrid[["risk_flagged", "compliance_flagged"]].fillna(False)
    hybrid["rules_flagged"] = hybrid.risk_flagged | hybrid.compliance_flagged
    hybrid["ml_top20"] = top_k_mask(hybrid, "percentile", 20).to_numpy()
    base["base_top20"] = top_k_mask(base, "score", 20).to_numpy()
    hybrid = hybrid.merge(base[["txn_id", "base_top20"]], on="txn_id", how="left")

    set_rows, overlap_rows = [], []
    for lc in label_cols:
        y = hybrid[lc].to_numpy(dtype=bool)
        for name in ("ml_flagged", "ml_top20", "base_top20", "risk_flagged", "compliance_flagged",
                     "rules_flagged"):
            set_rows.append({"label": lc, "set": name, **set_metrics(hybrid[name].to_numpy(dtype=bool), y)})
        pos = hybrid[y]
        for ml_set in ("ml_flagged", "ml_top20"):
            overlap_rows.append({
                "label": lc, "ml_set": ml_set, "positives": int(len(pos)),
                "caught_by_rules": int(pos.rules_flagged.sum()),
                "caught_by_ml": int(pos[ml_set].sum()),
                "ml_and_rules": int((pos[ml_set] & pos.rules_flagged).sum()),
                "ml_not_rules": int((pos[ml_set] & ~pos.rules_flagged).sum()),
                "rules_not_ml": int((~pos[ml_set] & pos.rules_flagged).sum()),
                "neither": int((~pos[ml_set] & ~pos.rules_flagged).sum()),
            })
    set_df, overlap_df = pd.DataFrame(set_rows), pd.DataFrame(overlap_rows)

    # severity on test
    fl = hybrid[hybrid.ml_flagged].copy()
    fl["severity"] = [severity_of(p, sel["severity_high_pct"], sel["severity_medium_pct"])
                      for p in fl.percentile]
    sev_df = fl.groupby("severity").agg(n=("txn_id", "size"), precision_any=("any", "mean"),
                                        precision_ml_only=("ml_only", "mean")).reset_index()

    scorer_counts = {
        "rows": hybrid.scorer.value_counts().to_dict(),
        "companies": hybrid.groupby("company_id").scorer.first().value_counts().to_dict(),
    }
    lat = latency(model, version, sel, load_rows(corpus_dir), test_ids[:10],
                  int(cfg["evaluate"]["latency_runs"]))
    diag = counterparty_diagnostic(te, corpus_dir)

    out_dir = results / version
    out_dir.mkdir(parents=True, exist_ok=True)
    w = {"index": False, "lineterminator": "\n"}
    at_df.to_csv(out_dir / "at_k.csv", **w)
    at_df[at_df.label.isin(ml_types + list(KINDS))].to_csv(out_dir / "by_type.csv", **w)
    per_company.to_csv(out_dir / "per_company.csv", **w)
    set_df.to_csv(out_dir / "sets.csv", **w)
    overlap_df.to_csv(out_dir / "overlap.csv", **w)
    sev_df.to_csv(out_dir / "severity.csv", **w)
    (out_dir / "latency.json").write_text(json.dumps(lat, indent=2) + "\n", encoding="utf-8", newline="\n")
    summary = {
        "model_version": version, "corpus": ident, "category_source": cat_ver, "synthetic": True,
        "selected_on_val": sel, "test_companies": len(test_ids), "test_rows": int(len(hybrid)),
        "prevalence_test": prevalence, "scorer_counts": scorer_counts, "latency": lat,
        "counterparty_key_diagnostic": diag, "scoring_seconds": round(t_score, 1),
    }
    (out_dir / "metrics.json").write_text(json.dumps(summary, indent=2, default=float) + "\n",
                                          encoding="utf-8", newline="\n")
    md = report_md(summary, at_df, set_df, overlap_df, sev_df, ks, ml_types)
    (out_dir / "report.md").write_text(md, encoding="utf-8", newline="\n")
    return {"summary": summary, "report": md, "out_dir": str(out_dir), "selected": sel}


def _f(v: Any, nd: int = 3) -> str:
    if v is None or (isinstance(v, float) and v != v):
        return "n/a"
    return f"{v:.{nd}f}" if isinstance(v, float) else str(v)


def report_md(s: dict[str, Any], at_df: pd.DataFrame, set_df: pd.DataFrame, ov: pd.DataFrame,
              sev: pd.DataFrame, ks: list[int], ml_types: list[str]) -> str:
    sel = s["selected_on_val"]
    L = [f"# Anomaly model `{s['model_version']}`: test report",
         "", "SYNTHETIC data (corpus v1). Detects *synthetic injections*; not evidence of real-world performance.",
         f"Test: {s['test_companies']} companies, {s['test_rows']} rows. Category input: `{s['category_source']}` "
         "(predicted, never labels). Counterparty: narration-derived.",
         f"Frozen from validation: min_history={sel['min_history']}, contamination={sel['contamination']}, "
         f"max_flags={sel['max_flags_per_statement']}, severity HIGH≥{sel['severity_high_pct']} "
         f"MEDIUM≥{sel['severity_medium_pct']} (fallback: {sel['severity_fallback']}).", ""]
    L += ["## precision@k / recall@k (per-company macro · pooled micro)", ""]
    hdr = "| method | label | positives | " + " | ".join(
        f"P@{k} mac | P@{k} mic | R@{k} mac | R@{k} mic" for k in ks) + " | AP |"
    L += [hdr, "|" + "---|" * (3 + 4 * len(ks) + 1)]
    order = ["iforest", "zscore_baseline", "iforest_all_pooled", "iforest_all_per_account", "random_expected"]
    for lbl in ["any", *KINDS, *ml_types]:
        for mname in order:
            r = at_df[(at_df.method == mname) & (at_df.label == lbl)]
            if r.empty:
                continue
            r = r.iloc[0]
            cells = []
            for k in ks:
                cells += [_f(r.get(f"p@{k}_macro")), _f(r.get(f"p@{k}_micro")),
                          _f(r.get(f"r@{k}_macro")), _f(r.get(f"r@{k}_micro"))]
            L.append(f"| {mname} | {lbl} | {r.n_positive} | " + " | ".join(cells)
                     + f" | {_f(r.get('average_precision'))} |")
    L += ["", "## Operating point vs rule detectors (sets)", "",
          "| label | set | flagged | TP | precision | recall |", "|---|---|---|---|---|---|"]
    for r in set_df.itertuples(index=False):
        L.append(f"| {r.label} | {r.set} | {r.flagged} | {r.tp} | {_f(r.precision)} | {_f(r.recall)} |")
    L += ["", "## Overlap: what ML finds that the rules miss", "",
          "| label | ML set | positives | rules | ML | ML∧rules | ML¬rules | rules¬ML | neither |",
          "|---|---|---|---|---|---|---|---|---|"]
    for r in ov.itertuples(index=False):
        L.append(f"| {r.label} | {r.ml_set} | {r.positives} | {r.caught_by_rules} | {r.caught_by_ml} | "
                 f"{r.ml_and_rules} | {r.ml_not_rules} | {r.rules_not_ml} | {r.neither} |")
    L += ["", "## Severity bands on test (flagged rows)", "", "| severity | n | precision (any) | precision (ml_only) |",
          "|---|---|---|---|"]
    for r in sev.itertuples(index=False):
        L.append(f"| {r.severity} | {r.n} | {_f(r.precision_any)} | {_f(r.precision_ml_only)} |")
    lat = s["latency"]
    L += ["", f"Scorer use on test: {s['scorer_counts']}.",
          f"Inference: {lat['ms_per_1k_rows']} ms per 1k rows ({lat['rows']} rows, {lat['companies']} companies, "
          f"median of {lat['runs']}; {lat['note']}).",
          f"Counterparty key vs label key (diagnostic): {s['counterparty_key_diagnostic']}.",
          f"Test prevalence: any={_f(s['prevalence_test']['any'], 5)}, ml_only={_f(s['prevalence_test']['ml_only'], 5)}.",
          "", "Caveats: ml_off_hours_burst is defined by txn_time, which is not a feature (PDF input has no time); "
          "compliance_tds_gap removes rows and labels none; ml_only injectors are built to evade the rule detectors.", ""]
    return "\n".join(L)


def write_thresholds(version: str, sel: dict[str, Any], path: Path) -> None:
    from config.thresholds import update_section

    update_section(path, "anomaly", {
        "model_version": f'"{version}"',
        "contamination": f"{sel['contamination']}",
        "min_history": f"{int(sel['min_history'])}",
        "severity_high_pct": f"{sel['severity_high_pct']:.4f}",
        "severity_medium_pct": f"{sel['severity_medium_pct']:.4f}",
        "max_flags_per_statement": f"{int(sel['max_flags_per_statement'])}",
    })


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--results", type=Path, default=Path("results/anomaly"))
    ap.add_argument("--write-thresholds", action="store_true")
    ap.add_argument("--n-jobs", type=int, default=3)
    ap.add_argument("--selection", default="selection.json", help="selection_ci.json for --profile ci models")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    out = evaluate(a.corpus, allow_unpinned=a.allow_unpinned, results=a.results, n_jobs=a.n_jobs,
                   selection_file=a.selection)
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:      # Windows consoles default to cp1252
        reconfigure(encoding="utf-8")
    print(out["report"])
    print(f"wrote {out['out_dir']}")
    if a.write_thresholds:
        write_thresholds(out["summary"]["model_version"], out["selected"],
                         Path(__file__).resolve().parents[2] / "config" / "thresholds.yaml")
        print("updated config/thresholds.yaml anomaly section")
    return 0


if __name__ == "__main__":
    sys.exit(main())
