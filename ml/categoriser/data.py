"""Corpus loading for the categoriser, with the hash gate.

* Features come only from ``features/transactions.parquet``.
* The label is only ``labels/transactions.parquet:true_category``.
* ``counterparty_key`` (unseen-vendor split) and ``template_id`` (slicing
  in reports) are read from labels for GROUPING/SLICING only, held in
  :class:`CorpusData` separately from the feature frame and never fed to a model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
FEATURE_COLUMNS = ["txn_id", "company_id", "date", "description", "debit", "credit"]


class CorpusGateError(RuntimeError):
    pass


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)
    return cfg


def corpus_identity(corpus_dir: Path) -> dict[str, Any]:
    m = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    sp = json.loads((corpus_dir / "splits" / "manifest.json").read_text(encoding="utf-8"))
    return {
        "corpus_dir": corpus_dir.name,
        "corpus_version": m.get("corpus_version"),
        "generator_version": m.get("generator_version"),
        "corpus_hash": m["corpus_hash"],
        "splits_hash": sp["splits_hash"],
    }


def check_gate(corpus_dir: Path, cfg: dict[str, Any], allow_unpinned: bool = False) -> dict[str, Any]:
    """Return the corpus identity; raise unless hashes match the pinned values."""
    ident = corpus_identity(corpus_dir)
    pinned = (cfg.get("pinned_corpora") or {}).get(corpus_dir.name)
    if pinned is None:
        if not allow_unpinned:
            raise CorpusGateError(f"corpus {corpus_dir.name!r} is not pinned in config.yaml; "
                                  "pass --allow-unpinned to use it (recorded in metadata)")
        ident["pinned"] = False
        return ident
    for k in ("corpus_hash", "splits_hash"):
        if ident[k] != pinned[k]:
            raise CorpusGateError(f"{k} mismatch for {corpus_dir.name}: got {ident[k]}, "
                                  f"expected {pinned[k]}. Rebuild with `make corpus`.")
    ident["pinned"] = True
    return ident


@dataclass
class CorpusData:
    rows: pd.DataFrame          # FEATURE_COLUMNS only (+ row order = labels order)
    y: np.ndarray               # true_category
    groups: pd.DataFrame        # company_id, bank, counterparty_key, template_id (NOT features)
    identity: dict[str, Any]

    def split_masks(self, corpus_dir: Path, split_file: str) -> dict[str, np.ndarray]:
        sp = pd.read_parquet(corpus_dir / "splits" / split_file)
        if "company_id" in sp.columns:
            assign = self.groups["company_id"].map(dict(zip(sp.company_id, sp.split, strict=True)))
        else:
            key = self.groups["counterparty_key"].fillna("OTHER")
            assign = key.map(dict(zip(sp.counterparty_key, sp.split, strict=True)))
        if assign.isna().any():
            raise CorpusGateError(f"{split_file}: {int(assign.isna().sum())} rows without a split")
        return {s: (assign == s).to_numpy() for s in ("train", "val", "test")}


def load_corpus(corpus_dir: Path, cfg: dict[str, Any], allow_unpinned: bool = False) -> CorpusData:
    ident = check_gate(corpus_dir, cfg, allow_unpinned)
    feats = pd.read_parquet(corpus_dir / "features" / "transactions.parquet", columns=FEATURE_COLUMNS)
    labels = pd.read_parquet(corpus_dir / "labels" / "transactions.parquet",
                             columns=["txn_id", "true_category", "counterparty_key", "template_id"])
    companies = pd.read_parquet(corpus_dir / "features" / "companies.parquet",
                                columns=["company_id", "bank"])
    df = feats.merge(labels, on="txn_id", how="inner", validate="one_to_one")
    if len(df) != len(feats):
        raise CorpusGateError("feature/label row mismatch")
    df = df.merge(companies, on="company_id", how="left", validate="many_to_one")
    rows = df[FEATURE_COLUMNS].reset_index(drop=True)
    groups = df[["company_id", "bank", "counterparty_key", "template_id"]].reset_index(drop=True)
    return CorpusData(rows=rows, y=df["true_category"].to_numpy(), groups=groups, identity=ident)
