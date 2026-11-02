# Upgrade changelog

One entry per phase of `UPGRADE_GUIDE.md`. Each entry has three parts: what changed, which files changed, and what is verified vs not.

---

## Phase 0: Baseline & fixes (`upgrade/p0-baseline`, 2026-11-02)

The goal was a reproducible, green starting point with no pipeline behaviour change. The real command output is in `docs/baseline_2026-11.md`.

### What changed

- **D1 (build):** the build backend `setuptools.backends.legacy:build`, which doesn't exist, became `setuptools.build_meta`. `scripts*` was added to package discovery.
- **D2 (gold set):** new `scripts/build_gold_set.py` deterministically regenerates `data/gold/*.pdf` and `data/gold/labels/*.truth.json` from an explicit 22-entry `GOLD_MANIFEST`.
  - Why determinism needed work: the generator's `seed` doesn't control `uuid.uuid4()` (statement-ID suffixes, narration hex refs, txn IDs) or the import-time `account_no`, and reportlab stamps timestamps into PDFs.
  - The build script pins all three during each call. `tools/synthetic_gen.py` itself is not modified.
  - New `tests/conftest.py` builds the gold set in `pytest_configure` when files are missing. It runs there rather than in a fixture because `test_demo.py` globs `data/gold/*.pdf` at collection time.
  - PDFs remain gitignored and are not committed.
- **D3 (UI tests):** `tests/test_demo_ui.py` now passes an absolute `demo/app.py` path to `AppTest.from_file`.
- **D4 (Makefile):** rewritten to be cross-platform.
  - It picks `.venv/Scripts` vs `.venv/bin` by `$(OS)` and calls every tool as `python -m …`.
  - New targets: `gold` and `typecheck-all` (informational).
  - `demo` now points at `demo/app.py`.
- **D5 (CI):**
  - Triggers also on `sprint-*` and `upgrade/*`.
  - mypy is gated (no `|| true`) on `schema/ pipeline/` with `--follow-imports=silent`; a full-scope mypy step runs as `continue-on-error`.
  - CI builds the gold set, runs the regression suite and uploads `gold_regression.json`.
- **D12 (regression targets):** `test_risk_recall` and `test_customer_id_accuracy` are marked `xfail(strict=False, raises=AssertionError)`.
  - Targets are unchanged.
  - Thanks to `raises=AssertionError`, the "metric could not be computed" coverage failure (`pytest.fail`) still fails hard.
- **Undeclared dependency:** `plotly>=5.20` was added to `dependencies`. `demo/app.py` imports it, and a fresh venv failed 6 UI tests without it.
- **Lint (needed for CI to pass at all):** `ruff check .` had 112 pre-existing errors.
  - **78** fixed by ruff's *safe* autofixes: unused imports, import sorting, quoted annotations, `Optional[X]` → `X | None`, f-strings without placeholders.
  - E402 ignored for `demo/*.py`, where the `sys.path` insert before imports is intentional.
  - **Manual:** renamed ambiguous `l` (2 places); removed 3 dead local assignments in `reports/` and 2 in `tests/test_reconciliation.py`.
  - `noqa` on `CashLoanProhibition269SS_269T` (N801: the name mirrors the statute and is referenced 9×) and on `Decimal as D` in a test (N817).
  - **Check:** every file whose annotations ruff unquoted has `from __future__ import annotations`. The gold set rebuilt after the lint fixes is byte-identical to the one before, so generator behaviour is unchanged.
- **Type fixes in the gated scope, annotation-only:**
  - `pipeline/related_party.py`: `cast` to `TextBlock`, `dict[str, Any]`, and a stale `type: ignore` removed.
  - `pipeline/customer_identity.py`: `cast` on the embedding model and the cache loader, and stale `type: ignore`s removed.
  - These errors appeared because `anthropic` and `sentence-transformers` now ship type information.
- **Baseline:** `docs/baseline_2026-11.md`.

### Files

