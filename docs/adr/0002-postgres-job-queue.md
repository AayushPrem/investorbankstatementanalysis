# ADR-0002: A Postgres-backed job queue (`SELECT … FOR UPDATE SKIP LOCKED`)

- **Status:** Accepted
- **Date:** 2026-11-02
- **Phase:** 2 (`upgrade/p2-data-platform`)
- **Depends on:** ADR-0001 (PostgreSQL as the system of record)

## Context

`POST /v1/statements` has to return quickly (202 plus a job id). The pipeline runs for seconds per statement (PDF parsing, 12 stages, optional LLM calls), so it has to run somewhere else, with:

- durable status (`queued → running → succeeded | failed`) that the API can report,
- retries (at most 3 attempts with backoff), keeping the last error,
- recovery when a worker dies mid-job: another worker must pick the job up again,
- a per-stage execution log (`stage_executions`).

The expected throughput is a handful of statements per minute (a demo and small-team tool), not thousands per second.

## Decision

The queue is a `jobs` table in the same PostgreSQL database. A worker (`python -m worker`) claims one job at a time:

```sql
SELECT … FROM jobs
WHERE (status = 'queued' AND available_at <= now())
   OR (status = 'running' AND locked_at < now() - :lease)   -- worker died
ORDER BY created_at
LIMIT 1
FOR UPDATE SKIP LOCKED;
```

Then, in the same transaction, it sets `status='running'`, `locked_at`, `locked_by` and `attempts += 1`, and commits.

- `SKIP LOCKED` means concurrent workers never block on, or double-claim, the same row.
- **Lease expiry** recovers crashed workers. A worker heartbeats `locked_at` between stages, so a job is only reclaimed once nothing has touched it for the whole lease.
- **Retries:** a failure with `attempts < max_attempts` re-queues the job with `available_at = now + backoff·2^(attempts-1)`. Otherwise it ends `failed` with `last_error`. A validation failure (deterministic bad data) fails at once; retrying can't fix it.
- **Idempotent results:** the result write for a job happens in one transaction keyed on the document. A reclaimed job doesn't duplicate rows.
- The worker code goes through a small `JobQueue` interface, so the backend could be replaced without touching the pipeline.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **Celery + Redis/RabbitMQ** | Adds a broker service, which brings another failure mode and another thing to deploy, secure and monitor. Job state would live in two places (broker + DB), and they can disagree. Nothing at this throughput needs it. |
| **RQ / Dramatiq / arq** | Same objection (they need Redis), on a smaller scale. |
| **FastAPI `BackgroundTasks` / in-process threads** | Not durable: a restart loses queued work, and you can't have more than one worker process. |
| **Postgres `LISTEN/NOTIFY` only** | Notifications aren't persisted. It could later be added *on top of* the table to cut polling latency. |
| **pgmq / pg-boss-style extensions** | That would be a reasonable choice, but it adds an extension dependency for features we don't need yet. |
| **Kafka / Airflow** | Explicitly out of scope (§15). Those are streaming and batch-orchestration systems, not a task queue for a few jobs a minute. |

## Consequences

- **Positive:**
  - one fewer system to run;
  - job state and results commit in the same database, so the status can't disagree with the stored results;
  - easy to explain and to test (claim, lease reclaim and retry are just SQL).
- **Negative:**
  - workers poll (default every 1 s);
  - throughput is limited by Postgres row locking, which is fine for hundreds of jobs a minute and not designed for far more;
  - SQLite (used in unit tests) ignores `FOR UPDATE`, so the no-double-claim guarantee is only exercised in the `postgres`-marked tests that CI runs.
- **Exit path:** if throughput ever needs it, swap the `JobQueue` implementation for a broker-backed one. The API and pipeline wouldn't change.
