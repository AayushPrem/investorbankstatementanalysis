"""Build the synthetic corpus: N companies straight to Parquet (+ a small PDF subset).

    python -m datasets.build_corpus --n-companies 1000 --seed 42
    python -m datasets.build_corpus --small            # 50 companies, <=12 months, 5 PDFs (CI)

Output (default ``data/corpus/v1``, ``data/corpus/v1-small`` with --small)::

    features/transactions.parquet   statement rows only (no labels)
    features/companies.parquet      statement header facts
    labels/transactions.parquet     true_category, template_id, anomaly_*, true_customer_id, ...
    labels/customers.parquet        latent health, churn month, ...
    labels/companies.parquet        profile, generator parameters, injected flags
    labels/flags.parquet            one row per injected flag
    labels/pdf/<company>.truth.json ground truth for the rendered subset
    pdf/<company>.pdf               rendered statements (ingestion end-to-end checks)
    manifest.json                   args, versions, row counts, per-file sha256, corpus_hash

Reproducibility: each company is generated from a seed derived from
(--seed, company index), independently of every other company, so the worker
count doesn't change the output. Parquet files carry no wall-clock metadata
and manifest.json has no timestamp, so two builds with the same arguments (and
the same pyarrow version) are byte-identical; ``corpus_hash`` covers every file.

All data is SYNTHETIC (tools/synthetic_gen.py, see synthetic_gen.GENERATOR_VERSION).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass
from datetime import date
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from datasets.schema import CORPUS_VERSION, FEATURE_SCHEMAS, LABEL_SCHEMAS

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO_ROOT / "data" / "corpus"
ROW_GROUP_SIZE = 250_000


@dataclass(frozen=True)
class BuildArgs:
    n_companies: int
    seed: int
    max_months: int
    pdf_subset: int
    enable_ml_only: bool = True


def pdf_indices(n_companies: int, k: int) -> set[int]:
    """Evenly spread company indices that get a rendered PDF."""
    if k <= 0 or n_companies <= 0:
        return set()
    k = min(k, n_companies)
    return {(j * n_companies) // k for j in range(k)}


def _time(s: str | None) -> dtime | None:
    if s is None:
        return None
    h, m, sec = (int(x) for x in s.split(":"))
    return dtime(h, m, sec)


def _money(v: Decimal | None) -> Decimal | None:
    return None if v is None else v.quantize(Decimal("0.01"))


def _company_tables(index: int, args: BuildArgs, stage: str) -> dict[str, pa.Table]:
    """Generate one company and return its rows as pyarrow tables keyed by
    "features/<name>" / "labels/<name>"."""
    from tools.synthetic_corpus import ANOMALY_KIND, make_spec
    from tools.synthetic_corpus import generate_company as gen

    spec = make_spec(index, args.seed, max_months=args.max_months,
                     enable_ml_only=args.enable_ml_only)
    render = index in pdf_indices(args.n_companies, args.pdf_subset)
    stage_dir = Path(stage)
    res = gen(spec,
              pdf_dir=stage_dir / "pdf" if render else None,
              truth_dir=stage_dir / "labels" / "pdf" if render else None)
    company, txns = res.company, res.txns

    ft: dict[str, list[Any]] = {f.name: [] for f in FEATURE_SCHEMAS["transactions"]}
    lt: dict[str, list[Any]] = {f.name: [] for f in LABEL_SCHEMAS["transactions"]}
    payments: dict[str, list[date]] = {}
    for seq, t in enumerate(txns):
        ft["txn_id"].append(t.txn_id)
        ft["company_id"].append(spec.company_id)
        ft["account_id"].append(company.account_no)
        ft["bank"].append(spec.bank)
        ft["date"].append(t.date)
        ft["txn_time"].append(_time(t.txn_time))
        ft["value_date"].append(t.value_date)
        ft["description"].append(t.description)
        ft["ref_no"].append(t.ref_no)
        ft["debit"].append(_money(t.debit))
        ft["credit"].append(_money(t.credit))
        ft["balance"].append(_money(t.balance))
        ft["row_seq"].append(seq)

        first = t.anomaly_flags[0] if t.anomaly_flags else None
        lt["txn_id"].append(t.txn_id)
        lt["company_id"].append(spec.company_id)
        lt["true_category"].append(t.category)
        lt["template_id"].append(t.template_id)
        lt["anomaly_type"].append(first)
        lt["anomaly_kind"].append(ANOMALY_KIND.get(first) if first else None)
        lt["anomaly_flags"].append(list(t.anomaly_flags))
        lt["true_customer_id"].append(t.customer_id)
        lt["counterparty_key"].append(t.counterparty_key)
        lt["is_related_party"].append(t.is_related_party)
        if t.customer_id and t.category == "REVENUE" and t.credit is not None:
            payments.setdefault(t.customer_id, []).append(t.date)

    fc = {
        "company_id": [spec.company_id], "bank": [spec.bank],
        "account_id": [company.account_no], "account_holder": [company.name],
        "period_start": [txns[0].date if txns else spec.start],
        "period_end": [txns[-1].date if txns else spec.start],
        "opening_balance": [_money(company.opening_balance)],
    }

    from tools.synthetic_corpus import _add_months

    lc: dict[str, list[Any]] = {f.name: [] for f in LABEL_SCHEMAS["customers"]}
    for cid, lat in res.latents.items():
        pays = sorted(payments.get(cid, []))
        lc["company_id"].append(spec.company_id)
        lc["customer_id"].append(cid)
        lc["customer_name"].append(lat.name)
        lc["health"].append(round(lat.health, 6))
        lc["acquired_month"].append(lat.acquired_month)
        lc["churn_month"].append(lat.churn_month)
        lc["churn_month_start"].append(
            _add_months(spec.start, lat.churn_month) if lat.churn_month is not None else None)
        lc["signaller"].append(lat.signaller)
        lc["lead_months"].append(lat.lead_months if lat.signaller else None)
        lc["n_payments"].append(len(pays))
        lc["last_payment_date"].append(pays[-1] if pays else None)

    rule_flags = [f["flag_type"] for f in res.injected_flags if f.get("kind") != "ml_only"]
    ml_flags = [f["flag_type"] for f in res.injected_flags if f.get("kind") == "ml_only"]
    lco = {
        "company_id": [spec.company_id], "profile": [spec.profile], "bank": [spec.bank],
        "seed": [str(spec.seed)], "n_months": [spec.n_months], "start": [spec.start],
        "revenue_scale": [spec.revenue_scale], "n_customers": [spec.n_customers],
        "rule_flags": [rule_flags], "ml_flags": [ml_flags], "pdf_rendered": [render],
    }
    lf: dict[str, list[Any]] = {f.name: [] for f in LABEL_SCHEMAS["flags"]}
    for f in res.injected_flags:
        lf["company_id"].append(spec.company_id)
        lf["flag_type"].append(f["flag_type"])
        lf["kind"].append(f.get("kind"))
        lf["flag_description"].append(f.get("description", ""))
        lf["triggering_txn_ids"].append(list(f.get("triggering_transaction_ids", [])))

    return {
        "features/transactions": pa.table(ft, schema=FEATURE_SCHEMAS["transactions"]),
        "features/companies": pa.table(fc, schema=FEATURE_SCHEMAS["companies"]),
        "labels/transactions": pa.table(lt, schema=LABEL_SCHEMAS["transactions"]),
        "labels/customers": pa.table(lc, schema=LABEL_SCHEMAS["customers"]),
        "labels/companies": pa.table(lco, schema=LABEL_SCHEMAS["companies"]),
        "labels/flags": pa.table(lf, schema=LABEL_SCHEMAS["flags"]),
    }


def _chunk_tables(job: tuple[Sequence[int], BuildArgs, str]) -> dict[str, pa.Table]:
    indices, args, stage = job
    parts: dict[str, list[pa.Table]] = {}
    for i in indices:
        for name, tbl in _company_tables(i, args, stage).items():
            parts.setdefault(name, []).append(tbl)
    return {name: pa.concat_tables(tbls) for name, tbls in parts.items()}


def _chunks(n: int, size: int) -> list[list[int]]:
    return [list(range(i, min(i + size, n))) for i in range(0, n, size)]


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def combined_hash(file_hashes: dict[str, str]) -> str:
    body = "".join(f"{p}:{h}\n" for p, h in sorted(file_hashes.items()))
    return hashlib.sha256(body.encode()).hexdigest()


def _write_parquet(table: pa.Table, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, compression="zstd", row_group_size=ROW_GROUP_SIZE,
                   write_statistics=True)


def _replace_dir(stage: Path, target: Path) -> None:
    """Move the finished stage into place. Only ever deletes a previous corpus
    (a directory with our manifest.json) — never an unrelated directory."""
    if target.exists():
        if not (target / "manifest.json").exists() and any(target.iterdir()):
            raise SystemExit(f"refusing to overwrite non-corpus directory: {target}")
        shutil.rmtree(target)
    stage.rename(target)


def build(
    *,
    n_companies: int,
    seed: int,
    out_dir: Path,
    max_months: int = 24,
    pdf_subset: int = 30,
    workers: int = 1,
    enable_ml_only: bool = True,
    chunk_size: int = 25,
    quiet: bool = False,
) -> dict[str, Any]:
    """Build the corpus into ``out_dir`` and return the manifest."""
    from tools.synthetic_gen import GENERATOR_VERSION

    if n_companies < 1:
        raise ValueError("n_companies must be >= 1")
    args = BuildArgs(n_companies, seed, max_months, pdf_subset, enable_ml_only)
    out_dir = out_dir.resolve()
    stage = out_dir.parent / f".{out_dir.name}.staging"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    t0 = time.perf_counter()
    jobs = [(c, args, str(stage)) for c in _chunks(n_companies, chunk_size)]
    collected: list[dict[str, pa.Table]]
    if workers <= 1:
        collected = [_chunk_tables(j) for j in jobs]
    else:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            collected = list(ex.map(_chunk_tables, jobs))   # map() preserves job order
    gen_secs = time.perf_counter() - t0

    names = list(collected[0])
    tables = {n: pa.concat_tables([c[n] for c in collected]) for n in names}
    for name, tbl in tables.items():
        _write_parquet(tbl, stage / f"{name}.parquet")

    file_hashes = {
        p.relative_to(stage).as_posix(): sha256_file(p)
        for p in sorted(stage.rglob("*")) if p.is_file()
    }
    manifest: dict[str, Any] = {
        "corpus_version": CORPUS_VERSION,
        "generator_version": GENERATOR_VERSION,
        "synthetic": True,
        "args": {"n_companies": n_companies, "seed": seed, "max_months": max_months,
                 "pdf_subset": pdf_subset, "enable_ml_only": enable_ml_only},
        "pyarrow_version": pa.__version__,
        "row_counts": {n: t.num_rows for n, t in sorted(tables.items())},
        "files": file_hashes,
        "corpus_hash": combined_hash(file_hashes),
    }
    # write_bytes, not write_text: LF on every OS so the manifest is byte-identical.
    (stage / "manifest.json").write_bytes(
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"))
    _replace_dir(stage, out_dir)
    if not quiet:
        total = time.perf_counter() - t0
        print(f"corpus {CORPUS_VERSION} (generator v{GENERATOR_VERSION}, SYNTHETIC) -> {out_dir}")
        for n, c in manifest["row_counts"].items():
            print(f"  {n:24s} {c:>10,d} rows")
        pdfs = sum(1 for p in file_hashes if p.startswith("pdf/"))
        print(f"  pdf subset               {pdfs:>10d} files")
        print(f"  generate {gen_secs:.1f}s, total {total:.1f}s, workers={workers}")
        print(f"corpus_hash {manifest['corpus_hash']}")
    return manifest


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Build the synthetic Parquet corpus.")
    parser.add_argument("--n-companies", type=int, default=None)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--small", action="store_true",
                        help="CI mode: 50 companies, <=12 months, 5 PDFs, out=data/corpus/v1-small")
    parser.add_argument("--max-months", type=int, default=None)
    parser.add_argument("--pdf-subset", type=int, default=None)
    parser.add_argument("--workers", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--no-ml-only", action="store_true", help="skip ml_only injectors")
    parser.add_argument("--out", type=Path, default=None)
    a = parser.parse_args(argv)

    if a.small:
        n, months, pdfs = 50, 12, 5
        out = a.out or DEFAULT_OUT / f"{CORPUS_VERSION}-small"
    else:
        n, months, pdfs = 1000, 24, 30
        out = a.out or DEFAULT_OUT / CORPUS_VERSION
    build(
        n_companies=a.n_companies if a.n_companies is not None else n,
        seed=a.seed,
        out_dir=out,
        max_months=a.max_months if a.max_months is not None else months,
        pdf_subset=a.pdf_subset if a.pdf_subset is not None else pdfs,
        workers=a.workers,
        enable_ml_only=not a.no_ml_only,
    )


if __name__ == "__main__":
    main(sys.argv[1:])