- **Added:** `scripts/__init__.py`, `scripts/build_gold_set.py`, `tests/conftest.py`, `docs/baseline_2026-11.md`, `docs/CHANGELOG_UPGRADE.md`, `UPGRADE_GUIDE.md`
- **Modified (Phase 0 tasks):** `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `tests/test_demo_ui.py`, `tests/regression/test_gold_set.py`, `pipeline/related_party.py`, `pipeline/customer_identity.py`
- **Modified (lint only):** about 25 files across `analysis/`, `reports/`, `jurisdictions/`, `pipeline/`, `tools/`, `demo/` and `tests/`. See `git show --stat`.
- **Removed:** none

### Verified (run in this session, Windows 11, Python 3.12.2)

- A fresh `.venv` plus `pip install -e ".[dev]"` succeeds.
- `python -m scripts.build_gold_set` builds 22 statements. Three independent builds are byte-identical, one of them under a different `PYTHONHASHSEED`.
- `pytest -q --ignore=tests/regression`: **909 passed**, 0 failed, 0 errors.
- `pytest tests/regression`: **7 passed, 1 xfailed** (customer-ID 66.6%), **1 xpassed** (risk recall 88.5%).
- `ruff check .`: clean. `mypy --follow-imports=silent schema/ pipeline/`: clean.
- Makefile on Windows via `mingw32-make`:
  - `gold`, `lint` and `typecheck` were actually run.
  - `install`, `test`, `regression` and `demo` were checked with `-n` (dry run).

### Not verified / known gaps

- **Linux/macOS and CI have not run yet.** The Makefile's `bin/` branch and the GitHub workflow are "implemented, not run" until the branch is pushed.
- **Regression numbers aren't comparable to README/FEATURES.md (73.3% / 82.3%),** because the gold-set composition changed. README/FEATURES.md are not updated here; that's Phase 8.
- **The gold set's account numbers are pinned per profile,** so a profile's statements share one account (the same semantics as one process of the original generator). Each statement's other content (seed, uuid stream) differs.
- **Latent determinism risk:** `services_firm` salary logic calls `date.today()` (`tools/synthetic_gen.py`, `_gen_services_firm`). It's inert for the fixed 2024 start date; a fix belongs with the Phase 3 generator work.
- **mypy remaining scope:** 219 errors across 19 files in `analysis/`, `reports/`, `jurisdictions/`, `adapters/` and `tools/`. Widen the gated scope as these are fixed.
- **D12 remains open:** customer-ID resolution on `restaurant` narrations (0–25% per statement).
- `PIL` is imported but only installed transitively (via streamlit/reportlab). That's left for the Phase 1 dependency audit (D9).
- Windows only: `pytest -s` with stdout redirected to a file needs `PYTHONUTF8=1`, because the regression summary prints box-drawing characters.

---

## Phase 1: Foundations (`upgrade/p1-foundations`, 2026-11-02)

This phase removed duplication and hardcoding and added one canonical pipeline entry point. It is a pure refactor. `AGENT_VERSIONS` was not bumped, and the regression output is identical to `docs/baseline_2026-11.md` (see "Verified").

Tasks 1–5 and 7 landed in commit 20c2375 (CI passed). Task 6, the `demo/app.py` split, was done in a follow-up session on the same branch; see the "Task 6" bullets below.

### What changed

- **Config (`config/settings.py`):** pydantic-settings `Settings`.
  - Fields: `ANTHROPIC_API_KEY` (`SecretStr`), `LLM_MODEL` (default `claude-haiku-4-5-20251001`, unchanged), `LLM_ENABLED`, `LLM_TIMEOUT_S`, `LLM_MAX_RETRIES`, `DATABASE_URL`, `LOG_LEVEL`, `LOG_JSON`, `MODELS_DIR`, `EMBEDDINGS_ENABLED`.
  - `get_settings()` is **not cached**, on purpose. Tests toggle `ANTHROPIC_API_KEY` right before constructing analysts.
  - A `.env` is read from `BSAA_ENV_FILE` (default `.env`). `tests/conftest.py` sets `BSAA_ENV_FILE=""` so a local `.env` can't flip tests into LLM mode.
  - `.env.example` has placeholders only.
- **Thresholds (D7) → `config/thresholds.yaml` + `config/thresholds.py`.** These are frozen, typed dataclasses.
  - Values that were `Decimal` in code are quoted strings in YAML, and the loader rejects an unquoted float for a `Decimal` field. Missing or unknown keys are errors.
  - Module-level names (`_STRUCT_LOWER`, `RUNWAY_LOW_MONTHS`, …) are kept as aliases, so importers and tests are unchanged.
  - Moved from `analysis/risk.py`: 15 constants plus `_SEVERITY_WEIGHTS`.
  - Moved from `analysis/financial_health_alerts.py`: 10 named constants plus 4 inline literals (sustained decline 3, stagnation band `0.02`, burn acceleration 0.20 / 0.15).
  - Moved from `analysis/customer_analytics.py`: 4.
  - Moved from `analysis/reconciliation.py`: 8 inline literals (severity bands 30/15, tolerances 8/12/18, NRR cutoff 5, NRR tolerance 0.08 and HIGH 0.20).
  - Moved from `analysis/portfolio_narrative.py`: 1.
  - Moved from `jurisdictions/india/rules.py`: 7.
  - Moved from `pipeline/customer_identity.py`: 1. From `pipeline/related_party.py`: 2. Categoriser batch size: 1.
  - `tests/test_thresholds.py` pins every value **and its type** against the pre-Phase-1 literal.
  - Known limitation: the sustained-decline alert still averages the last 3 months (`rates[-3:]`, `/ 3`) whatever `sustained_decline_months` is set to.
- **LLM client (D6) → `llm/client.py`.**
  - An `LLMClient` protocol with `complete(prompt, *, system, max_tokens, temperature=None)` and `complete_json(..., schema)`. `complete_json` validates with Pydantic and retries once on invalid JSON.
  - `AnthropicClient`:
    - imports the SDK lazily
    - takes model, timeout and retries from settings; backoff on 408/409/429/5xx and connection errors is the SDK's own `max_retries`, default 2, the same as before
    - logs model, token counts, `duration_ms` and `stop_reason`, never prompt or response text
  - `NullClient` raises `LLMUnavailable`. `FakeLLM` returns scripted responses for tests.
  - **All 8 call sites were replaced:** categoriser, compliance, financial_health_alerts, portfolio_narrative, reconciliation, report_narrative, risk, related_party.
    - Prompts, system text, `max_tokens` and local response parsing are byte-identical.
    - The request body is the same shape: model / max_tokens / system / one user message. `temperature` is only sent if a caller passes it, and none do.
    - Each existing fallback path is kept. `LLMUnavailable` lands in the same fallback that a missing key or a missing `anthropic` package did before.
    - `bool(os.getenv("ANTHROPIC_API_KEY"))` became `llm_key_configured()`, which also honours `LLM_ENABLED=false` and `.env`.
  - **Timeout changed on the LLM path only:** it was the SDK default (10 min) and is now `LLM_TIMEOUT_S` = 60 s. This path is not exercised by any test or by the regression suite, which run without a key.
  - `Categoriser.LLM_MODEL` was removed. `Categoriser(llm_model=None)` now resolves to `settings.llm_model`.
  - `complete_json` is implemented and tested but **not wired** into the call sites. Wiring it would change parsing paths that tests pin, so it's left for P4/P7.
- **Logging → `config/logging.py`.**
  - JSON formatter fields: `ts`, `level`, `logger`, `msg`, `run_id`, `stage`, plus `extra` counts.
  - `run_id` and `stage` come from contextvars. `stage_timer(name)` logs `stage done` with `duration_ms`, or `stage failed` with the exception type.
  - Redaction helpers: `redact()` gives `h:<sha256[:10]>`, and `safe_exc()` gives the exception type name only.
  - `configure_logging()` is called only by `demo/app.py`. It is idempotent.
  - **It pins `pdfminer`, `pdfplumber`, `PIL`, `httpx`, `httpcore`, `anthropic` and `urllib3` to WARNING.** A new test found that `pdfminer` logs the raw PDF token stream at DEBUG, including transaction descriptions and names, so `LOG_LEVEL=DEBUG` would otherwise leak statement content.
  - **PII log fixes:**
    - `pipeline/orchestrator.py` logged the filename plus total revenue and burn. It now logs a redacted filename and counts.
    - `pipeline/batch.py` logged the input path plus the exception message. It now logs a redacted path plus the exception type.
    - `reports/angel_lens.py` logged the output path. It now logs a byte count.
    - 10 LLM-failure and rule-failure `log.warning(..., exc)` lines now log `safe_exc(exc)`, because exception text can echo row content or LLM output.
- **Canonical pipeline (D8) → `pipeline/run.py`.**
  - `run_analysis(data, filename, *, options=AnalysisOptions()) -> AnalysisResult` has the same stages, order and per-stage flags as the pre-Phase-1 `run_pipeline_on_bytes`, minus report rendering. Every stage is wrapped in `stage_timer`.
  - The temp input file keeps the `{sha1[:8]}_{filename}` name, so transaction IDs (which hash the file stem) are unchanged.
  - **Adapter auto-selection:**
    - `.csv` → CSV
    - `.xlsx/.xlsm/.xls` → Excel
    - `.png/.jpg/.jpeg/.tif/.tiff` → Image
    - PDF and anything else → a pdfplumber text probe: some text means digital, no text on any page means scanned. If the probe errors it picks digital, so error behaviour for bad or empty bytes and unsupported banks is unchanged.
    - OCR adapters are imported lazily.
  - **One intended behaviour change:** a text-less (scanned) PDF used to go to the digital adapter and fail with the unsupported-bank error. It now goes to `ScannedPDFAdapter`. No gold statement is scanned.
  - `demo/pipeline_runner.run_pipeline_on_bytes` is now `run_analysis` + lens rendering, with the `DemoResult` shape unchanged.
  - `pipeline/batch.py` defaults to that same runner, because the Network UI needs the report bytes. A new `BatchOrchestrator(render_reports=False)` calls `run_analysis` directly.
  - `pipeline/orchestrator.py` is marked deprecated (docstring + `DeprecationWarning`) and kept, with its tests, until P7. The pytest `filterwarnings` ignores exactly that message.
- **Dependency audit (D9).** Evidence: an import grep (top-level and lazy) over `adapters analysis pipeline reports jurisdictions demo schema tools scripts profiles tests workbench_config.py`.

  | Package | Imported? | Decision |
  |---|---|---|
  | `pymupdf` | no `fitz`/`pymupdf` import | **removed** |
  | `tabula-py` | no | **removed** |
  | `weasyprint` | no | **removed** |
  | `jinja2` | no (still installed transitively by streamlit) | **removed** as a direct dep |
  | `sqlalchemy` | no DB code | **removed**; re-add in P2 with an ADR |
  | `psycopg2-binary` | no DB code | **removed**; re-add in P2 with an ADR |
  | `networkx` | no (union-find is hand-written) | **removed** |
  | `sentence-transformers` | lazy, guarded in `pipeline/customer_identity.py` | moved to extra **`[embeddings]`** |
  | `pytesseract` | module-level in `adapters/scanned_pdf.py`, `adapters/image.py` | moved to extra **`[ocr]`**; `run_analysis` imports those adapters lazily |
  | `pillow` | `adapters/image.py`, `adapters/scanned_pdf.py`, tools, tests | **declared** (was transitive only) |
  | `pydantic-settings`, `pyyaml` | new (`config/`) | **added**; `types-PyYAML` added to `dev` |
  | pdfplumber, pydantic, pandas, numpy, anthropic, streamlit, openpyxl, reportlab, plotly | yes | kept |

  - `make install` and CI install `.[dev,ocr,embeddings]` so the whole suite still runs.
  - The CI venv cache lost its `restore-keys`. A `pyproject.toml` change now builds a fresh venv, so a removed package can't linger in a cached venv and hide a missing dependency.
- **Type-check scope:** `config/` and `llm/` were added to the gated mypy scope, in both the Makefile `TYPECHECK_GATED` and `ci.yml`.
- **Task 6: UI split.** `demo/app.py` went from 1,668 lines to 64 and now holds only the sys.path bootstrap, `set_page_config`, `configure_logging()`, the theme CSS call and the mode switch. Everything else moved to two packages:
  - `demo/views/` holds the screens: `landing`, `single_company` (source → two-pass cached pipeline run → lens), `lenses` (which tabs Angel / VC / Workbench show), and `network`.
  - `demo/components/` holds the building blocks: `sidebar`, `summary`, `tabs_financial`, `tabs_risk`, `tabs_customers`, `downloads`, `network_tables`, `theme`, `formatting`, `labels`.
  - Imports go one way only: `app → views → components`. The components make no Streamlit calls at import time.
  - **The guide says `demo/pages/`; we used `views/` on purpose.** Streamlit auto-discovers a `pages/` folder next to the entry script and adds sidebar navigation, which would be a UI change.
  - **The code moved, it wasn't rewritten.** A script sliced the original by line ranges, so function bodies are verbatim. The only edits are private names becoming public (`_badge` → `badge`, `_sidebar` → `render_statement_source`, …), each module's imports, and one block lifted out of `main()` into `render_status_badges()` with no change. Widget order, widget keys, labels and `session_state` keys are unchanged.

### Files

- **Added:** `config/__init__.py`, `config/settings.py`, `config/thresholds.py`, `config/thresholds.yaml`, `config/logging.py`, `llm/__init__.py`, `llm/client.py`, `pipeline/run.py`, `.env.example`, `tests/test_thresholds.py`, `tests/test_llm_client.py`, `tests/test_logging_config.py`, `tests/test_run.py`
- **Modified:** `analysis/{categoriser,compliance,customer_analytics,financial_health_alerts,portfolio_narrative,reconciliation,report_narrative,risk}.py`, `jurisdictions/india/rules.py`, `pipeline/{batch,customer_identity,orchestrator,related_party}.py`, `reports/angel_lens.py`, `demo/pipeline_runner.py`, `demo/app.py` (only the `configure_logging()` call), `tests/conftest.py`, `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `docs/CHANGELOG_UPGRADE.md`
- **Removed:** none
- **Task 6, added:** `demo/views/{__init__,landing,single_company,lenses,network}.py`, `demo/components/{__init__,theme,formatting,labels,sidebar,summary,tabs_financial,tabs_risk,tabs_customers,downloads,network_tables}.py`
- **Task 6, modified:** `demo/app.py` (now the entry point only), `README.md` (demo tree), `docs/CHANGELOG_UPGRADE.md`

