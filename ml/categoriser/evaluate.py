"""Evaluate the hybrid categoriser on the three corpus splits.

    python -m ml.categoriser.evaluate data/corpus/v1 [--write-thresholds] [--allow-unpinned]

For each split, using the model ``train.py`` chose on validation:

1. **τ on VALIDATION.** Rows not caught by a high-precision rule are scored by
   the model. τ_accept is the lowest grid τ with ML-accepted precision
   ≥ target_accept_precision. τ_review is the lowest τ ≤ τ_accept whose band
   [τ_review, τ_accept) has precision ≥ target_band_precision.
2. **Test, once, with τ frozen:** rules-only (v2), ML-only and hybrid (no LLM)
   metrics, per-class P/R/F1, confusion matrix, precision–coverage curve,
   rows per routing source, expected LLM calls per 1,000 transactions, and
   slices (inline-template rows vs diversified, by bank).
3. **Latency** of the production model: Categoriser.categorise on 1,000-row
   documents, median of 5.

Outputs go to ``results/categoriser/<production model_version>/``. All numbers
are on SYNTHETIC data (generator v2.0.0, corpus v1).
"""
from __future__ import annotations

import argparse
import json
import math
import time
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.categoriser import _RULES, Categoriser
from config.thresholds import DEFAULT_PATH as THRESHOLDS_PATH
from config.thresholds import get_thresholds
from ml.categoriser.data import CorpusData, load_config, load_corpus
from ml.categoriser.infer import CategoriserModel, load_model
from ml.categoriser.metrics import (
    ALL_CLASSES,
    expected_calibration_error,
    per_class,
    summary,
)
from ml.categoriser.rule_audit import first_match_ids
from ml.features.categoriser import build_frame
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    ValidationStatus,
)

RESULTS = Path("results/categoriser")
SPLITS = ("company", "cross_bank", "unseen_vendor")


# ── τ selection ─────────────────────────────────────────────────────────────

def precision_coverage(p_max: np.ndarray, correct: np.ndarray, taus: np.ndarray) -> pd.DataFrame:
    rows = []
    n = len(p_max)
    for t in taus:
        m = p_max >= t
        k = int(m.sum())
        rows.append({"tau": round(float(t), 4), "coverage": k / n if n else 0.0,
                     "precision": float(correct[m].mean()) if k else float("nan"), "n": k})
    return pd.DataFrame(rows)


def choose_taus(p_max: np.ndarray, correct: np.ndarray, rcfg: dict[str, Any]) -> dict[str, Any]:
    g = rcfg["tau_grid"]
    grid = np.round(np.arange(g["start"], g["stop"] + 1e-9, g["step"]), 4)
    curve = precision_coverage(p_max, correct, grid)
    ok = curve[curve.precision >= rcfg["target_accept_precision"]]
    if len(ok):
        tau_accept = float(ok.tau.min())
        accept_note = "lowest grid τ meeting target"
    else:
        tau_accept = float(grid.max())
        accept_note = "target not reached on grid; using grid max"
    tau_review = tau_accept
    review_note = "no band met the target; τ_review = τ_accept"
    for t in np.round(np.arange(0.0, tau_accept + 1e-9, g["step"]), 4):
        band = (p_max >= t) & (p_max < tau_accept)
        if band.sum() == 0:
            continue
        if correct[band].mean() >= rcfg["target_band_precision"]:
            tau_review = float(t)
            review_note = "lowest τ with band precision ≥ target"
            break
    return {"tau_accept": tau_accept, "tau_review": tau_review,
            "accept_note": accept_note, "review_note": review_note, "curve": curve}


# ── routing simulation (deterministic, no LLM) ─────────────────────────────

def route(rule_hit: np.ndarray, p_max: np.ndarray, tau_accept: float, tau_review: float) -> np.ndarray:
    src = np.full(len(p_max), "review", dtype=object)
    src[p_max >= tau_review] = "ml_degraded"
    src[p_max >= tau_accept] = "ml"
    src[rule_hit] = "rule"
    return src


