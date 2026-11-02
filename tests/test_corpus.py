"""Phase 3 corpus: determinism, schema, label isolation, generator semantics.

Builds a tiny corpus (8 companies, <= 12 months) once per module.
"""
from __future__ import annotations

import re
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from datasets.build_corpus import build, pdf_indices
from datasets.schema import (
    FEATURE_SCHEMAS,
    FORBIDDEN_FEATURE_TOKENS,
    JOIN_KEYS,
    LABEL_SCHEMAS,
)

N, SEED, MONTHS = 8, 42, 12


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("corpus") / "v1-test"
    manifest = build(n_companies=N, seed=SEED, out_dir=out, max_months=MONTHS,
                     pdf_subset=1, workers=1, quiet=True)
    return out, manifest


# ── determinism ─────────────────────────────────────────────────────────────

def test_rebuild_gives_identical_hash(corpus, tmp_path: Path) -> None:
    _, m1 = corpus
    m2 = build(n_companies=N, seed=SEED, out_dir=tmp_path / "again", max_months=MONTHS,
               pdf_subset=1, workers=2, quiet=True)
    assert m2["corpus_hash"] == m1["corpus_hash"]
    assert m2["files"] == m1["files"]


def test_different_seed_gives_different_hash(corpus, tmp_path: Path) -> None:
    _, m1 = corpus
    m2 = build(n_companies=N, seed=SEED + 1, out_dir=tmp_path / "other", max_months=MONTHS,
               pdf_subset=1, workers=1, quiet=True)
    assert m2["corpus_hash"] != m1["corpus_hash"]


def test_manifest_has_no_timestamp_and_marks_synthetic(corpus) -> None:
    out, m = corpus
    text = (out / "manifest.json").read_text(encoding="utf-8")
    assert m["synthetic"] is True
    assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", text)


def test_refuses_to_overwrite_a_non_corpus_directory(tmp_path: Path) -> None:
    victim = tmp_path / "precious"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    with pytest.raises(SystemExit):
        build(n_companies=1, seed=1, out_dir=victim, max_months=12, pdf_subset=0, quiet=True)
    assert (victim / "keep.txt").exists()


# ── label isolation ─────────────────────────────────────────────────────────

def test_feature_files_have_exactly_the_feature_schema(corpus) -> None:
    out, _ = corpus
    feature_files = sorted(p.name for p in (out / "features").iterdir())
    assert feature_files == sorted(f"{n}.parquet" for n in FEATURE_SCHEMAS)
    for name, schema in FEATURE_SCHEMAS.items():
        assert pq.read_schema(out / "features" / f"{name}.parquet").names == schema.names


def test_no_label_column_reachable_from_feature_tables(corpus) -> None:
    out, _ = corpus
    feature_cols = {c for n in FEATURE_SCHEMAS
                    for c in pq.read_schema(out / "features" / f"{n}.parquet").names}
    label_cols = {c for n in LABEL_SCHEMAS
                  for c in pq.read_schema(out / "labels" / f"{n}.parquet").names}
    assert feature_cols & label_cols <= JOIN_KEYS
    leaked = {c for c in feature_cols for tok in FORBIDDEN_FEATURE_TOKENS if tok in c.lower()}
    assert not leaked, leaked


