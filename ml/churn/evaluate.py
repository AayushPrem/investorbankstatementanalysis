"""Evaluate a registered churn model ONCE on the test split (or dry-run elsewhere).

Usage:
    python -m ml.churn.evaluate data/corpus/v1 [--model-version V] [--write-thresholds]
    python -m ml.churn.evaluate data/corpus/v1-small --allow-unpinned --dry-run

Writes ``results/churn/<model_version>/{metrics.json, report.md, calibration.png,
pr_curve.png}`` (dry runs: ``results/churn/dryrun-<corpus>/``).

Labels are read here for EVALUATION only: ``labels/companies.parquet:profile``
(slicing) and ``labels/transactions.parquet:true_customer_id`` (identity audit,
oracle-identity row, label agreement). The model, its thresholds and its
calibration were fixed by ``ml.churn.train`` before this runs.

Rule comparison uses the real ``CustomerAnalyticsAnalyst`` on each statement
truncated at the cutoff, with ``customer_id`` set to the unit key (evaluation
harness only), so rule and model see the same units and the same rows.
"""
from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd

from ml import registry
from ml.churn import MODEL_NAME
from ml.churn.data import check_gate, corpus_payments, load_config, load_units
from ml.churn.identity_audit import CHURN_PROFILES, audit, eval_frame
from ml.churn.metrics import (
    bootstrap_ci,
    brier,
    calibration_table,
    pr_auc,
    precision_at_top,
    prf,
    roc_auc,
)
from ml.churn.model import ChurnModel
from ml.features.churn_features import company_units, is_active

log = logging.getLogger(__name__)
THRESHOLDS_PATH = Path("config/thresholds.yaml")


# ── rule churn on truncated statements (the existing analyst, unchanged) ────

def _rule_company(args: tuple[str, pd.DataFrame, list[date]]) -> list[tuple[str, date, str, int, int]]:
    from analysis.customer_analytics import CustomerAnalyticsAnalyst
    from schema.canonical import (
        CanonicalTransaction,
        SourceReference,
        StatementDocument,
        TransactionCategory,
    )

    cid, pay, cutoffs = args
    out = []
    for c in cutoffs:
        hist = pay[pay["date"] <= c]
        if hist.empty:
            continue
        txns = [CanonicalTransaction(
            transaction_id=f"{cid}-{i}", date=r.date, description="payment",
            credit=Decimal(str(round(r.amount, 2))), balance=Decimal("0"),
            category=TransactionCategory.REVENUE, customer_id=r.key,
            source_reference=SourceReference(file_path="eval", page=0, row=i, raw_text=""))
            for i, r in enumerate(hist.itertuples(index=False))]
        doc = StatementDocument(account_id=cid, statement_period_start=txns[0].date,
                                statement_period_end=c, source_format="eval", transactions=txns)
        rep = CustomerAnalyticsAnalyst().analyse(doc)
        churned = {e.customer_id for e in rep.churn_events}
        alerted = {a.customer_id for a in rep.payment_regularity_alerts}
        for k in sorted(set(hist["key"])):
            out.append((cid, c, k, int(k in churned), int(k in alerted)))
    return out


def rule_predictions(payments: pd.DataFrame, units: pd.DataFrame, n_jobs: int) -> pd.DataFrame:
    by_co = {c: g for c, g in payments.groupby("company_id", sort=True)}
    jobs = [(c, by_co[c], sorted(set(g["cutoff_date"])))
            for c, g in units.groupby("company_id", sort=True) if c in by_co]
    if n_jobs > 1 and len(jobs) > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            parts = list(ex.map(_rule_company, jobs, chunksize=2))
    else:
        parts = [_rule_company(j) for j in jobs]
    return pd.DataFrame([r for p in parts for r in p],
                        columns=["company_id", "cutoff_date", "key", "rule_churn", "rule_regularity"])


# ── metric blocks ──────────────────────────────────────────────────────────

def _ap(col: str) -> Callable[[pd.DataFrame], float]:
    return lambda f: pr_auc(f["y"].to_numpy(), f[col].to_numpy())


def _f1(col: str) -> Callable[[pd.DataFrame], float]:
    return lambda f: prf(f["y"].to_numpy(), f[col].to_numpy())["f1"]


