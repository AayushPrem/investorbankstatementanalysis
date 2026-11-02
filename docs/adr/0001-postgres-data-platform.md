# ADR-0001: PostgreSQL + SQLAlchemy 2.0 + Alembic as the system of record

- **Status:** Accepted
- **Date:** 2026-11-02
- **Phase:** 2 (`upgrade/p2-data-platform`)
- **Supersedes:** nothing. This ADR **reverses part of the Phase 1 dependency audit (D9)**, see "Re-adding dependencies".

## Context

Every run's state lives on the local filesystem today (defect D11 in `UPGRADE_GUIDE.md` §2.4):

- `pipeline/versioning.py` writes JSON results to `.pipeline_state/`, keyed by a 16-character prefix of the input's SHA-256.
- `pipeline/customer_identity.py` keeps an embedding cache in `.cache/`.
- Nothing else is persisted. A Streamlit session holds the results in memory, and they are gone when it ends.

That leaves the following unsolved, and Phase 2 needs every one of them:

1. **Idempotency.** Uploading the same statement twice should not re-run the pipeline or produce two copies. The filesystem index can't enforce uniqueness when two processes write at once.
2. **Atomic writes.** A statement has a header, up to thousands of transactions, categorisations, flags, a DQ report and analysis outputs. Either all of it is stored or none of it is. Writing several JSON files gives no such guarantee.
3. **Integrity constraints on money.** Every transaction must have exactly one of debit/credit, every statement period must have `start ≤ end`, and `(statement, txn_seq)` must be unique. These belong in the store, not only in Pydantic, because the API's manual category override writes to it directly.
4. **Job tracking.** An API upload has to return immediately and be processed by a worker. That needs a durable queue with status, attempts and the last error (ADR-0002).
5. **Queryable history.** The API pages and filters transactions by category, date and source. Later, the investigator agent (Phase 7) reads flags and transactions through tools. JSON blobs on disk can't be queried.

## Decision

Use **PostgreSQL 16** as the system of record:

- **SQLAlchemy 2.0** (typed `Mapped[...]` ORM) for models and repositories in `db/`.
- **Alembic** for schema migrations in `db/migrations/`, with `alembic.ini` at the repo root.
- **psycopg2** (`psycopg2-binary`) as the driver.

Design rules that follow from it:

- **Money is `NUMERIC(18,2)`, never float.** `CanonicalTransaction` already holds `Decimal`, and it stays `Decimal` from the adapter to the database. Floats appear only at the presentation and ML-feature boundary.
- **Content-hash idempotency.** `documents.sha256` is `UNIQUE`, and the API checks it before doing any work. When two uploads race, the database constraint decides, not application code.
- **One DB transaction per statement.** If the write fails, no rows from that statement remain.
- **JSONB for semi-structured evidence** (DQ checks, flag evidence, agent/model versions, analysis payloads). The fixed columns stay relational and constrained.
- **Portable model definitions.** Types are `Uuid`, `Numeric`, and `JSON` with a `JSONB` variant on PostgreSQL. Unit and API tests can therefore run on SQLite in-memory. PostgreSQL-specific behaviour (`SKIP LOCKED`, the migration on a real server) is tested under a `postgres` pytest marker, against the `postgres:16` service in CI.

## Re-adding dependencies (reversal of Phase 1 D9)

Phase 1 removed `sqlalchemy` and `psycopg2-binary` from `pyproject.toml` because **no module imported them**. They were declared, never used. That was the correct call then, and the reason was absence of use, not a problem with the libraries.

The new `db/` package imports both, so that reason no longer holds. They are re-added together with `alembic`, but as an **optional extra** (`pip install -e ".[platform]"`) alongside the API dependencies (`fastapi`, `uvicorn`, `python-multipart`, `httpx`). The core analysis pipeline (`pipeline.run.run_analysis`) and the Streamlit demo don't import `db/`, so their install stays as lean as Phase 1 left it. CI and `make install` install the extra.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Keep the filesystem (`.pipeline_state/`)** | No uniqueness under concurrency, no atomic multi-file writes, no queries. It is the problem described above. |
| **SQLite as the production store** | One writer at a time, and no `SELECT … FOR UPDATE SKIP LOCKED`, so the job queue (ADR-0002) would need another system. SQLite is still used for **tests only**. |
| **MongoDB / a document store** | Money needs exact `NUMERIC` and cross-row constraints (exactly-one-of debit/credit, unique `(statement_id, txn_seq)`, foreign keys). Postgres JSONB already covers the document-shaped parts (evidence and payloads) inside the same transaction. |
| **Iceberg / a lakehouse (Parquet + catalog)** | That is an analytical store for large, append-heavy scans. This system needs an OLTP system of record: row-level updates (manual category overrides, job state), unique constraints and transactional upload semantics. Phase 3's corpus *does* use Parquet + DuckDB for analytics, which is a different job. |
| **Raw SQL / no ORM** | That would be workable, but migrations would have to be hand-rolled. Alembic plus typed models give reviewable, versioned schema changes and mypy-checked repositories. |
| **SQLModel / Tortoise / Django ORM** | Each adds a layer or a framework. SQLAlchemy 2.0 is the de facto standard, and Alembic is built for it. |

## Consequences

- **Positive:** idempotent ingestion, atomic writes, enforced constraints, queryable history, and a job queue without another service. `pgvector` can be added later inside the same database if retrieval is ever needed (no standalone vector DB, per §15).
- **Negative:**
  - Running the platform now needs a Postgres server (`docker-compose.yml` provides one).
  - Two more dependencies return.
  - Keeping models portable to SQLite for tests means avoiding some PG-only column types in the base definition (they are used through `with_variant`).
- **Unchanged:** the analysis stages and `run_analysis` don't touch the database. The worker calls `run_analysis` and persists its result, so the gold-set regression is unaffected by this ADR.
- **Not decided here:** the job queue mechanism (ADR-0002) and whether Streamlit calls the API instead of `run_analysis` (deferred; the UI stays local in Phase 2).