### Verified (run in this session, Windows 11, Python 3.12.2)

**Regression, before vs after.** "Before" is `docs/baseline_2026-11.md`, and it was also re-run on this branch before any edit with the same output. "After" is the final code.

| Metric (synthetic gold set, 22 statements) | Before | After |
|---|---|---|
| Extraction | 100.0% | 100.0% |
| Categorisation | 99.9% | 99.9% |
| Risk recall | 88.5% (n=13) | 88.5% (n=13) |
| Compliance recall | 90.0% (n=10) | 90.0% (n=10) |
| Customer ID acc. | 66.6% (pairs=176521) | 66.6% (pairs=176521) |
| Regression result | 7 passed, 1 xfailed, 1 xpassed | 7 passed, 1 xfailed, 1 xpassed |

- `results/sprint1/gold_regression.json` from the pre-edit run and the final run is **identical in every key except `timestamp`**, including `per_statement`.
- `pytest -q --ignore=tests/regression`: **1016 passed**, 0 failed (909 pre-existing + 107 new), before the last hardening change. The 2 warnings are the same two pre-existing deprecation notices.
- **Hardening found in review:** `run_analysis` built its temp path from the caller's `filename`, so `../x.pdf` could escape the temp dir. That was inherited from the old runner. Only the basename is used now, and a new test asserts identical transaction IDs, which brings the suite to 1017 tests. See the next bullet for the final runs.
- **Final code, three full-suite runs:** (1) `1 failed, 1016 passed`, failing test not captured; (2) `1 failed, 1016 passed`, with the failure being the flaky `test_vc_lens.py::test_generate_is_idempotent` described under known gaps; (3) **`1017 passed`**, 0 failed. Regression on the final code: `7 passed, 1 xfailed, 1 xpassed`, with the same metrics as the table above and `gold_regression.json` identical except `timestamp`.
- Gold set rebuilt with `python -m scripts.build_gold_set --out <tmp> --force` on the final code: **byte-identical** to the pre-change build (44 files: 22 PDFs + 22 truth JSONs).
- `ruff check .`: clean. `mypy --follow-imports=silent schema/ pipeline/ config/ llm/`: `Success: no issues found in 17 source files`.
- Acceptance greps:
  - `grep -rn "anthropic.Anthropic(" --include=*.py . | grep -v llm/` → no output
  - `grep -rn "claude-haiku" --include=*.py .` → only `config/settings.py`
