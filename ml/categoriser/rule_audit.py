"""Rule precision audit (D10): how precise is each v2 rule on the corpus?

    python -m ml.categoriser.rule_audit data/corpus/v1 [--out results/categoriser/rule_audit]

Selection uses company-split TRAIN+VAL only. Test precision is reported
alongside but never used to select. For each rule in analysis.categoriser._RULES:

* first-match support/precision: the rows where it is the rule that fires (what v2 does)
* standalone support/precision: every row its pattern + direction matches,
  ignoring order. Shows how much a rule depends on earlier rules
* template concentration: distinct template_ids among first-match hits and
  the top template's share. A rule that only fires on one or two generator
  templates is flagged as possibly fitted to generator quirks
* the top wrong labels

Decision → ``high_precision`` (enters routing step 1) or a reason it doesn't.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from analysis.categoriser import _RULES, RULE_NAMES, match_rule_fields
from ml.categoriser.data import CorpusData, load_config, load_corpus

DEFAULT_OUT = Path("results/categoriser/rule_audit")


def first_match_ids(desc: pd.Series, is_credit: np.ndarray) -> np.ndarray:
    out = np.empty(len(desc), dtype=np.int16)
    for i, (d, c) in enumerate(zip(desc.tolist(), is_credit.tolist(), strict=True)):
        hit = match_rule_fields(d or "", bool(c))
        out[i] = -1 if hit is None else hit[0]
    return out


def standalone_matches(desc: pd.Series, is_credit: np.ndarray, idx: int) -> np.ndarray:
    pattern, requires_credit, _ = _RULES[idx]
    dir_ok = np.ones(len(desc), dtype=bool) if requires_credit is None else (is_credit == requires_credit)
    if pattern is None:
        return dir_ok
    pat_ok = desc.fillna("").str.contains(pattern, regex=True).to_numpy()
    out: np.ndarray = dir_ok & pat_ok
    return out


def _prec(y: np.ndarray, mask: np.ndarray, cat: str) -> tuple[int, float | None]:
    n = int(mask.sum())
    return n, (float((y[mask] == cat).mean()) if n else None)


def audit(data: CorpusData, corpus_dir: Path, cfg: dict[str, Any]) -> pd.DataFrame:
    ac = cfg["rule_audit"]
    masks = data.split_masks(corpus_dir, cfg["splits"]["company"])
    sel = masks["train"] | masks["val"]
    test = masks["test"]
    desc = data.rows["description"]
    is_credit = data.rows["credit"].notna().to_numpy()
    fm = first_match_ids(desc, is_credit)
    tmpl = data.groups["template_id"].to_numpy()
    y = data.y

    recs = []
    for idx, (_, _, category) in enumerate(_RULES):
        cat = category.value
        name = RULE_NAMES[idx]
        hit = fm == idx
        n_sel, p_sel = _prec(y, hit & sel, cat)
        n_test, p_test = _prec(y, hit & test, cat)
        sa = standalone_matches(desc, is_credit, idx)
        n_sa, p_sa = _prec(y, sa & sel, cat)
        t_hits = pd.Series(tmpl[hit & sel])
        n_tmpl = int(t_hits.nunique())
        top_share = float(t_hits.value_counts(normalize=True).iloc[0]) if len(t_hits) else None
        inline_share = float(t_hits.str.contains(":inline:").mean()) if len(t_hits) else None
        wrong = pd.Series(y[hit & sel & (y != cat)]).value_counts().head(3)

        concentrated = n_sel > 0 and (
            n_tmpl < ac["min_distinct_templates"] or (top_share or 0) > ac["max_top_template_share"]
        )
        flags = []
        if name in ac["known_generator_fitted"]:
            flags.append("known_generator_fitted")
        if concentrated:
            flags.append("template_concentrated")

        if name in ac["excluded_by_construction"]:
            decision = "excluded_by_construction"
        elif n_sel < ac["min_support"]:
            decision = "low_support"
        elif p_sel is None or p_sel < ac["min_precision"]:
            decision = "below_precision"
        elif flags:
            decision = "flagged_generator_fit"
        else:
            decision = "high_precision"

        recs.append({
            "rule_id": idx, "rule": name, "category": cat,
            "first_match_n": n_sel, "first_match_precision": p_sel,
            "standalone_n": n_sa, "standalone_precision": p_sa,
            "distinct_templates": n_tmpl, "top_template_share": top_share,
            "inline_share": inline_share,
            "top_errors": "; ".join(f"{k}:{v}" for k, v in wrong.items()),
            "flags": ",".join(flags), "decision": decision,
            "test_first_match_n": n_test, "test_first_match_precision": p_test,
        })
    return pd.DataFrame(recs)


def _fmt(v: Any) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return "n/a"
    if isinstance(v, float):
        return f"{v:.4f}"
    return str(v)


def to_markdown(df: pd.DataFrame) -> str:
    cols = ["rule_id", "rule", "category", "first_match_n", "first_match_precision",
            "standalone_precision", "distinct_templates", "top_template_share",
            "flags", "decision", "test_first_match_precision"]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for _, r in df.iterrows():
        lines.append("| " + " | ".join(_fmt(r[c]) for c in cols) + " |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("corpus_dir", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--allow-unpinned", action="store_true")
    a = ap.parse_args(argv)
    cfg = load_config()
    data = load_corpus(a.corpus_dir, cfg, a.allow_unpinned)
    df = audit(data, a.corpus_dir, cfg)
    a.out.mkdir(parents=True, exist_ok=True)
    df.to_csv(a.out / "rule_audit.csv", index=False, lineterminator="\n")
    md = to_markdown(df)
    (a.out / "rule_audit.md").write_text(md, encoding="utf-8", newline="\n")
    hp = [int(i) for i in df.loc[df.decision == "high_precision", "rule_id"]]
    summary = {"corpus": data.identity, "selection_rows": "company train+val",
               "high_precision_rules": hp}
    (a.out / "rule_audit.json").write_text(json.dumps(summary, indent=2) + "\n",
                                           encoding="utf-8", newline="\n")
    print(md)
    print("high_precision_rules:", hp)
    pd.set_option("display.width", 200)
    print(df[["rule", "top_errors", "inline_share", "standalone_n", "test_first_match_n"]].to_string())


if __name__ == "__main__":
    main()