def llm_calls_per_1k(src: np.ndarray, company: np.ndarray, batch: int) -> dict[str, float]:
    """If an LLM were configured, every non-rule row with p < τ_accept goes to it,
    batched per statement (one company = one statement here)."""
    to_llm = np.isin(src, ["ml_degraded", "review"])
    n = len(src)
    per_co = pd.Series(to_llm).groupby(company).sum()
    calls = float(sum(math.ceil(v / batch) for v in per_co if v))
    return {"llm_rows_per_1k": 1000 * float(to_llm.sum()) / n if n else 0.0,
            "llm_api_calls_per_1k": 1000 * calls / n if n else 0.0,
            "batch_size": batch}


# ── plots ──────────────────────────────────────────────────────────────────

def save_confusion(y_true: np.ndarray, y_pred: np.ndarray, out: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sklearn.metrics import confusion_matrix

    cm = confusion_matrix(y_true, y_pred, labels=ALL_CLASSES)
    pd.DataFrame(cm, index=[f"true_{c}" for c in ALL_CLASSES],
                 columns=[f"pred_{c}" for c in ALL_CLASSES]).to_csv(out.with_suffix(".csv"),
                                                                    lineterminator="\n")
    row = cm.sum(axis=1, keepdims=True)
    norm = np.divide(cm, row, out=np.zeros_like(cm, dtype=float), where=row > 0)
    fig, ax = plt.subplots(figsize=(9, 8))
    ax.imshow(norm, cmap="Blues", vmin=0, vmax=1)
    ax.set_xticks(range(len(ALL_CLASSES)), ALL_CLASSES, rotation=45, ha="right", fontsize=8)
    ax.set_yticks(range(len(ALL_CLASSES)), ALL_CLASSES, fontsize=8)
    for i in range(len(ALL_CLASSES)):
        for j in range(len(ALL_CLASSES)):
            if cm[i, j]:
                ax.text(j, i, f"{cm[i, j]}", ha="center", va="center", fontsize=7,
                        color="white" if norm[i, j] > 0.5 else "black")
    ax.set_xlabel("predicted")
    ax.set_ylabel("true (row-normalised colour)")
    ax.set_title(title + "\nSYNTHETIC data (generator v2.0.0, corpus v1)", fontsize=9)
    fig.tight_layout()
    fig.savefig(out.with_suffix(".png"), dpi=120)
    plt.close(fig)


def save_curve(val_curve: pd.DataFrame, test_curve: pd.DataFrame, taus: dict[str, Any],
               out: Path, title: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    both = pd.concat([val_curve.assign(split="val"), test_curve.assign(split="test")])
    both.to_csv(out.with_suffix(".csv"), index=False, lineterminator="\n")
    fig, ax = plt.subplots(figsize=(7, 5))
    for name, c in (("val", val_curve), ("test", test_curve)):
        ax.plot(c.coverage, c.precision, marker=".", label=name)
    for k, ls in (("tau_accept", "--"), ("tau_review", ":")):
        row = val_curve.iloc[(val_curve.tau - taus[k]).abs().argmin()]
        ax.axvline(row.coverage, ls=ls, color="grey", label=f"{k}={taus[k]:.2f} (val)")
    ax.set_xlabel("coverage (share of model-routed rows auto-accepted)")
    ax.set_ylabel("precision of accepted rows")
    ax.set_title(title + "\nSYNTHETIC data (generator v2.0.0, corpus v1)", fontsize=9)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(out.with_suffix(".png"), dpi=120)
    plt.close(fig)


# ── latency ────────────────────────────────────────────────────────────────

def _to_txns(rows: pd.DataFrame) -> list[CanonicalTransaction]:
    out = []
    for i, r in enumerate(rows.itertuples(index=False)):
        d = r.date if isinstance(r.date, date) else pd.Timestamp(r.date).date()
        out.append(CanonicalTransaction(
            transaction_id=f"lat_{i}", date=d, description=r.description or "",
            debit=None if r.debit is None or pd.isna(r.debit) else Decimal(str(r.debit)),
            credit=None if r.credit is None or pd.isna(r.credit) else Decimal(str(r.credit)),
            balance=Decimal("0"),
            source_reference=SourceReference(file_path="corpus", page=0, row=i, raw_text=""),
        ))
    return out


def measure_latency(model: CategoriserModel, rows: pd.DataFrame, runs: int = 5) -> dict[str, Any]:
    txns = _to_txns(rows.head(1000))
    doc = StatementDocument(
        account_id="lat", statement_period_start=min(t.date for t in txns),
        statement_period_end=max(t.date for t in txns), source_format="corpus",
        validation_status=ValidationStatus.UNVALIDATED, transactions=txns,
    )
    cat = Categoriser(llm_enabled=False, model=model)
    v2 = Categoriser(llm_enabled=False, model=None)
    cat.categorise(doc)  # warm-up
    t_v3, t_v2, t_ml = [], [], []
    def timed(fn: Any) -> float:
        t = time.perf_counter()
        fn()
        return time.perf_counter() - t

    for _ in range(runs):
        t_v3.append(timed(lambda: cat.categorise(doc)))
        t_v2.append(timed(lambda: v2.categorise(doc)))
        t_ml.append(timed(lambda: model.predict_proba_transactions(txns)))
    return {"rows": len(txns), "runs": runs,
            "v3_categorise_ms_per_1k": 1000 * float(np.median(t_v3)),
            "v2_rules_only_ms_per_1k": 1000 * float(np.median(t_v2)),
            "model_predict_ms_per_1k": 1000 * float(np.median(t_ml)),
            "note": "single process, CPU, includes feature building; median of runs"}


# ── one split ──────────────────────────────────────────────────────────────

def evaluate_split(split: str, data: CorpusData, frame: pd.DataFrame, fm: np.ndarray,
                   corpus_dir: Path, cfg: dict[str, Any], results: Path, out: Path,
                   hp_rules: tuple[int, ...]) -> dict[str, Any]:
    sel = json.loads((results / f"selection_{split}.json").read_text(encoding="utf-8"))
    version = sel["versions"][sel["chosen_on_val"]]
    model = load_model(version)
    masks = data.split_masks(corpus_dir, cfg["splits"][split])
    rule_cat = np.array([c.value for _, _, c in _RULES], dtype=object)
    hp = np.isin(fm, list(hp_rules))

    def score(mask: np.ndarray) -> dict[str, Any]:
        idx = np.flatnonzero(mask)
        proba = model.predict_proba_frame(frame.iloc[idx])
        classes = np.array(model.classes, dtype=object)
        top3 = np.argsort(-proba, axis=1)[:, :3]
        return {"idx": idx, "proba": proba, "pred": classes[proba.argmax(1)],
                "p_max": proba.max(1), "top3": classes[top3], "y": data.y[idx],
                "hp": hp[idx], "rules_v2": rule_cat[fm[idx]], "classes": classes}

    va, te = score(masks["val"]), score(masks["test"])

    # τ on validation, over rows the high-precision rules don't catch.
    vm = ~va["hp"]
    taus = choose_taus(va["p_max"][vm], (va["pred"] == va["y"])[vm], cfg["routing"])
    tm = ~te["hp"]
    g = cfg["routing"]["tau_grid"]
    grid = np.round(np.arange(g["start"], g["stop"] + 1e-9, g["step"]), 4)
    test_curve = precision_coverage(te["p_max"][tm], (te["pred"] == te["y"])[tm], grid)

    # Production τ = max(validation τ, policy floor); see config.yaml routing.policy_floor.
    floor = cfg["routing"].get("policy_floor") or {"tau_accept": 0.0, "tau_review": 0.0}
    policy = {"tau_accept": max(taus["tau_accept"], floor["tau_accept"]),
              "tau_review": max(taus["tau_review"], floor["tau_review"])}
    policy["tau_review"] = min(policy["tau_review"], policy["tau_accept"])

    # Test, τ frozen.
    src = route(te["hp"], te["p_max"], taus["tau_accept"], taus["tau_review"])
    hybrid = np.where(te["hp"], te["rules_v2"], te["pred"])
    y = te["y"]
    in_top3 = np.array([y[i] in te["top3"][i] for i in range(len(y))])
    company = data.groups["company_id"].to_numpy()[te["idx"]]
    tmpl = data.groups["template_id"].to_numpy()[te["idx"]]
    inline = pd.Series(tmpl).str.contains(":inline:").to_numpy()
    bank = data.groups["bank"].to_numpy()[te["idx"]]
    def routing_stats(src: np.ndarray) -> dict[str, Any]:
        acc = np.isin(src, ["rule", "ml"])
        pend = np.isin(src, ["ml_degraded", "review"])

        def prec(m: np.ndarray) -> float | None:
            return float((hybrid[m] == y[m]).mean()) if m.any() else None
        return {
            **{f"pct_{k}": 100 * float((src == k).mean()) for k in ("rule", "ml", "ml_degraded", "review")},
            "precision_rule": prec(src == "rule"),
            "precision_ml_accepted": prec(src == "ml"),
            "precision_ml_degraded": prec(src == "ml_degraded"),
            "precision_review_best_guess": prec(src == "review"),
            "precision_auto_accepted": prec(acc),
            "coverage_auto_accepted": float(acc.mean()),
            "pending_true_in_top3": float(in_top3[pend].mean()) if pend.any() else None,
            **llm_calls_per_1k(src, company, cfg["routing"]["llm_batch_size"]),
        }

    y_idx_val = np.array([list(va["classes"]).index(c) if c in va["classes"] else -1 for c in va["y"]])

    res: dict[str, Any] = {
        "split": split, "model_version": version, "algo": sel["chosen_on_val"],
        "n_val": int(len(va["y"])), "n_test": int(len(y)),
        "tau": {k: v for k, v in taus.items() if k != "curve"},
        "val_ece_top_label": expected_calibration_error(va["proba"], y_idx_val),
        "test": {
            "rules_v2": summary(y, te["rules_v2"]),
            "ml_only": summary(y, te["pred"]),
            "hybrid_no_llm": summary(y, hybrid),
        },
        "routing_test": routing_stats(src),
        "tau_policy": policy,
        "routing_test_policy": routing_stats(
            route(te["hp"], te["p_max"], policy["tau_accept"], policy["tau_review"])),
        "slices_test": {
            "inline_templates": {"share_of_rows": float(inline.mean()),
                                 "hybrid": summary(y[inline], hybrid[inline]) if inline.any() else None,
                                 "rules_v2": summary(y[inline], te["rules_v2"][inline]) if inline.any() else None},
            "diversified_templates": {"hybrid": summary(y[~inline], hybrid[~inline]),
                                      "rules_v2": summary(y[~inline], te["rules_v2"][~inline])},
            **{f"bank_{b}": {"hybrid": summary(y[bank == b], hybrid[bank == b]),
                             "rules_v2": summary(y[bank == b], te["rules_v2"][bank == b])}
               for b in sorted(set(bank))},
        },
        "per_class_test": {"rules_v2": per_class(y, te["rules_v2"]),
                           "ml_only": per_class(y, te["pred"]),
                           "hybrid_no_llm": per_class(y, hybrid)},
        "candidates": {k: {"val_macro_f1": v["val"]["macro_f1"], "test_macro_f1": v["test"]["macro_f1"],
                           "val_accuracy": v["val"]["accuracy"], "test_accuracy": v["test"]["accuracy"]}
                       for k, v in sel["candidates"].items()},
        "chosen_on_val": sel["chosen_on_val"],
    }

    out.mkdir(parents=True, exist_ok=True)
    (out / f"metrics_{split}.json").write_text(json.dumps(res, indent=2, default=str) + "\n",
                                               encoding="utf-8", newline="\n")
    rows = []
    for variant, pcs in res["per_class_test"].items():
        rows += [{"variant": variant, **r} for r in pcs]
    pd.DataFrame(rows).to_csv(out / f"per_class_{split}.csv", index=False, lineterminator="\n")
    save_confusion(y, hybrid, out / f"confusion_{split}", f"Hybrid (no LLM), {split} test")
    save_confusion(y, te["rules_v2"], out / f"confusion_rules_v2_{split}", f"Rules v2, {split} test")
    save_curve(taus["curve"], test_curve, taus, out / f"precision_coverage_{split}",
               f"Precision–coverage, model-routed rows, {split}")
    (out / f"routing_{split}.json").write_text(json.dumps(
        {"validation_tau": {**res["tau"], "routing": res["routing_test"]},
         "policy_tau": {**policy, "routing": res["routing_test_policy"]}}, indent=2) + "\n",
                                               encoding="utf-8", newline="\n")
    return res


def write_thresholds(version: str, tau_accept: float, tau_review: float, path: Path) -> None:
    from config.thresholds import update_section

    update_section(path, "categoriser", {"model_version": f'"{version}"',
                                          "tau_accept": f"{tau_accept:.2f}",
                                          "tau_review": f"{tau_review:.2f}"})


def _f(v: Any, nd: int = 4) -> str:
    return "n/a" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))