- Fresh venv + `pip install -e ".[dev,ocr]"` (no `[embeddings]`): install succeeds and `pip check` reports no broken requirements. None of the removed packages are installed (Jinja2 only transitively).
- That fresh venv (no `sentence-transformers`, no torch) then ran `pytest -q --ignore=tests/regression`: **1016 passed**. The venv had to live at a short path (`.vf/`, since deleted) because the scratchpad path exceeded Windows' 260-character limit during `pip install`.

**CI for 20c2375 (tasks 1–5, 7): passed** on GitHub Actions (Linux), as reported by the user.

**Task 6 (UI split), run in the follow-up session:**

- **The element trees are identical before and after.** A scratchpad script, not committed, ran Streamlit's `AppTest` against the old `app.py` (`git show 20c2375:demo/app.py`, copied in temporarily) and against the new one, in 9 scenarios.
  - It serialised every element's protobuf (type, labels, bodies, options, decoded dataframe contents, plotly specs, sidebar vs main, tab labels), dropping only widget ids, URLs and exception stack traces, and diffed old against new.
  - The synthetic generator calls `uuid.uuid4()` for narration refs and transaction IDs even when it is seeded, so the script pinned `uuid4` to a seeded RNG for each run. Without that, the transaction tables differed only in those random suffixes.
  - Result: `9/9 scenarios identical`.

  | Scenario | Element nodes |
  |---|---|
  | landing_angel / landing_vc / landing_workbench | 56 each |
  | network_empty | 11 |
  | angel_generated | 72 |
  | vc_generated | 112 |
  | workbench_generated_claims | 123 |
  | workbench_affiliate_selected | 33 (same exception, see known gaps) |
  | network_cohort (3 companies, seeded) | 50 |