def metric_block(f: pd.DataFrame, thr: float, top: float, n_boot: int, seed: int) -> dict[str, Any]:
    y = f["y"].to_numpy()
    p = f["p"].to_numpy()
    pred = (p >= thr).astype(int)
    out: dict[str, Any] = {
        "n_units": int(len(f)), "n_companies": int(f["company_id"].nunique()),
        "pos_rate": float(y.mean()) if len(y) else float("nan"),
        "model": {
            "pr_auc": bootstrap_ci(f, _ap("p"), n_boot, seed),
            "roc_auc": roc_auc(y, p),
            "at_threshold": {**prf(y, pred), "threshold": thr,
                             "f1_ci": bootstrap_ci(f.assign(pred=pred), _f1("pred"), n_boot, seed)},
            **precision_at_top(f, "p", top),
            "brier_calibrated": brier(y, p), "brier_raw": brier(y, f["raw"].to_numpy()),
        },
        "baseline_days_since_last": {
            "pr_auc": bootstrap_ci(f, _ap("days_since_last"), n_boot, seed),
            "roc_auc": roc_auc(y, f["days_since_last"].to_numpy()),
            **precision_at_top(f, "days_since_last", top),
        },
        "rule_churn_events": {**prf(y, f["rule_churn"].to_numpy()),
                              "f1_ci": bootstrap_ci(f, _f1("rule_churn"), n_boot, seed)},
        "rule_regularity_alerts": prf(y, f["rule_regularity"].to_numpy()),
        "rule_silence_fallback": prf(y, f["rule_silence"].to_numpy()),
    }
    return out


