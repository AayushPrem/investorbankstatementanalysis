"""Corpus access for the anomaly model.

The model's inputs come from ``features/`` only:
``txn_id, company_id, account_id, date, row_seq, description, debit, credit, balance``.
``pred_category`` comes from the Phase 4 categoriser run over those rows: v3
if the model resolves, otherwise the v2 rules. It is cached per corpus hash and
categoriser version.

Labels are read only by :func:`load_eval_labels`, which training uses for
validation-set selection and evaluation uses on test.
``true_category`` and ``counterparty_key`` are never read here.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from ml.categoriser.data import CorpusGateError, check_gate  # reuse the hash gate

log = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
FEATURE_COLUMNS = ["txn_id", "company_id", "account_id", "date", "row_seq",
                   "description", "debit", "credit", "balance"]
KINDS = ("rule", "compliance", "ml_only")

__all__ = ["CorpusGateError", "check_gate", "load_config", "load_rows", "company_splits",
           "predict_categories", "load_eval_labels", "rule_flags", "KINDS"]


def load_config(path: Path | None = None) -> dict[str, Any]:
    with (path or CONFIG_PATH).open(encoding="utf-8") as fh:
        cfg: dict[str, Any] = yaml.safe_load(fh)
    return cfg


def load_rows(corpus_dir: Path) -> pd.DataFrame:
    rows = pd.read_parquet(corpus_dir / "features" / "transactions.parquet", columns=FEATURE_COLUMNS)
    return rows.sort_values(["company_id", "row_seq"], kind="mergesort").reset_index(drop=True)


def company_splits(corpus_dir: Path) -> dict[str, str]:
    sp = pd.read_parquet(corpus_dir / "splits" / "company.parquet")
    return dict(zip(sp.company_id, sp.split, strict=True))


# ── documents from corpus rows (no labels) ──────────────────────────────────

def _money(v: Any) -> Decimal | None:
    if v is None or (isinstance(v, float) and v != v):
        return None
    return Decimal(str(v))


def company_document(g: pd.DataFrame) -> Any:
    """A StatementDocument for one company's feature rows (statement order)."""
    from schema.canonical import (
        CanonicalTransaction,
        SourceReference,
        StatementDocument,
        ValidationStatus,
    )

    g = g.sort_values("row_seq", kind="mergesort")
    txns = []
    for i, r in enumerate(g.itertuples(index=False)):
        d = r.date if isinstance(r.date, date) else pd.Timestamp(r.date).date()
        txns.append(CanonicalTransaction(
            transaction_id=r.txn_id, date=d, description=r.description or "",
            debit=_money(r.debit), credit=_money(r.credit), balance=_money(r.balance) or Decimal("0"),
            source_reference=SourceReference(file_path="corpus", page=0, row=i, raw_text=""),
        ))
    return StatementDocument(
        account_id=str(g["account_id"].iloc[0]),
        statement_period_start=min(t.date for t in txns),
        statement_period_end=max(t.date for t in txns),
        source_format="corpus", validation_status=ValidationStatus.UNVALIDATED,
        transactions=txns,
    )


def _categorise_company(g: pd.DataFrame) -> tuple[list[str], list[str | None], str | None]:
    from analysis.categoriser import Categoriser

    doc = company_document(g)
    cat = Categoriser(llm_enabled=False)
    doc, stats = cat.categorise(doc)
    ids = [t.transaction_id for t in doc.transactions]
    cats = [None if t.category is None else str(getattr(t.category, "value", t.category))
            for t in doc.transactions]
    return ids, cats, stats.model_version


def categoriser_version() -> str:
    from ml.categoriser.infer import resolve_model

    m = resolve_model()
    return "v2-rules" if m is None else m.version