- `pytest tests/test_demo_ui.py -q`: **9 passed**.
- `pytest -q` (full suite including regression): **1024 passed, 1 xfailed, 1 xpassed**, 0 failed.
- `pytest tests/regression -q -s`: **7 passed, 1 xfailed, 1 xpassed**, with every metric the same as the table above (100.0 / 99.9 / 88.5 n=13 / 90.0 n=10 / 66.6 pairs=176521). `gold_regression.json` is identical to the previous run except for `timestamp`.
- `ruff check .`: clean. mypy is unchanged, because `demo/` is outside its configured scope.
- `streamlit run demo/app.py --server.headless true` booted, and `/_stcore/health` returned `ok`.

### Not verified / known gaps

- **Pre-existing demo bug, found by the task 6 equivalence check and deliberately not fixed:** selecting any counterparty under "Mark as affiliates" crashes the single-company view with `ValidationError: 1 validation error for Affiliate — relationship Field required`. `affiliate_selector` builds `Affiliate(name=...)`, but `Affiliate.relationship` is a required field. The same code is in 74799d1, before the upgrade, so the bug predates it. Task 6 had to leave the UI unchanged, so it's left for a later phase. The likely fix is `relationship="user_tagged"`, or making the field optional. No test covers the affiliate path.
- **CI for the task 6 commit:** pending at the time of writing; see the push report.
- **The real LLM path was not run.** No API key was used in this session. Its request shape is checked against a stubbed SDK client (`tests/test_llm_client.py`), not against the live API.
- **The `[embeddings]` extra was not installed in a fresh venv** (torch is large). The existing `.venv`, which has `sentence-transformers`, ran the full suite.
- **OCR on a real scanned PDF wasn't run.** The tesseract binary isn't checked; only adapter *selection* is tested.
- **Linux CI:** passed for 20c2375 (see above).
- `jurisdictions/india/rules.py` still defines `_LAKH`, which is now unused there.
- **Pre-existing flaky tests, found this session and not caused by Phase 1:**
  - `tests/test_vc_lens.py::TestEdgeCases::test_generate_is_idempotent` compares XLSX byte lengths. openpyxl writes wall-clock `created`/`modified` timestamps into `docProps/core.xml`, and that was confirmed to be the only differing part. Two calls in different seconds can change the compressed size (seen once: 12087 vs 12084). `reports/vc_lens.py` is not modified in this phase. Fix in a later phase by pinning workbook properties or comparing sheet contents; it isn't fixed here because pinning would change report output.
  - `tests/test_demo.py::TestRunIsolation::test_temp_files_cleaned_up` globs the *system* temp dir, so it can fail if another process creates `tmp*` entries during the run. It failed once while a second pytest was running alongside, and passed on rerun.


---

## Phase 2: Data platform (`upgrade/p2-data-platform`, 2026-11-02)

Branched from `new_improvements` at fddaf16. This phase adds a PostgreSQL system of record, idempotent ingestion, a DQ report, a Postgres-backed job queue with a worker, a FastAPI service and docker-compose. It also fixes the affiliate-selector crash logged under Phase 1's known gaps. The decisions are in **ADR-0001** (`docs/adr/0001-postgres-data-platform.md`: Postgres + SQLAlchemy 2 + Alembic, and why `sqlalchemy`/`psycopg2-binary` come back after Phase 1 removed them as unused) and **ADR-0002** (`docs/adr/0002-postgres-job-queue.md`: `SKIP LOCKED` queue vs Celery/Redis). Both were written before any code.