def _plots(f: pd.DataFrame, out_dir: Path, bins: int) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from sklearn.metrics import precision_recall_curve

    a = f[is_active(f)]
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], ls="--", c="grey", lw=1)
    for col, lab in (("raw", "raw (class-weighted)"), ("p", "calibrated")):
        t = calibration_table(a["y"].to_numpy(), a[col].to_numpy(), bins)
        ax.plot([r["mean_pred"] for r in t], [r["frac_pos"] for r in t], marker="o", label=lab)
    ax.set(xlabel="predicted probability", ylabel="observed churn rate",
           title="Churn calibration, test active units (synthetic)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "calibration.png", dpi=120)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(5, 5))
    for col, lab in (("p", "model"), ("days_since_last", "days since last payment")):
        pr, rc, _ = precision_recall_curve(a["y"], a[col])
        ax.plot(rc, pr, label=lab)
    ax.axhline(a["y"].mean(), ls="--", c="grey", lw=1, label="base rate")
    ax.set(xlabel="recall", ylabel="precision", title="Churn PR curve, test active units (synthetic)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out_dir / "pr_curve.png", dpi=120)
    plt.close(fig)


# ── identity: oracle row and label agreement (evaluation only) ─────────────

def oracle_and_agreement(frame: pd.DataFrame, split_units: pd.DataFrame, model: ChurnModel,
                         thr: float) -> dict[str, Any]:
    """Score units built on true_customer_id; compare key labels with true-id labels."""
    from ml.features.churn_features import label_at

    cos = set(split_units["company_id"])
    f = frame[frame["company_id"].isin(cos) & frame["true_customer_id"].notna()]
    cut = split_units[["company_id", "cutoff_date"]].drop_duplicates()
    oracle_parts, agree, n_cmp = [], 0, 0
    for cid, g in f.groupby("company_id", sort=True):
        cutoffs = sorted(cut.loc[cut.company_id == cid, "cutoff_date"])
        pay_true = pd.DataFrame({"company_id": cid, "date": [d if isinstance(d, date) else pd.Timestamp(d).date()
                                                             for d in g["date"]],
                                 "amount": g["credit"].astype(float).to_numpy(),
                                 "key": g["true_customer_id"].to_numpy()})
        u = company_units(pay_true, cutoffs)
        if not u.empty:
            oracle_parts.append(u.assign(company_id=cid))
        # majority true id of each key's rows, then compare labels at each cutoff
        g2 = g.assign(date=pay_true["date"].to_numpy())
        su = split_units[split_units.company_id == cid]
        for c, gu in su.groupby("cutoff_date"):
            hist = g2[g2["date"] <= c]
            maj = hist.groupby("narration_key")["true_customer_id"].agg(lambda s: s.value_counts().index[0])
            keys = [k for k in gu["key"] if k in maj.index]
            if not keys:
                continue
            true_lab = label_at(pay_true, [maj[k] for k in keys], c)
            key_lab = gu.set_index("key").loc[keys, "y"].to_numpy()
            agree += int((true_lab == key_lab).sum())
            n_cmp += len(keys)
    out: dict[str, Any] = {"label_agreement_key_vs_true_id": agree / n_cmp if n_cmp else float("nan"),
                           "label_agreement_units": n_cmp}
    if oracle_parts:
        o = pd.concat(oracle_parts, ignore_index=True)
        o["p"] = model.predict_proba(o)
        a = o[is_active(o)]
        out["oracle_identity_active"] = {
            "n_units": int(len(a)), "pos_rate": float(a["y"].mean()),
            "pr_auc": pr_auc(a["y"].to_numpy(), a["p"].to_numpy()),
            "roc_auc": roc_auc(a["y"].to_numpy(), a["p"].to_numpy()),
            "at_threshold": prf(a["y"].to_numpy(), (a["p"].to_numpy() >= thr).astype(int)),
        }
    return out


# ── latency ────────────────────────────────────────────────────────────────

def latency(model: ChurnModel, version: str, units: pd.DataFrame, payments: pd.DataFrame,
            corpus_dir: Path, ident: dict[str, Any], runs: int) -> dict[str, Any]:
    from ml.anomaly.data import company_document, load_rows, predict_categories
    from ml.churn.data import PRED_CACHE_DIR
    from ml.churn.infer import ChurnScorer
    from ml.features.churn_features import features_at
    from pipeline.customer_identity import CustomerIdentityResolver
    from schema.canonical import TransactionCategory

    # features + scoring per 1,000 units (every company-cutoff of the split)
    jobs = [(g, c) for cid, g in payments.groupby("company_id")
            for c in sorted(set(units.loc[units.company_id == cid, "cutoff_date"]))]
    times: list[float] = []
    n_units = 0
    for _ in range(runs):
        t0 = time.perf_counter()
        n = 0
        for g, c in jobs:
            f = features_at(g, c)
            if not f.empty:
                model.predict_proba(f)
                n += len(f)
        times.append((time.perf_counter() - t0) / max(n, 1) * 1e6)   # ms per 1,000 units
        n_units = n
    # per statement: ChurnScorer.score on full documents (resolver ids, predicted categories)
    rows = load_rows(corpus_dir)
    cos = sorted(set(units["company_id"]))[:10]
    rows = rows[rows.company_id.isin(cos)]
    cats, _ = predict_categories(rows, PRED_CACHE_DIR, ident["corpus_hash"], n_jobs=1)
    docs = []
    for _, g in rows.groupby("company_id", sort=True):
        doc = company_document(g)
        doc = doc.model_copy(update={"transactions": [
            t.model_copy(update={"category": TransactionCategory(cats[t.transaction_id])})
            if cats.get(t.transaction_id) else t for t in doc.transactions]})
        docs.append(CustomerIdentityResolver(embedding_enabled=False).resolve(doc))
    scorer = ChurnScorer(model, version, decision_threshold=0.5, band_medium=0.2,
                         min_history_payments=2, active_window_days=90, rule_silence_days=60)
    st = []
    for _ in range(runs):
        for d in docs:
            t0 = time.perf_counter()
            scorer.score(d)
            st.append((time.perf_counter() - t0) * 1000)
    return {"ms_per_1k_units_median": round(statistics.median(times), 1),
            "units_timed": int(n_units), "statement_cutoffs_timed": len(jobs), "runs": runs,
            "ms_per_statement_median": round(statistics.median(st), 1) if st else None,
            "statements_timed": len(docs)}


# ── main ───────────────────────────────────────────────────────────────────

def evaluate(corpus_dir: Path, *, version: str, split: str, allow_unpinned: bool,
             dry_run: bool, n_jobs: int, out_root: Path) -> dict[str, Any]:
    cfg = load_config()
    ecfg = cfg["evaluate"]
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    loaded = registry.load(MODEL_NAME, version)
    model: ChurnModel = loaded.model
    sel = model.selected
    thr = float(sel["decision_threshold"])
    units, _ = load_units(corpus_dir, ident, n_jobs)
    f = units[units["split"] == split].reset_index(drop=True)
    if f.empty:
        raise RuntimeError(f"no {split} units")
    f["raw"] = model.raw_score(f)
    f["p"] = model.predict_proba(f)
    f["rule_silence"] = (f["days_since_last"] > int(sel["rule_silence_days"])).astype(int)
    payments, _ = corpus_payments(corpus_dir, ident, n_jobs)
    rules = rule_predictions(payments, f, n_jobs)
    f = f.merge(rules, on=["company_id", "cutoff_date", "key"], how="left", validate="one_to_one")
    if f[["rule_churn", "rule_regularity"]].isna().any().any():
        raise RuntimeError("rule predictions missing for some units")

    prof = pd.read_parquet(corpus_dir / "labels" / "companies.parquet", columns=["company_id", "profile"])
    f = f.merge(prof, on="company_id", how="left")
    n_boot, seed, top = int(ecfg["bootstrap"]), 0, float(ecfg["top_frac"])
    if dry_run:
        n_boot = min(n_boot, 50)
    act = is_active(f)
    res: dict[str, Any] = {
        "model_version": version, "split": split, "corpus": ident, "dry_run": dry_run,
        "selected": sel, "synthetic": True,
        "active": metric_block(f[act], thr, top, n_boot, seed),
        "all_units": metric_block(f, thr, top, n_boot, seed),
        "by_profile": {},
    }
    for p_name, g in f.groupby("profile", sort=True):
        ga = g[is_active(g)]
        res["by_profile"][p_name] = {
            "customer_level_churn_in_generator": p_name in CHURN_PROFILES,
            "units_all": int(len(g)), "units_active": int(len(ga)),
            "pos_rate_active": float(ga["y"].mean()) if len(ga) else None,
            "model_pr_auc_active": pr_auc(ga["y"].to_numpy(), ga["p"].to_numpy()),
            "baseline_pr_auc_active": pr_auc(ga["y"].to_numpy(), ga["days_since_last"].to_numpy()),
            "model_f1_active": prf(ga["y"].to_numpy(), (ga["p"].to_numpy() >= thr).astype(int))["f1"],
            "rule_churn_f1_all": prf(g["y"].to_numpy(), g["rule_churn"].to_numpy())["f1"],
        }
    churn_prof = f[f["profile"].isin(CHURN_PROFILES)]
    res["churn_profiles_active"] = metric_block(churn_prof[is_active(churn_prof)], thr, top, n_boot, seed)
    res["calibration_active"] = calibration_table(f.loc[act, "y"].to_numpy(), f.loc[act, "p"].to_numpy(),
                                                  int(ecfg["calibration_bins"]))

    frame = eval_frame(corpus_dir, ident, n_jobs)
    res["identity_audit"] = audit(frame)
    res["identity_oracle"] = oracle_and_agreement(frame, f, model, thr)
    res["latency"] = latency(model, version, f, payments, corpus_dir, ident, int(ecfg["latency_runs"]))

    out_dir = out_root / (f"dryrun-{corpus_dir.name}" if dry_run else version)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "metrics.json").write_text(json.dumps(res, indent=2, default=float) + "\n",
                                          encoding="utf-8", newline="\n")
    (out_dir / "report.md").write_text(report_md(res), encoding="utf-8", newline="\n")
    _plots(f, out_dir, int(ecfg["calibration_bins"]))
    res["out_dir"] = str(out_dir)
    return res


def _fmt(v: Any) -> str:
    if isinstance(v, dict) and "value" in v:
        return f"{v['value']:.3f} [{v['lo']:.3f}, {v['hi']:.3f}]"
    if isinstance(v, float):
        return "n/a" if v != v else f"{v:.3f}"
    return str(v)


def report_md(r: dict[str, Any]) -> str:
    lines = [f"# Churn evaluation: {r['model_version']} ({r['split']}{', DRY RUN' if r['dry_run'] else ''})",
             "", "SYNTHETIC data (generator 2.0.0). 95% CIs: company-cluster bootstrap.", ""]
    for pop in ("active", "all_units", "churn_profiles_active"):
        b = r[pop]
        m = b["model"]
        lines += [f"## {pop}: {b['n_units']} units, {b['n_companies']} companies, base rate {_fmt(b['pos_rate'])}", "",
                  "| scorer | PR-AUC | ROC-AUC | P | R | F1 | P@top10% (stmt) | P@top10% (global) |",
                  "|---|---|---|---|---|---|---|---|",
                  f"| model (thr {m['at_threshold']['threshold']}) | {_fmt(m['pr_auc'])} | {_fmt(m['roc_auc'])} | "
                  f"{_fmt(m['at_threshold']['precision'])} | {_fmt(m['at_threshold']['recall'])} | "
                  f"{_fmt(m['at_threshold']['f1_ci'])} | {_fmt(m['p_top_per_statement'])} | {_fmt(m['p_top_global'])} |"]
        bl = b["baseline_days_since_last"]
        lines.append(f"| days since last payment | {_fmt(bl['pr_auc'])} | {_fmt(bl['roc_auc'])} | | | | "
                     f"{_fmt(bl['p_top_per_statement'])} | {_fmt(bl['p_top_global'])} |")
        for k, lab in (("rule_churn_events", "rule: churn_events"), ("rule_regularity_alerts", "rule: regularity alert"),
                       ("rule_silence_fallback", "rule: silence > 60d")):
            x = b[k]
            f1 = _fmt(x["f1_ci"]) if "f1_ci" in x else _fmt(x["f1"])
            lines.append(f"| {lab} | | | {_fmt(x['precision'])} | {_fmt(x['recall'])} | {f1} | | |")
        lines += ["", f"Brier: calibrated {_fmt(m['brier_calibrated'])}, raw {_fmt(m['brier_raw'])}", ""]
    lines += ["## By profile", "", "| profile | customer churn in generator | units (active) | base rate | model PR-AUC | baseline PR-AUC | model F1 | rule churn F1 (all) |",
              "|---|---|---|---|---|---|---|---|"]
    for p, x in r["by_profile"].items():
        lines.append(f"| {p} | {x['customer_level_churn_in_generator']} | {x['units_active']} | {_fmt(x['pos_rate_active'])} | "
                     f"{_fmt(x['model_pr_auc_active'])} | {_fmt(x['baseline_pr_auc_active'])} | {_fmt(x['model_f1_active'])} | {_fmt(x['rule_churn_f1_all'])} |")
    ia = r["identity_audit"]
    lines += ["", "## Identity (evaluation only)", "", "| identity | purity | completeness | ids/company | true/company | true split | ids single-row |",
              "|---|---|---|---|---|---|---|"]
    for k in ("resolver", "narration_key"):
        q = ia[k]
        lines.append(f"| {k} | {q['purity']} | {q['completeness']} | {q['ids_per_company_median']} | "
                     f"{q['true_per_company_median']} | {q['true_split_over_ids']} | {q['ids_single_row']} |")
    io = r["identity_oracle"]
    lines += ["", f"Label agreement (key label vs true-id label): {_fmt(io['label_agreement_key_vs_true_id'])} "
                  f"over {io['label_agreement_units']} units."]
    if "oracle_identity_active" in io:
        o = io["oracle_identity_active"]
        lines.append(f"Oracle identity (true_customer_id units, same frozen model), active: {o['n_units']} units, "
                     f"base rate {_fmt(o['pos_rate'])}, PR-AUC {_fmt(o['pr_auc'])}, ROC-AUC {_fmt(o['roc_auc'])}, "
                     f"F1 {_fmt(o['at_threshold']['f1'])}.")
    lat = r["latency"]
    lines += ["", f"Latency: {lat['ms_per_1k_units_median']} ms per 1k units (features + scoring), "
                  f"{lat['ms_per_statement_median']} ms per statement (ChurnScorer.score), median of {lat['runs']} runs.", ""]
    return "\n".join(lines)


def write_thresholds(version: str, sel: dict[str, Any], path: Path = THRESHOLDS_PATH) -> None:
    from config.thresholds import update_section

    update_section(path, "churn", {
        "model_version": f'"{version}"',
        "decision_threshold": str(sel["decision_threshold"]),
        "band_medium": str(sel["band_medium"]),
        "min_history_payments": str(int(sel["min_history_payments"])),
        "rule_silence_days": str(int(sel["rule_silence_days"])),
    })


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--model-version")
    ap.add_argument("--split", choices=["test", "val"], default="test")
    ap.add_argument("--allow-unpinned", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="any corpus; results to dryrun-<corpus>/")
    ap.add_argument("--write-thresholds", action="store_true")
    ap.add_argument("--results", type=Path, default=Path("results/churn"))
    ap.add_argument("--n-jobs", type=int, default=3)
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    version = a.model_version
    if version is None:
        name = "selection_ci.json" if a.dry_run else "selection.json"
        version = json.loads((a.results / name).read_text(encoding="utf-8"))["model_version"]
    res = evaluate(a.corpus, version=version, split=a.split, allow_unpinned=a.allow_unpinned,
                   dry_run=a.dry_run, n_jobs=a.n_jobs, out_root=a.results)
    print(report_md(res))
    print(f"results: {res['out_dir']}")
    if a.write_thresholds and not a.dry_run:
        write_thresholds(version, res["selected"])
        print("thresholds.yaml churn section updated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