def test_duckdb_feature_views_expose_no_label_columns(corpus) -> None:
    from datasets.duckdb_stats import connect

    out, _ = corpus
    con = connect(out)
    views = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    assert views == {"transactions", "companies"}
    cols = {r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns").fetchall()}
    assert not {c for c in cols for tok in FORBIDDEN_FEATURE_TOKENS if tok in c.lower()}


def test_txn_ids_are_opaque(corpus) -> None:
    out, _ = corpus
    ids = pq.read_table(out / "features" / "transactions.parquet", columns=["txn_id"])
    pat = re.compile(r"^txn_[0-9a-f]{16}$")
    vals = ids.column("txn_id").to_pylist()
    assert all(pat.match(v) for v in vals)
    assert len(set(vals)) == len(vals)


_LABEL_WORDS = re.compile(
    r"\b(VENDOR_PAYMENT|FOUNDER_WITHDRAWAL|LOAN_IN|LOAN_OUT|REVENUE|ML_ONLY|ML_\w+|ANOMALY|"
    r"CHURN\w*|STRUCTURING|ROUND_TRIPPING|COMPLIANCE\w*|HEALTH|SIGNALLER|TEMPLATE|"
    r"HYPERION MEGA CORP|ZENITH CAPITAL ADVISORY|ENTERPRISE SOFTWARE SALES)\b", re.I)


def test_narrations_carry_no_label_tokens(corpus) -> None:
    out, _ = corpus
    desc = pq.read_table(out / "features" / "transactions.parquet",
                         columns=["description"]).column("description").to_pylist()
    hits = [d for d in desc if _LABEL_WORDS.search(d)]
    assert not hits, hits[:5]


# ── statement semantics ─────────────────────────────────────────────────────

def test_balances_are_continuous_and_rows_ordered(corpus) -> None:
    out, _ = corpus
    tx = pq.read_table(out / "features" / "transactions.parquet").to_pylist()
    co = {r["company_id"]: r for r in
          pq.read_table(out / "features" / "companies.parquet").to_pylist()}
    by_co: dict[str, list[dict]] = {}
    for r in tx:
        by_co.setdefault(r["company_id"], []).append(r)
    assert set(by_co) == set(co)
    for cid, rows in by_co.items():
        bal = co[cid]["opening_balance"]
        keys = [(r["date"], r["txn_time"]) for r in rows]
        assert keys == sorted(keys)
        assert [r["row_seq"] for r in rows] == list(range(len(rows)))
        for r in rows:
            bal = bal + (r["credit"] or Decimal(0)) - (r["debit"] or Decimal(0))
            assert r["balance"] == bal
            assert r["txn_time"] is not None


def test_label_rows_align_with_feature_rows(corpus) -> None:
    out, _ = corpus
    f = pq.read_table(out / "features" / "transactions.parquet", columns=["txn_id"])
    lab = pq.read_table(out / "labels" / "transactions.parquet", columns=["txn_id"])
    assert f.column("txn_id").to_pylist() == lab.column("txn_id").to_pylist()


def test_no_customer_payment_long_after_churn(corpus) -> None:
    out, _ = corpus
    cust = pq.read_table(out / "labels" / "customers.parquet").to_pylist()
    churned = {(c["company_id"], c["customer_id"]): c["churn_month_start"]
               for c in cust if c["churn_month"] is not None}
    assert churned, "tiny corpus should contain churned customers"
    lab = pq.read_table(out / "labels" / "transactions.parquet",
                        columns=["txn_id", "company_id", "true_customer_id"]).to_pylist()
    dates = dict(zip(
        pq.read_table(out / "features" / "transactions.parquet", columns=["txn_id"]).column(0).to_pylist(),
        pq.read_table(out / "features" / "transactions.parquet", columns=["date"]).column(0).to_pylist(),
        strict=True))
    for r in lab:
        start = churned.get((r["company_id"], r["true_customer_id"]))
        if start is not None:
            # Only a late payment for the month before churn may land after
            # churn_month_start (pay day <= 25 + delay <= 40 days).
            assert dates[r["txn_id"]] < start + timedelta(days=40)


def test_pdf_subset_rendered_with_truth(corpus) -> None:
    out, m = corpus
    pdfs = sorted((out / "pdf").glob("*.pdf"))
    assert len(pdfs) == len(pdf_indices(N, 1)) == 1
    assert pdfs[0].read_bytes()[:4] == b"%PDF"
    assert (out / "labels" / "pdf" / f"{pdfs[0].stem}.truth.json").exists()


def test_pdf_subset_round_trips_through_ingestion(corpus) -> None:
    from pipeline.run import run_analysis

    out, _ = corpus
    pdf = sorted((out / "pdf").glob("*.pdf"))[0]
    res = run_analysis(pdf.read_bytes(), pdf.name)
    n_rows = sum(1 for r in pq.read_table(out / "features" / "transactions.parquet",
                                          columns=["company_id"]).column(0).to_pylist()
                 if r == pdf.stem)
    assert len(res.doc.transactions) == n_rows


# ── ml_only injectors vs the real rule detectors ────────────────────────────

def _doc(txns):
    from schema.canonical import CanonicalTransaction, SourceReference, StatementDocument

    rows = [CanonicalTransaction(
        transaction_id=t.txn_id, date=t.date, description=t.description,
        debit=t.debit, credit=t.credit, balance=t.balance,
        source_reference=SourceReference(file_path="corpus", page=0, row=i, raw_text=""),
        category=t.category, counterparty=t.counterparty_key, customer_id=t.customer_id,
    ) for i, t in enumerate(txns)]
    return StatementDocument(account_id="x", statement_period_start=txns[0].date,
                             statement_period_end=txns[-1].date, source_format="corpus",
                             transactions=rows)


def _ml_specs(k: int):
    from tools.synthetic_corpus import make_spec

    out, i = [], 0
    while len(out) < k:
        s = make_spec(i, 4242, max_months=12)
        if s.ml_flags:
            out.append(s)
        i += 1
    return out


def test_ml_only_rows_are_not_flagged_by_rule_detectors() -> None:
    from analysis import risk
    from tools.synthetic_corpus import generate_company

    detectors = [risk.detect_structuring, risk.detect_round_tripping, risk.detect_spikes_drains,
                 risk.detect_customer_concentration, risk.detect_round_amount_clustering,
                 risk.detect_founder_over_extraction]
    injected = 0
    for spec in _ml_specs(12):
        res = generate_company(spec)
        ml_ids = {t.txn_id for t in res.txns
                  if any(f.startswith("ml_") for f in t.anomaly_flags)}
        injected += bool(ml_ids)
        doc = _doc(res.txns)
        for det in detectors:
            for flag in det(doc):
                assert not ml_ids & set(flag.triggering_transaction_ids), (spec, det.__name__)
    assert injected >= 8


def test_ml_only_injection_leaves_the_base_statement_unchanged() -> None:
    from tools.synthetic_corpus import generate_company

    for spec in _ml_specs(4):
        with_ml = generate_company(spec)
        base = generate_company(spec, with_ml_only=False)
        kept = [t for t in with_ml.txns if not any(f.startswith("ml_") for f in t.anomaly_flags)]
        key = lambda t: (t.txn_id, t.date, t.description, t.debit, t.credit, t.txn_time)  # noqa: E731
        assert sorted(map(key, kept)) == sorted(map(key, base.txns))


def test_ml_only_amounts_are_non_round_debits() -> None:
    from tools.synthetic_corpus import generate_company

    for spec in _ml_specs(12):
        for t in generate_company(spec).txns:
            if any(f.startswith("ml_") for f in t.anomaly_flags):
                assert t.credit is None and t.debit is not None
                assert t.debit % 1000 != 0 and t.debit != t.debit.to_integral_value()
                assert "CASH" not in t.description.upper()


def test_off_hours_rows_are_at_night() -> None:
    from tools.synthetic_corpus import generate_company

    for spec in _ml_specs(12):
        for t in generate_company(spec).txns:
            if "ml_off_hours_burst" in t.anomaly_flags:
                assert "01:00:00" <= t.txn_time <= "04:30:00"


def test_tax_templates_still_match_the_compliance_injector_regexes() -> None:
    from tools import synthetic_gen as sg
    from tools.synthetic_corpus import _TEMPLATES

    for bank, modes in _TEMPLATES.items():
        for fmt in (f for _, f in modes["gst"]):
            assert sg._GST_STRIP_RE.search(fmt), (bank, fmt)
        for fmt in (f for _, f in modes["tds"]):
            assert sg._TDS_STRIP_RE.search(fmt), (bank, fmt)