### What changed

**Schema (`db/models.py`, `db/migrations/versions/0001_initial.py`).**
- Every §8.1 table is in place, with the §8.1 constraints:
  - `UNIQUE(documents.sha256)`
  - `CHECK(period_start <= period_end)`
  - `CHECK` exactly-one-of debit/credit, plus non-negative amounts and a rail allowlist
  - `UNIQUE(statement_id, txn_seq)` and `UNIQUE(run_id, kind)`
  - a partial unique index so each transaction has one `is_current` categorisation, with history kept
  - indexes `(statement_id, txn_date)`, `(customer_id)`, `(run_id, severity)` and `(status, created_at)`
- Money is `NUMERIC(18,2)` and round-trips as `Decimal`. A test covers this.
- Types are portable: `Uuid`, and `JSON` with a `JSONB` variant. `flags.transaction_ids` is `uuid[]` on Postgres and a JSON list elsewhere. Unit and API tests therefore run on SQLite.
- The migration was autogenerated, then reviewed: batch wrappers removed and type references qualified. A test asserts `upgrade head` gives no autogenerate diff against the models, and that downgrade works.
- **Deviations from §8.1, all additive:**
  - `report_artifacts` stores the rendered lens PDFs/XLSX per run. The lens renderers need the full in-memory analysis objects, which aren't rebuilt from the DB, so `/report` serves the bytes rendered at job time.
  - `documents.content` stores the uploaded bytes so the worker, a separate process, can read them.
  - `transactions.source_txn_id` keeps the pipeline's deterministic id, so flags and DQ sample ids map to DB uuids.
  - `jobs` gained `available_at` (for backoff), `options`, `run_id` and `finished_at`.
  - `runs` gained `job_id`, `document_id` and `error`.
  - The categorisation `source` vocabulary adds `default` for "no rule fired, assigned OTHER".