def report_md(all_res: dict[str, dict[str, Any]], latency: dict[str, Any]) -> str:
    L = ["## Test metrics (τ frozen from validation). SYNTHETIC data, generator v2.0.0, corpus v1", "",
         "| split | variant | n | accuracy | macro-P | macro-R | macro-F1 |", "|---|---|---|---|---|---|---|"]
    for s, r in all_res.items():
        for v in ("rules_v2", "ml_only", "hybrid_no_llm"):
            m = r["test"][v]
            L.append(f"| {s} | {v} | {m['n']} | {_f(m['accuracy'])} | {_f(m['macro_precision'])} | "
                     f"{_f(m['macro_recall'])} | {_f(m['macro_f1'])} |")
    L += ["", "Macro averages are over classes with test support; LOAN_OUT has 0 rows and can't be evaluated.", "",
          "## Candidates (chosen on validation macro-F1; test shown for reference only)", "",
          "| split | candidate | val macro-F1 | test macro-F1 | val acc | test acc | chosen |", "|---|---|---|---|---|---|---|"]
    for s, r in all_res.items():
        for k, v in r["candidates"].items():
            L.append(f"| {s} | {k} | {_f(v['val_macro_f1'])} | {_f(v['test_macro_f1'])} | "
                     f"{_f(v['val_accuracy'])} | {_f(v['test_accuracy'])} | {'yes' if k == r['chosen_on_val'] else ''} |")
    L += ["", "## Routing on test (no LLM configured)", "",
          "`val` = τ chosen on validation; `policy` = max(val τ, policy floor), written to thresholds.yaml.", "",
          "| split | τ set | τ_accept | τ_review | % rule | % ml | % ml_degraded | % review | precision auto-accepted | coverage auto-accepted | pending true∈top-3 | LLM rows /1k | LLM calls /1k | val ECE |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s, r in all_res.items():
        for label, tau, q in (("val", r["tau"], r["routing_test"]),
                              ("policy", r["tau_policy"], r["routing_test_policy"])):
            L.append(f"| {s} | {label} | {tau['tau_accept']:.2f} | {tau['tau_review']:.2f} | {q['pct_rule']:.2f} | "
                     f"{q['pct_ml']:.2f} | {q['pct_ml_degraded']:.3f} | {q['pct_review']:.3f} | "
                     f"{_f(q['precision_auto_accepted'])} | {_f(q['coverage_auto_accepted'])} | "
                     f"{_f(q['pending_true_in_top3'])} | {q['llm_rows_per_1k']:.2f} | {q['llm_api_calls_per_1k']:.2f} | "
                     f"{_f(r['val_ece_top_label'])} |")
    L += ["", "## Per-class test F1 (rules_v2 → hybrid_no_llm)", "",
          "| class | " + " | ".join(f"{s} support | {s} rules_v2 | {s} hybrid" for s in all_res) + " |",
          "|---|" + "---|---|---|" * len(all_res)]
    for i, c in enumerate(ALL_CLASSES):
        cells = []
        for r in all_res.values():
            a, b = r["per_class_test"]["rules_v2"][i], r["per_class_test"]["hybrid_no_llm"][i]
            cells += [str(a["support"]), _f(a["f1"]), _f(b["f1"])]
        L.append(f"| {c} | " + " | ".join(cells) + " |")
    L += ["", "## Slices (test macro-F1 / accuracy, hybrid vs rules_v2)", "",
          "| split | slice | hybrid acc | hybrid macro-F1 | rules_v2 acc | rules_v2 macro-F1 |", "|---|---|---|---|---|---|"]
    for s, r in all_res.items():
        for k, v in r["slices_test"].items():
            if not isinstance(v, dict) or v.get("hybrid") is None:
                continue
            h, b = v["hybrid"], v["rules_v2"]
            L.append(f"| {s} | {k} (n={h['n']}) | {_f(h['accuracy'])} | {_f(h['macro_f1'])} | "
                     f"{_f(b['accuracy'])} | {_f(b['macro_f1'])} |")
    L += ["", "## Latency (production model, 1,000-row document, CPU, median of 5)", "",
          f"- v3 Categoriser.categorise: {latency['v3_categorise_ms_per_1k']:.1f} ms per 1k rows",
          f"- model predict only (incl. feature building): {latency['model_predict_ms_per_1k']:.1f} ms per 1k rows",
          f"- v2 rules only: {latency['v2_rules_only_ms_per_1k']:.1f} ms per 1k rows", ""]
    return "\n".join(L)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Evaluate the hybrid categoriser (SYNTHETIC corpus).")
    ap.add_argument("corpus_dir", type=Path)
    ap.add_argument("--results", type=Path, default=RESULTS)
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--write-thresholds", action="store_true",
                    help="write the production model_version and company-val τ to config/thresholds.yaml")
    a = ap.parse_args(argv)

    cfg = load_config()
    data = load_corpus(a.corpus_dir, cfg, a.allow_unpinned)
    hp_rules = get_thresholds().categoriser.high_precision_rules
    frame = build_frame(data.rows)
    fm = first_match_ids(data.rows["description"], data.rows["credit"].notna().to_numpy())

    prod_split = cfg["production_split"]
    prod_sel = json.loads((a.results / f"selection_{prod_split}.json").read_text(encoding="utf-8"))
    prod_version = prod_sel["versions"][prod_sel["chosen_on_val"]]
    out = a.results / prod_version

    all_res = {}
    for split in SPLITS:
        print(f"[{split}] evaluating", flush=True)
        all_res[split] = evaluate_split(split, data, frame, fm, a.corpus_dir, cfg, a.results, out, hp_rules)

    test_rows = data.rows.iloc[np.flatnonzero(data.split_masks(a.corpus_dir, cfg["splits"][prod_split])["test"])]
    latency = measure_latency(load_model(prod_version), test_rows)
    (out / "latency.json").write_text(json.dumps(latency, indent=2) + "\n", encoding="utf-8", newline="\n")
    summary_doc = {"corpus": data.identity, "production_model_version": prod_version,
                   "high_precision_rules": list(hp_rules),
                   "tau_validation": all_res[prod_split]["tau"],
                   "tau_production": all_res[prod_split]["tau_policy"], "latency": latency}
    (out / "summary.json").write_text(json.dumps(summary_doc, indent=2, default=str) + "\n",
                                      encoding="utf-8", newline="\n")
    md = report_md(all_res, latency)
    (out / "report.md").write_text(md, encoding="utf-8", newline="\n")
    print(md)
    if a.write_thresholds:
        t = all_res[prod_split]["tau_policy"]
        write_thresholds(prod_version, t["tau_accept"], t["tau_review"], THRESHOLDS_PATH)
        print(f"wrote config/thresholds.yaml: model_version={prod_version} "
              f"tau_accept={t['tau_accept']:.2f} tau_review={t['tau_review']:.2f}")


if __name__ == "__main__":
    main()