def predict_categories(rows: pd.DataFrame, cache_dir: Path, corpus_hash: str,
                       n_jobs: int = 3) -> tuple[pd.Series, str]:
    """Predicted category per txn_id (Phase 4 categoriser, LLM off), cached."""
    ver = categoriser_version()
    cache = cache_dir / f"pred_categories-{corpus_hash[:8]}-{ver}.parquet"
    if cache.exists():
        df = pd.read_parquet(cache)
        if set(df.txn_id) >= set(rows.txn_id):
            log.info("predicted categories from cache", extra={"rows": len(df)})
            return pd.Series(df.pred_category.to_numpy(), index=df.txn_id), ver
    groups = [g for _, g in rows.groupby("company_id", sort=True)]
    ids: list[str] = []
    cats: list[str | None] = []
    seen_versions: set[str | None] = set()
    if n_jobs > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            results = list(ex.map(_categorise_company, groups, chunksize=8))
    else:
        results = [_categorise_company(g) for g in groups]
    for i, c, v in results:
        ids += i
        cats += c
        seen_versions.add(v)
    if len(seen_versions) != 1:
        raise RuntimeError(f"categoriser version changed during prediction: {seen_versions}")
    cache_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame({"txn_id": ids, "pred_category": cats}).to_parquet(cache, index=False)
    return pd.Series(cats, index=ids), ver


# ── labels (evaluation / validation selection only) ─────────────────────────

@dataclass
class EvalLabels:
    frame: pd.DataFrame     # txn_id, company_id, any, rule, compliance, ml_only, <ml type>...
    ml_types: tuple[str, ...]


def load_eval_labels(corpus_dir: Path) -> EvalLabels:
    from tools.synthetic_corpus import ANOMALY_KIND

    lab = pd.read_parquet(corpus_dir / "labels" / "transactions.parquet",
                          columns=["txn_id", "company_id", "anomaly_flags"])
    ml_types = tuple(sorted(t for t, k in ANOMALY_KIND.items() if k == "ml_only"))
    flags = lab["anomaly_flags"].map(lambda fl: list(fl) if fl is not None else [])
    out = pd.DataFrame({"txn_id": lab.txn_id, "company_id": lab.company_id})
    out["any"] = flags.map(bool)
    for kind in KINDS:
        out[kind] = flags.map(lambda fl, k=kind: any(ANOMALY_KIND.get(f) == k for f in fl))
    for t in ml_types:
        out[t] = flags.map(lambda fl, t=t: t in fl)
    return EvalLabels(out, ml_types)


# ── rule detectors on corpus companies (comparison only) ────────────────────

def _rules_company(g: pd.DataFrame) -> tuple[str, list[str], list[str]]:
    """Run the pipeline's deterministic stages (LLM off) with predicted categories."""
    from analysis.categoriser import Categoriser
    from analysis.compliance import ComplianceAnalyst
    from analysis.risk import RiskAnalyst
    from jurisdictions.india import IndiaJurisdictionModule
    from pipeline.customer_identity import CustomerIdentityResolver
    from pipeline.related_party import RelatedPartyTagger

    doc = company_document(g)
    doc, _ = Categoriser(llm_enabled=False).categorise(doc)
    doc = RelatedPartyTagger(llm_enabled=False).tag(doc, [])
    doc = CustomerIdentityResolver(embedding_enabled=False).resolve(doc)
    risk = RiskAnalyst(llm_enabled=False).analyse(doc)
    comp = ComplianceAnalyst(llm_enabled=False).analyse(doc, IndiaJurisdictionModule())
    risk_ids = sorted({i for f in risk.flags for i in f.triggering_transaction_ids})
    comp_ids = sorted({i for e in comp.exceptions for i in e.triggering_transaction_ids})
    return str(g["company_id"].iloc[0]), risk_ids, comp_ids


def rule_flags(rows: pd.DataFrame, company_ids: Sequence[str], n_jobs: int = 3) -> pd.DataFrame:
    """txn_id, company_id, risk_flagged, compliance_flagged for the given companies."""
    sub = rows[rows.company_id.isin(set(company_ids))]
    groups = [g for _, g in sub.groupby("company_id", sort=True)]
    if n_jobs > 1:
        with ProcessPoolExecutor(max_workers=n_jobs) as ex:
            res = list(ex.map(_rules_company, groups, chunksize=4))
    else:
        res = [_rules_company(g) for g in groups]
    risk = {i for _, r, _ in res for i in r}
    comp = {i for _, _, c in res for i in c}
    out = sub[["txn_id", "company_id"]].copy()
    out["risk_flagged"] = out.txn_id.isin(risk)
    out["compliance_flagged"] = out.txn_id.isin(comp)
    return out.reset_index(drop=True)