**DQ report (extends `pipeline/validator.py`, doesn't replace it).**
- `build_dq_report(doc, opening_balance=…, closing_balance=…)` returns `DQCheck{name, passed, severity, count, sample_ids, skipped, detail}` for each check:
  - required fields
  - date within period
  - duplicate rows (date+amount+description+balance)
  - debit/credit exclusivity
  - **running balance at ₹0.01**, where `sample_ids` are the exact break rows
  - opening and closing balance vs header
  - outliers (modified z-score on log amount, MAD)
  - non-INR currency
- **`Validator.validate()` and its ₹1 gate are unchanged.** The DQ report is informational, which is why a ₹0.50 break passes the gate but fails the DQ `running_balance` check (both are tested).
- **Closing balance vs header:** no adapter extracts a closing balance, so this check always reports `skipped`.
- **Opening balance vs header:** runs whenever the adapter supplies an opening balance (the digital-PDF adapter on the gold set does).

**`pipeline/run.py`.** These additions are behaviour-preserving; stage order and log lines are unchanged.
- `AnalysisOptions` gets `stage_observer` (a per-stage callback with stage, status, duration_ms and exception *type*) and `run_id` (so the worker's `runs` row and the pipeline share an id).
- `AnalysisResult.dq_report` is new.
- `ValidationFailedError` now carries `dq_report` and `doc`, so a failed job still persists the statement and the DQ report.

**`demo/pipeline_runner.py`.** `render_reports(analysis)` is split out of `run_pipeline_on_bytes`, which now calls it. The rendering code itself is unchanged, and the worker reuses it.

**`analysis/categoriser.py`.** New `match_rule(txn) -> (rule index, category)`, and `_rule_classify` delegates to it with identical results. It lets the DB record `categorisations.rule_id` without duplicating the rule loop.

**Ingestion (`db/ingest.py`).**
- `sha256(bytes)` is computed first. An existing hash returns the existing document and job with `duplicate=True`, and nothing is reprocessed.
- Otherwise the document and a queued job are inserted in one transaction.
- When two uploads race, the `UNIQUE` violation is caught and the winner is returned as a duplicate.

**Job queue and worker (`db/repositories/jobs.py`, `worker/`, `python -m worker`).**
- **Claiming:** `SELECT … FOR UPDATE SKIP LOCKED` on queued-and-due jobs, or running jobs whose lease has expired.
- **Retries:** a non-validation failure re-queues with exponential backoff, up to `job_max_attempts` (3). After that the job is `failed`.
- **Validation failure:** the job is failed with no retry. The statement, transactions and DQ report are still persisted, so `GET /dq` shows the exact break rows.
- **One transaction per result:** statement, transactions, categorisations, DQ report, flags, outputs, report artifacts and the job's success mark all commit together.
- **Lease ownership:**
  - The result transaction locks the job row and checks that this worker still holds the lease before writing anything.
  - A worker that lost its lease writes nothing, and it can't re-queue a job that another worker has since completed. That case was a real bug: the zombie-worker test caught it.
- **Reclaiming:** a reclaimed job's orphaned `running` run is closed as `failed/WorkerLost`.
- **Logging:** a heartbeat after each stage extends the lease, and each stage writes a `stage_executions` row (13 pipeline stages + `render_reports`).
- **Errors:** `last_error` stores the exception type only (§1.9), for example `RuntimeError` or `ValidationFailed: BALANCE_BREAK`.

**API (`api/`, `uvicorn api.main:app`).**
- Every §8.5 route is implemented, plus `/health` and `/ready`.
- **Auth:** an `X-API-Key` header checked against `API_KEY_HASH` (sha256) with `hmac.compare_digest`. It **fails closed** (503) if no hash is configured.
- **Upload checks:**
  - a suffix allowlist plus a magic-byte check (415);
  - a size limit, enforced both by a Content-Length guard before multipart parsing and by the route (413);
  - empty-file rejection (422);
  - basename-only filenames;
  - options validated with Pydantic, with alias length bounded.
- **Errors:** every error has the shape `{error: {code, message, details}}`, and validation details never echo submitted input.
- **Responses:** money is serialised as strings, never floats. `/docs` serves OpenAPI.
- **Manual override:** `PATCH /v1/transactions/{id}/category` adds a `source=manual` row and keeps the history.

**Ops.**
- `Dockerfile` is multi-stage and runs as a non-root user.
- `docker-compose.yml` runs postgres:16 with a healthcheck, `api` (which runs `alembic upgrade head`, then uvicorn, bound to 127.0.0.1) and `worker`.
- `.env.example` gains the new settings.
- Makefile gets `migrate`, `api` and `worker` targets, and `install` includes `[platform]`.
- CI gains a `postgres:16` service with `BSAA_TEST_DATABASE_URL`, and installs `[platform]`.
- CI's gated mypy scope is widened to `db/ api/ worker/`.
- CI has a step that **fails if the Postgres tests skip**.

**Dependencies.** A new `[platform]` extra: `sqlalchemy>=2.1`, `alembic`, `psycopg2-binary`, `fastapi`, `uvicorn`, `python-multipart` and `httpx`. Core dependencies are unchanged, so the pipeline and demo install as lean as Phase 1 left them. `sqlalchemy>=2.1` is needed for the `Select[T1, T2]` typing used in `api/routers/runs.py`. The new test package `importorskip`s, so a core-only venv still passes.

**Affiliate-selector crash (Phase 1 known gap).**
- `demo/components/sidebar.py` now builds `Affiliate(name=…, relationship="user_tagged")`.
- New `tests/test_demo_ui.py::TestAffiliateSelector` selects a counterparty in Workbench mode and asserts there's no exception and that all 9 tabs render.
- To prove the test catches the bug, it was run against the original `sidebar.py` (from `git show HEAD:`). It failed with `1 validation error for Affiliate relationship Field required`, the exact Phase 1 symptom, and passes with the fix.

**Deferred:** Streamlit calling the API instead of `run_analysis` (the guide says "where practical"). The UI stays local in this phase.

### Files

- **Added:**
  - `docs/adr/0001-postgres-data-platform.md`, `docs/adr/0002-postgres-job-queue.md`
  - `db/{__init__,models,session,serialize,ingest}.py`, `db/repositories/{__init__,jobs,results}.py`, `db/migrations/{env.py,script.py.mako,versions/0001_initial.py}`, `alembic.ini`
  - `api/{__init__,main,deps,errors,schemas}.py`, `api/routers/{__init__,health,statements,jobs,runs,transactions}.py`
  - `worker/{__init__,__main__,runner}.py`
  - `Dockerfile`, `.dockerignore`, `docker-compose.yml`
  - `tests/platform/{__init__,conftest,test_dq_report,test_models_migration,test_ingest_idempotency,test_jobs_worker,test_api,test_pg_queue}.py`
- **Modified:** `pipeline/validator.py`, `pipeline/run.py`, `demo/pipeline_runner.py`, `demo/components/sidebar.py`, `analysis/categoriser.py`, `config/settings.py`, `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `.env.example`, `tests/test_demo_ui.py` (one new test class appended; existing tests untouched), `docs/CHANGELOG_UPGRADE.md`
- **Removed:** none

### Verified (run in this session, Windows 11, Python 3.12.2, SQLAlchemy 2.1.6, FastAPI 0.162.0)

**Baseline (pre-edit).** This ran in a clean `git worktree` at fddaf16, because the first baseline attempt overlapped with edits and was discarded.
- `pytest -q --ignore=tests/regression`: **1016 passed, 1 failed**. The failure was `test_vc_lens.py::test_generate_is_idempotent`, the known flaky test.
- `pytest tests/regression -q -s`: **7 passed, 1 xfailed, 1 xpassed**.

**After the change (final code):**

| Check | Before | After |
|---|---|---|
| Full suite (`--ignore=tests/regression`) | 1016 passed, 1 failed (known flaky) | **1083 passed, 5 skipped, 0 failed** |
| ↳ composition | 1017 tests | 1017 pre-existing + 1 affiliate UI test + 70 platform tests (65 run, 5 Postgres-only skipped) |
| Regression result | 7 passed, 1 xfailed, 1 xpassed | 7 passed, 1 xfailed, 1 xpassed |
| Extraction / Categorisation | 100.0% / 99.9% | 100.0% / 99.9% |
| Risk recall / Compliance recall | 88.5% (n=13) / 90.0% (n=10) | 88.5% (n=13) / 90.0% (n=10) |
| Customer ID acc. | 66.6% (pairs=176521) | 66.6% (pairs=176521) |

- `results/sprint1/gold_regression.json` before vs after: **the only differing key is `timestamp`**.
- Two files changed after that full run started: the alias-length bound in `db/ingest.py` and its test in `tests/platform/test_api.py`. Re-run afterwards: `pytest tests/platform -q` gave **65 passed, 5 skipped**.
- `ruff check .`: clean. `mypy --follow-imports=silent schema/ pipeline/ config/ llm/ db/ api/ worker/`: `Success: no issues found in 41 source files`.
- **Alembic on SQLite:**
  - `alembic upgrade head` worked.
  - `alembic check` → `No new upgrade operations detected.`
  - `alembic downgrade base` worked.
  - The same round-trip is also asserted in `tests/platform/test_models_migration.py`.
- **Live processes (SQLite file DB):**
  - Started `uvicorn api.main:app` and `python -m worker` as real processes and ran the flow with curl:
    - `/health` → `{"status":"ok"}`; `/ready` → `{"status":"ready"}`
    - upload a gold PDF → 202; same bytes again → **200, `duplicate: true`**
    - the job reached `succeeded` (attempts 1)
    - `/dq` → passed, with `opening_balance_vs_header` evaluated against the PDF header and only `closing_balance_vs_header` skipped
    - `/report?lens=angel` → `application/pdf`, 5315 B
    - `/docs` → 200; a request without a key → 401
  - **Kill-and-restart acceptance criterion, with real processes:**
    - Setup: `JOB_LEASE_S=4`.
    - Worker 1 was killed with `taskkill /T /F` mid-job, after 13 stage executions, with the job `running` and **0 statements** committed.
    - One second later nothing had changed.
    - Worker 2 started, reclaimed the job after the lease and completed it in 7.9 s: job `succeeded`, attempts 2, **1 statement**.
    - The killed attempt's run was left `running` (it was later closed as `WorkerLost` by the fix above; that path is covered by `test_killed_worker_job_is_reclaimed_and_completed`).
- **Acceptance criteria → tests:**
  - same file twice → one document: `test_ingest_idempotency.py`, `test_api.py::test_upload_202_then_duplicate_200`
  - killed worker → reclaimed and completed: `test_jobs_worker.py::test_killed_worker_job_is_reclaimed_and_completed`, plus the live run above
  - corrupted balance → failing reconciliation check at the exact break row:
    - `test_jobs_worker.py::test_validation_failure_fails_job_without_retry_and_keeps_dq` (₹500 break at row 10 → first sample is `txn_seq` 10)
    - `test_small_break_passes_gate_but_dq_reports_exact_row` (₹0.50 break at row 7 → rows 7, 8)
    - `test_api.py::test_failed_validation_run_exposes_dq_but_not_summary`
  - all endpoints covered: `test_api.py` (incl. `test_openapi_lists_every_route`)
- **Affiliate fix:** the new UI test **fails on the pre-fix `sidebar.py`** with the original `ValidationError` and **passes on the fix**.
- **Security review:**
  - The `/security-review` skill could not run. It diffs against `origin/HEAD` (= `origin/main`), which shares no history with this branch: `fatal: origin/HEAD...HEAD: no merge base`.
  - A manual review of the diff was done instead: auth, upload validation, SQL construction, error/log content, headers, container user and port binding.
  - It found one gap, alias strings with no length bound, which is now fixed and tested.

### Not verified / known gaps

- **Postgres-specific tests have not run** (`tests/platform/test_pg_queue.py`: migration on PG with `jsonb`/`ARRAY`/`numeric` types, `SKIP LOCKED` with 8 threads × 20 jobs and no double claims, locked-row skipping, end to end on PG). There's no Docker or Postgres on this machine. CI runs them against a `postgres:16` service, and a CI step fails the build if they skip. **Status: implemented, not run locally; pending CI.**
- **`docker compose up` and `docker build` have not run** (no Docker here). The Dockerfile and compose file haven't been exercised.
- **Postgres behaviour SQLite doesn't exercise:** SQLite ignores `FOR UPDATE SKIP LOCKED`, so on SQLite the no-double-claim guarantee is only checked logically (lease and ownership checks), not under real concurrency.
- **Auth is a single shared API key**, with no per-tenant isolation: any valid key can read every run. This is enough for a demo; it isn't multi-tenant authorisation.
- **Uploaded statements are stored unencrypted** in `documents.content` (BYTEA), and rendered reports in `report_artifacts`. There's no retention policy yet.
- **On SQLite, timestamps come back without a UTC offset**; Postgres `timestamptz` returns them with one.
- **The categorisation review queue is `source in (default, llm)`.** On the gold set every row is rule-categorised (the rule list ends in catch-alls), so the queue is empty there. Phase 4's confidence routing is what will populate it.
- **Streamlit still calls `run_analysis` locally**, not the API (deferred).
- The pre-existing flaky tests from Phase 1 are unchanged. `test_vc_lens::test_generate_is_idempotent` failed once in the clean baseline run and passed in the final run.
