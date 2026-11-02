# ADR-0003: Parquet files + DuckDB for the synthetic training corpus

- **Status:** Accepted
- **Date:** 2026-11-02
- **Phase:** 3 (`upgrade/p3-corpus`)

## Context

Phases 4–6 train and evaluate models on a synthetic corpus: 1,000 companies, about 0.6 M transactions, with labels kept separate from features. That data has to be:

- reproducible bit for bit from `(seed, args)`, so a model's metrics can be tied to an exact corpus (`corpus_hash`);
- columnar and typed (dates, `decimal(18,2)` money, list columns), and fast to scan and aggregate for feature building and split construction;
- split physically into feature tables and label tables, so a feature query can't reach a label column;
- buildable on a laptop and in CI with no service running.

## Decision

- The corpus is a directory of **Parquet** files (`features/`, `labels/`, `splits/`) written with pyarrow (zstd, fixed row-group size, no wall-clock metadata) plus a `manifest.json` with per-file sha256 hashes and a combined `corpus_hash`.
- **DuckDB** (embedded, in-process) is used for corpus statistics and joins (`datasets/duckdb_stats.py`). Its `connect()` exposes only the feature tables unless labels are asked for explicitly, and then only under a `label_` prefix.
- New dependencies go in a `[ml]` extra: `pyarrow`, `duckdb`, `scikit-learn`. The pipeline, demo and API don't need them.

## Alternatives rejected

- **Postgres tables (ADR-0001's database):** that database is the system of record for real uploads. Mixing a regenerable synthetic training set into it couples model work to a running server and to migrations. Parquet files are also simpler to hash and to version per corpus.
- **CSV / JSON lines:** untyped (money as strings or floats, dates as text), slow to parse at this size, and no column pruning.
- **Spark / a distributed engine:** out of scope (§15). The corpus is tens of MB. On this machine DuckDB ran the benchmark aggregation (a feature–label join plus a monthly group-by over 617k rows) in 0.52 s, against 1.05 s for pandas (`python -m datasets.duckdb_stats data/corpus/v1 --bench`, best of 3).

## Consequences

- Hashes are reproducible for a given pyarrow version. A pyarrow upgrade can change the bytes, so `manifest.json` records `pyarrow_version`.
- Text files in the corpus (truth JSON, manifests) are written with LF on every OS, so line endings don't change the hash.
- `data/corpus/` is gitignored, and the corpus is rebuilt by `make corpus` (`make corpus-small` in CI).
