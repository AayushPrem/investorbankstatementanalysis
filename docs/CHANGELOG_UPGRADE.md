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

- **Postgres-specific tests have not run locally** (`tests/platform/test_pg_queue.py`: migration on PG with `jsonb`/`ARRAY`/`numeric` types, `SKIP LOCKED` with 8 threads × 20 jobs and no double claims, locked-row skipping, end to end on PG). There's no Docker or Postgres on this machine. CI runs them against a `postgres:16` service, and a CI step fails the build if they skip. **Status: passed in CI run 41434153131 (commit 37dd801); not run locally.**
- **`docker compose up` and `docker build` have not run** (no Docker here). The Dockerfile and compose file haven't been exercised.
- **Postgres behaviour SQLite doesn't exercise:** SQLite ignores `FOR UPDATE SKIP LOCKED`, so on SQLite the no-double-claim guarantee is only checked logically (lease and ownership checks), not under real concurrency.
- **Auth is a single shared API key**, with no per-tenant isolation: any valid key can read every run. This is enough for a demo; it isn't multi-tenant authorisation.
- **Uploaded statements are stored unencrypted** in `documents.content` (BYTEA), and rendered reports in `report_artifacts`. There's no retention policy yet.
- **On SQLite, timestamps come back without a UTC offset**; Postgres `timestamptz` returns them with one.
- **The categorisation review queue is `source in (default, llm)`.** On the gold set every row is rule-categorised (the rule list ends in catch-alls), so the queue is empty there. Phase 4's confidence routing is what will populate it.
- **Streamlit still calls `run_analysis` locally**, not the API (deferred).
- The pre-existing flaky tests from Phase 1 are unchanged. `test_vc_lens::test_generate_is_idempotent` failed once in the clean baseline run and passed in the final run.

---

## Phase 3: Synthetic corpus at scale (`upgrade/p3-corpus`, 2026-11-02)

This phase adds a reproducible 1,000-company synthetic corpus (Parquet), with labels stored apart from features, latent-health churn, `ml_only` anomalies and leakage-safe split files. `tools/synthetic_gen.py` was extended through opt-in hooks, not rewritten. Default-argument output is byte-identical to before (see "Verified"). All data is **synthetic** (generator v2.0.0, corpus v1). See `docs/ml/corpus_card.md`.

A separate housekeeping commit on this branch updates the Phase 2 entry: the Postgres tests passed in CI run 41434153131 (commit 37dd801).

### What changed

- **Generator hooks (`tools/synthetic_gen.py`, +~120 lines).**
  - There is one module-level `_HOOKS` (typed by `CorpusHooksProtocol`), set only inside `corpus_mode(hooks)`. Every hook point is `if _HOOKS is not None`, so default runs execute the same statements and RNG draws as before.
  - The hook points:
    - `_make_txn`: seeded txn_id; adds `template_id`, `counterparty_key`, `txn_time`
    - `_narration`
    - `_hex8`
    - `_customers()` in the three B2B generators
    - a `customer_payment()` call in their revenue loops
    - `_today()`, which replaces the one `date.today()` call (fixes the Phase 0 latent-determinism note)
    - the within-day sort key in `_compute_balances`
  - The three new `_Txn` fields default to `None` and aren't read by `_build_truth` or the renderers.
  - New constant: `GENERATOR_VERSION = "2.0.0"`.
- **Corpus mode (`tools/synthetic_corpus.py`, new).** Provides `CorpusHooks`, `make_spec` and `generate_company` (in memory, with an optional PDF + truth JSON under `rl_config.invariant`). It adds:
  - **Per-company parameters:** profile mix, bank, 12–24 months, start 2022-04..2024-12, `revenue_scale ~ LogNormal(0, 0.5)` (applied as a roundness-preserving post-pass), B2B customer count log-uniform from 5 (max 200 saas / 80 startup / 40 services). Names for the company, founder, customers, vendors (12 pools) and employees are drawn per company.
  - **Narration diversity:** 95 templates over 8 rails, random UTR/ref/VPA, merchant-name variants, about 5% casing and about 5% spacing noise, truncation at 60 (HDFC) / 80 (ICICI) characters. `template_id` goes to labels.
  - **Latent-health churn,** exactly as approved in plan mode:
    - `λ = sigmoid(a_p − 1.0·health − 0.35·ln(1+tenure) + N(0, 0.5²))`, with `a_p` = −3.0 / −2.6 / −2.2
    - 55% of churners are signallers with a 1–3 month lead window
    - late payment / downgrade / skip base rates apply to everyone
    - the legacy `customer_churn` injector is excluded in corpus mode
  - **Three `ml_only` injectors** (`ml_vendor_amount_outlier`, `ml_first_time_large_counterparty`, `ml_off_hours_burst`, p = 0.15 each), with rule-evasion guards. They live in `ML_ONLY_INJECTORS`, not `_FLAG_INJECTORS`.
  - **Two changes from the approved plan:**
    1. **Spike guard.** The plan capped each amount so the month's net moves < 0.5σ. On a 300-company sample that cap blocked `ml_first_time_large_counterparty` in 52% of selected companies, mostly stable-cashflow ones, which would have skewed the anomaly toward volatile profiles. It is replaced by the direct check the cap stood for: the set of spike_drain-flagged months must be unchanged, the touched month must not have been flagged before, and its leave-one-out |z| must stay ≤ 2.0 (the rule fires at 2.5). Injection success on that sample went from 48% to 86% (first-time), 82% to 95% (vendor outlier) and stayed at 100% (off-hours).
    2. **Constant injector names.** The legacy injectors' constant counterparty names (concentration whale, round-trip advisory firm, GST-gap revenue party, cash-loan lender) are remapped per company in corpus mode. Otherwise the names would label the anomaly.
- **`datasets/` (new package, mypy-gated):**
  - `schema.py`: feature vs label Parquet schemas, `JOIN_KEYS`, `FORBIDDEN_FEATURE_TOKENS`.
  - `build_corpus.py`:
    - `--n-companies`, `--seed`, `--small` (50 companies, ≤ 12 months, 5 PDFs → `data/corpus/v1-small`), `--workers`, `--pdf-subset`, `--no-ml-only`, `--out`
    - Each company is generated from its own derived seed, in process-pool chunks merged in order.
    - Output is staged and then swapped in. It refuses to delete a directory without a corpus `manifest.json`.
    - The manifest has no timestamps and a combined `corpus_hash`.
  - `splits.py`:
    - one shared company partition (70/15/15)
    - cross-bank (HDFC → ICICI)
    - unseen-vendor by counterparty key (generic keys always train)
    - temporal churn (company, cutoff) with calendar thresholds T1 < T2
    - segmentation holdout (20%)
  - `duckdb_stats.py`: feature-only views by default (labels under `label_`), the corpus-card stats, a precursor-visibility query and a DuckDB-vs-pandas benchmark.
- **Packaging / CI / Makefile:**
  - New `[ml]` extra (`pyarrow`, `duckdb`, `scikit-learn`); `datasets*` added to package discovery.
  - `data/corpus/` gitignored.
  - CI installs `[ml]`, builds `--small` and its splits, and gates mypy on `datasets/`.
  - New Makefile targets `corpus` and `corpus-small`.
  - ADR `docs/adr/0003-parquet-duckdb-corpus.md`.
- **Docs:** `docs/ml/corpus_card.md`, with every number from the 1,000-company run below.

### Files

- **Added:** `tools/synthetic_corpus.py`, `datasets/__init__.py`, `datasets/schema.py`, `datasets/build_corpus.py`, `datasets/splits.py`, `datasets/duckdb_stats.py`, `tests/test_synthetic_gen_hooks.py`, `tests/test_corpus.py`, `tests/test_splits.py`, `docs/ml/corpus_card.md`, `docs/adr/0003-parquet-duckdb-corpus.md`
- **Modified:** `tools/synthetic_gen.py`, `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `.gitignore`, `docs/CHANGELOG_UPGRADE.md`
- **Removed:** none

### Verified (run in this session, Windows 11, Python 3.12.2, pyarrow 26.0.0, DuckDB 2.0.0, scikit-learn 1.9.1)

| Check | Before (37dd801 + changelog fix) | After |
|---|---|---|
| `pytest tests/ -q --ignore=tests/regression` | 1083 passed, 5 skipped | **1123 passed, 5 skipped** (+40 new) |
| `pytest tests/regression` | 7 passed, 1 xfailed, 1 xpassed | 7 passed, 1 xfailed, 1 xpassed |
| Extraction / Categorisation | 100.0% / 99.9% | 100.0% / 99.9% |
| Risk recall / Compliance recall | 88.5% (n=13) / 90.0% (n=10) | 88.5% (n=13) / 90.0% (n=10) |
| Customer ID acc. | 66.6% (pairs=176521) | 66.6% (pairs=176521) |
| Gold set (`build_gold_set --force`), sha256 of 44 files | baseline | **identical** |
| 60-case generator fingerprint (5 profiles × 2 banks × clean/realistic/manual, every injector, PDF + truth) | `792734698b48…71b5` | **identical** |

- `results/sprint1/gold_regression.json` before vs after: **the only differing key is `/timestamp`**.
- Default-path identity was re-checked after the last code edit (type-only): fingerprint and gold set identical again.
- `tests/test_synthetic_gen_hooks.py` pins the truth-JSON hashes of 10 default-argument cases, taken over **LF-normalised** bytes (see the CI fix below). Both the original and the re-pinned values were produced by the **pre-change** generator (`git show 37dd801:tools/synthetic_gen.py`, swapped in temporarily) and match the hooked generator.
- **Acceptance (same hash twice), final code (2515990):** `python -m datasets.build_corpus --n-companies 1000 --seed 42` run twice gave `corpus_hash 8a28bbe2c231577f9f8f05c21e6a180ce25d26a40bf224c007db0dde5fc4f3d1` both times, with byte-identical manifests. That's 617,483 transactions, 23,233 customers, 1,710 flags and 30 PDFs, built in 24–25 s with 4 workers.
  - Before the newline fix the hash was `e086b173…f30`. It matched across three builds, including a 1-worker build that took 67.0 s.
  - The two builds differ only in the 30 truth JSON files (CRLF → LF); every Parquet and PDF file is identical, and the row counts are unchanged.
- `python -m datasets.splits data/corpus/v1 --seed 42` gave `splits_hash a905e9f115aae4f98f311806d6701e41d5527056164fa2c6a03372dfdc6d9efe` on two runs before the fix and two after.
- **CI fix (2515990):**
  - **Failure:** CI run 41442150062 on bc42c68 failed in the Test step. The job log needs auth (HTTP 403 without `gh`), so the failing test names weren't read.
  - **Cause, confirmed locally:** `_build_truth` uses `Path.write_text`, which writes CRLF on Windows and LF on Linux, so the Windows-computed truth-JSON pins can't match on Linux. The clean ICICI e-commerce case hashes to `c8024c88…` raw and `de42a89c…` after CRLF → LF.
  - **Fix:** the test now hashes LF-normalised bytes. The corpus stores truth JSON with LF, and both manifests are written with LF, so `corpus_hash` no longer depends on line endings. The generator's default output is unchanged.
  - **Re-run after the fix:** Phase 3 + generator tests 68 passed; `ruff` clean; gated mypy clean (46 files).
- **Label isolation:** `tests/test_corpus.py` asserts that:
  - feature ∩ label columns ⊆ {txn_id, company_id, bank}
  - no feature column name contains a label token
  - the DuckDB feature views expose no label column
  - txn_ids are opaque
  - narrations contain no label words or legacy constant names
  - The column-overlap test caught a real collision during development (`labels/flags.description` vs the feature `description`), now renamed to `flag_description`.
- **ml_only vs rules:**
  - On 12 generated companies, the six real `analysis/risk.py` detectors never include an ml_only row in a flag.
  - Removing ml_only injections leaves every other row identical.
  - ml_only amounts are non-round, non-cash debits; burst times fall between 01:00 and 04:30.
- **Ingestion:** all 30 rendered PDFs went through `pipeline.run.run_analysis` (scratch script). 30/30 passed validation with row counts equal to the feature table; one is also covered by a test.
- **Corpus stats** (from `python -m datasets.duckdb_stats data/corpus/v1 --bench`):
  - churn in period: 30.3% saas / 39.3% services / 49.5% startup
  - churn by health band: 74.2% (< −1) to 9.3% (≥ 1)
  - precursor visibility, churners' last 90 days vs non-churners: 27.1% vs 17.2% gaps > 45 days, 29.0% vs 22.1% amount drops > 5%
  - DuckDB 0.52 s vs pandas 1.05 s on the benchmark aggregation
- `ruff check .`: clean. `mypy --follow-imports=silent schema/ pipeline/ config/ llm/ db/ api/ worker/ datasets/`: `Success: no issues found in 46 source files`.
- Informational mypy on `tools/synthetic_gen.py`: 38 errors before and 38 after. `tools/synthetic_corpus.py` has 2, both calls into pre-existing untyped generator functions.
- **Security:** manual review (the `/security-review` skill can't run here, no merge base with `origin/HEAD`). Covered: deletion safety of the output swap, SQL built from paths (CLI-only, quotes escaped), no new logging of narrations or amounts, no network or secrets. No findings.

### Not verified / known gaps

- **CI passed** in run 41444209874 on 2515990 (Ubuntu, Python 3.11). Every step was green: lint, gated and informational mypy, gold-set build, small corpus + splits build, tests, Postgres tests, regression. The first run (41442150062, bc42c68) failed on the CRLF pins described above.
- **Cross-OS hash equality isn't verified.** CI builds only the `--small` corpus, and its hash wasn't compared with a Windows build of the same arguments. The 1,000-company hash is claimed for this machine (pyarrow 26.0.0) only.
- **Class balance is skewed by the profiles:** REVENUE 55.2%, and **LOAN_OUT has 0 rows**. Phase 4 can't evaluate LOAN_OUT.
- **6.9% of rows are inline narrations that aren't diversified:** restaurant Swiggy/Zomato UPI, legacy cash/FX/self-transfer injectors.
- **`ml_first_time_large_counterparty` lands in 143 of the 174 companies selected; `ml_vendor_amount_outlier` in 132 of 140.** In the rest, the guard can't be met in 10 tries.
- **The churn temporal val split is small** (82 units vs 3,610 train / 232 test), because a val company's statement has to span T1..T2.
- **Churn is produced by the same latent model the card describes.** A churn model trained here recovers the simulator, not real behaviour.
- `make corpus` / `make corpus-small` were not run through make (only the underlying commands).

---

## Phase 4: Hybrid categoriser (`upgrade/p4-categoriser-ml`, 2026-11-02)

This phase adds a supervised categoriser and confidence routing: high-precision rule → ML ≥ τ_accept → LLM (picking from the ML top-3) → review queue. It also adds a rule precision audit (D10) and an evaluation on the three leakage-safe Phase 3 splits. All metrics are on **synthetic data** (generator v2.0.0, corpus v1); see `docs/ml/categoriser.md`. `categoriser` is bumped to **3.0.0** in `AGENT_VERSIONS`.

### What changed

- **Routing (`analysis/categoriser.py` v3).**
  - Steps:
    1. A rule from the audited `high_precision_rules` list (first-match semantics kept).
    2. Model p_max ≥ τ_accept.
    3. If an LLM is configured, the LLM chooses among the model's top-3. An answer outside them, or REVIEW, goes to review.
    4. With no LLM, or a failed call, p_max ≥ τ_review → ml (degraded accept); otherwise review, keeping the model's best guess so downstream stages never see `None`.
  - **No model → v2, unchanged.** The full rule list with the catch-alls runs, and the categories are identical to before (tested).
  - `Categoriser(model="auto" | None | CategoriserModel)`. `CATEGORISER_ML_ENABLED=false` forces v2.
  - The routing decisions (`route_ml`, `route_without_llm`, `route_llm_answer`) are pure functions with their own tests.
- **Per-row provenance.**
  - `CanonicalTransaction` gains `category_source` and `category_confidence` (optional, additive).
  - `CategorisationStats` gains `ml_matched`, `review` and `model_version`. The invariant is now rule + ml + llm + review + other = total.
  - `db/repositories/results._categorisation` writes source, confidence and the ML `model_version` from them instead of re-deriving.
  - Migration **0002** adds `review` to `ck_categorisations_source`. SQLite rebuilds the table from an explicit definition, since it can't alter or reflect CHECKs; Postgres drops and re-creates the constraint.
- **ML package (`ml/`, new).**
  - `features/categoriser.py`: one feature builder for training (corpus rows) and serving (`CanonicalTransaction`).
  - `registry.py`: `model.joblib` + `metadata.json` (git sha + dirty flag, corpus/splits hash, split, params, metrics, library versions, artifact sha256, checked before unpickling).
  - `categoriser/{data,rule_audit,train,evaluate,infer,metrics}.py`, `config.yaml`, `README.md`.
  - `data.py` refuses to run unless `corpus_hash` and `splits_hash` match the pinned values (`--allow-unpinned` for CI's v1-small).
- **Config.**
  - `config/thresholds.yaml → categoriser` gains `model_version`, `tau_accept`, `tau_review` and `high_precision_rules`; the loader learns `str` and `tuple[int, ...]`.
  - `config/settings.py` gains `categoriser_model_version` (env override) and `categoriser_ml_enabled`.
- **Infra.**
  - `[ml]` extra += joblib, matplotlib, lightgbm.
  - `ml*` is packaged.
  - `.gitignore`: `models/**` except `metadata.json`.
  - Makefile: `categoriser-audit`, `categoriser-train`, `categoriser-eval`; gated mypy now covers `ml/`.
  - CI trains an LR (C=1) on v1-small and exports `CATEGORISER_MODEL_VERSION` before the tests and the regression.
  - ADR-0004 covers the local registry and the pickle trust boundary.
- **Decisions made in this session (by the user):**
  - CI trains its own small model rather than committing the artifact.
  - matplotlib, lightgbm and MiniLM were approved.
  - **τ policy floor:** thresholds.yaml holds max(validation τ, 0.90 / 0.50), because the corpus can't calibrate τ.
  - **SELF TRANSFER label inconsistency:** documented, to fix in the generator in a later phase; rule 7 stays excluded.

### Rule precision audit (D10): `python -m ml.categoriser.rule_audit data/corpus/v1`

Selected on company train+val. High precision means precision ≥ 0.99, support ≥ 50, not a generic fallback and not template-concentrated. Result: `[0, 1, 4, 5, 8, 9, 10]`. The full table is in the model card. Notable:
- `self_transfer`: 0/91 correct (the corpus labels these FOUNDER_WITHDRAWAL).
- `rail_debit_vendor`: 0.6689, and `catchall_debit_vendor`: 0.5163. Together they mislabel 43,349 SALARY rows as VENDOR_PAYMENT.
- `personal_suffix`: flagged as generator-fitted (one template).

### Training: `python -m ml.categoriser.train data/corpus/v1 --split <s> --n-jobs 3|4`

- The three splits ran as concurrent processes. Unseen-vendor took 1,723 s, including 643 s of MiniLM encoding over 137,352 unique normalised narrations. Company and cross-bank were re-run together in 1,057 s.
- The first concurrent attempt crashed the calibrated-SVC step on company and cross-bank with `WinError 1450` (nested process × grid × fold parallelism). Calibration folds now run sequentially inside a parallel grid.
- An earlier sequential run was stopped for two reasons. A new test found the VPA mask swallowing the merchant name before `/handle` (fixed: `[A-Za-z0-9._-]+@…`). Later, the run was restarted with parallelism. No model from those runs was kept.

| split | candidate | val macro-F1 | test macro-F1 | val acc | test acc | chosen (val) |
|---|---|---|---|---|---|---|
| company | lr (C=10) | 0.9576 | 0.9675 | 0.9998 | 0.9999 |  |
| company | svc_cal (C=0.1) | 0.9964 | 1.0000 | 1.0000 | 1.0000 | yes |
| company | lgbm | 0.9946 | 0.9624 | 1.0000 | 1.0000 |  |
| cross_bank | lr (C=10) | 0.9395 | 0.9352 | 0.9997 | 0.9985 |  |
| cross_bank | svc_cal (C=0.3) | 1.0000 | 0.9967 | 1.0000 | 0.9999 | yes |
| cross_bank | lgbm | 0.9630 | 0.9416 | 1.0000 | 0.9811 |  |
| unseen_vendor | lr (C=3) | 0.9698 | 0.9854 | 0.9998 | 0.9998 |  |
| unseen_vendor | svc_cal (C=0.1) | 0.9762 | 0.9762 | 1.0000 | 0.9999 | yes |
| unseen_vendor | lgbm | 0.9762 | 0.9640 | 1.0000 | 0.9994 |  |
| unseen_vendor | lr + MiniLM | 0.9429 | 0.9822 | 0.9998 | 0.9998 |  |

LightGBM and MiniLM are not kept: neither wins on validation. Production model: `cat3-svc_cal-company-8a28bbe2-191ad9` (3.1 MB).

### Evaluation: `python -m ml.categoriser.evaluate data/corpus/v1 --write-thresholds`

Before (rules v2) vs after (hybrid, no LLM), test splits, τ frozen from validation:

| split | variant | n | accuracy | macro-P | macro-R | macro-F1 |
|---|---|---|---|---|---|---|
| company | rules_v2 | 93217 | 0.9172 | 0.7392 | 0.6561 | 0.6648 |
| company | hybrid_no_llm | 93217 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| cross_bank | rules_v2 | 306481 | 0.9335 | 0.7438 | 0.6725 | 0.6846 |
| cross_bank | hybrid_no_llm | 306481 | 1.0000 | 1.0000 | 0.9835 | 0.9911 |
| unseen_vendor | rules_v2 | 58925 | 0.9950 | 0.6639 | 0.5153 | 0.5266 |
| unseen_vendor | hybrid_no_llm | 58925 | 0.9999 | 1.0000 | 0.9417 | 0.9646 |

- Macro averages are over classes with test support. **LOAN_OUT has 0 rows and can't be evaluated.**
- REVENUE is 55% of rows, so accuracy hides minority-class errors; the macro and per-class tables are the ones to read.
- On the 6.9% inline (undiversified) rows, rules_v2 and hybrid both score 1.0000, so those rows say nothing about generalisation.
- **τ:** company validation gives τ_accept = τ_review = **0.30**, the grid floor, because precision is ≥ 0.97 at every τ. thresholds.yaml holds the policy floor **0.90 / 0.50**.
  - With policy τ on test: rule 34.71 / 15.38 % (company / unseen-vendor).
  - ML band 0.5–0.9: 0.008 / 0.017 / 0.025 % of rows (company / cross-bank / unseen-vendor); review 0%.
  - Precision of auto-accepted rows 1.0000 / 1.0000 / 0.9999.
  - Expected LLM calls if a key were set: **0.08 / 0.14 / 0.22 per 1,000 transactions**. The true class is in the top-3 for 100% of band rows.
  - Validation top-label ECE ≈ 0.0001.
- **Latency** (1,000-row document, median of 5): v3 categorise **112.0 ms per 1k rows**; model predict 108.8 ms; v2 rules 27.8 ms.
- Outputs: `results/categoriser/cat3-svc_cal-company-8a28bbe2-191ad9/` (gitignored), containing metrics, per-class CSVs, confusion PNG + CSV, precision–coverage PNG + CSV, routing, latency and `report.md`.

### Files

- **Added:**
  - `ml/__init__.py`, `ml/registry.py`
  - `ml/features/{__init__,categoriser}.py`
  - `ml/categoriser/{__init__,data,rule_audit,train,evaluate,infer,metrics}.py`, `ml/categoriser/config.yaml`, `ml/categoriser/README.md`
  - `models/categoriser/*/metadata.json` (9 models)
  - `db/migrations/versions/0002_categorisation_review_source.py`
  - `docs/ml/categoriser.md`, `docs/adr/0004-categoriser-model-registry.md`
  - `tests/test_ml_categoriser.py`, `tests/test_categoriser_routing.py`
- **Modified:**
  - `analysis/categoriser.py`
  - `schema/canonical.py`
  - `config/{settings,thresholds}.py`, `config/thresholds.yaml`
  - `pipeline/{run,versioning}.py`
  - `db/models.py`, `db/repositories/results.py`
  - `worker/runner.py`
  - `pyproject.toml`, `Makefile`, `.gitignore`, `.github/workflows/ci.yml`
  - tests: `tests/test_categoriser.py`, `tests/test_thresholds.py`, `tests/platform/{test_api,test_jobs_worker,test_models_migration}.py`
- **Removed:** none

### Existing tests consciously updated

- `test_categoriser.py::test_rule_match_rate_is_high` is now pinned to `model=None`, the v2 path. Under v3 the catch-alls aren't rules, so the rule rate drops by design. New companion: `test_v3_deterministic_coverage_is_high` (rule + ml ≥ 90%).
- `test_stats_totals_match_transaction_count`: the invariant now includes `ml_matched + review`.
- `TestLLMPlumbing` (2 tests): pinned to `model=None`, since they test the v2 LLM path. v3 LLM routing is tested in `test_categoriser_routing.py`.
- `platform/test_jobs_worker.py::test_gold_pdf_end_to_end`: expects categoriser `3.0.0`.
- `platform/test_api.py`:
  - `test_transactions_paginated_and_filtered` assumed "the gold set is fully rule-categorised". It now checks that the per-source filter partitions the run.
  - `test_manual_override_keeps_history` now expects the original source (rule or ml) in the history row.
  - Both pass with the model and with `CATEGORISER_ML_ENABLED=false`.

### Verified (run in this session, Windows 11, Python 3.12.2, scikit-learn 1.9.1, LightGBM 4.7.0, pyarrow 26.0.0)

| Check | Before (0f59c80) | After |
|---|---|---|
| corpus / splits rebuild | n/a | `corpus_hash 8a28bbe2…f3d1`, `splits_hash a905e9f1…` (match the pins) |
| `pytest tests/ -q --ignore=tests/regression` | 1123 passed, 5 skipped (Phase 3 entry) | **1172 passed, 5 skipped** (production model configured) |
| `pytest tests/regression` | 7 passed, 1 xfailed, 1 xpassed | 7 passed, 1 xfailed, 1 xpassed |
| Extraction / Categorisation | 1.0 / **0.9991** | 1.0 / **0.9986** |
| Risk recall / Compliance recall | 0.8846 (n=13) / 0.9 (n=10) | 0.8846 / 0.9 (unchanged) |
| Customer ID accuracy | 0.6662 (pairs=176521) | 0.6662 (unchanged) |
| CI simulation (v1-small LR via `CATEGORISER_MODEL_VERSION`) | n/a | categoriser/API/worker tests 130 passed; regression categorisation 0.998 |

- **`gold_regression.json` diff:** the only changed metric is `categorisation_accuracy` 0.9991 → 0.9986 (4,367 → 4,365 of 4,371 matched rows). From per-row inspection:
  - **+2:** ZENITH CAPITAL ADVISORY round-trip debits are now TRANSFER (v2 said VENDOR_PAYMENT).
  - **−4:** `NEFT … SELF TRANSFER XXXX3425` in `icici_services_firm_80e19b` is TRANSFER in the gold truth, but the model says FOUNDER_WITHDRAWAL (debits, p 0.99) or REVENUE (credits, p 0.56–0.57). The corpus labels "SELF TRANSFER" as FOUNDER_WITHDRAWAL, so this is a generator labelling inconsistency, not a model bug. Target 0.88 still passes.
- Earlier full-suite runs in the session:
  - **No model configured:** 2 failures. One was the pinned `2.0.0` (fixed). The other was `test_vc_lens::test_generate_is_idempotent`, the known timestamp flake, which ran under CPU load from training and passed 3/3 when re-run alone.
  - **With the model:** 2 `test_api` failures, the conscious updates above.
- `ruff check .` clean. `mypy --follow-imports=silent schema/ pipeline/ config/ llm/ db/ api/ worker/ datasets/ ml/`: `Success: no issues found in 58 source files`.
- **Security (manual; `/security-review` can't run here):**
  - New log lines carry counts, versions, timings and exception types only.
  - `joblib.load` is reached only via `registry.load`, after the path-component check and the sha256 match.
  - Model location and version come from operator config or env, never from uploads.
  - No findings.

### Not verified / known gaps

- **CI run 41470632531 (7d7653c) failed in the Test step.** Every earlier step passed, including training the CI categoriser model. Job logs need auth (HTTP 403), so the failing tests weren't read. Running the full suite locally with the CI's v1-small model passed (1172 passed, 5 skipped), so the failure is environment-specific.
  - **Cause, found from offline Postgres DDL** (`alembic upgrade 0001:0002 --sql` with a postgres URL): migration 0002 emitted `DROP CONSTRAINT ck_categorisations_ck_categorisations_source`. The metadata naming convention was applied to an already-final name. SQLite takes the table-rebuild path, so the local tests couldn't catch it; CI's Postgres tests run every migration.
  - **Fix:** `op.drop_constraint(op.f(_CK), …)`. The offline DDL is now `DROP CONSTRAINT ck_categorisations_source` / `ADD CONSTRAINT ck_categorisations_source CHECK (… 'review' …)`, and the downgrade mirrors it.
  - **CI passed** in run 41472865039 on 467af43 (Ubuntu, Python 3.11, Postgres 16). Every step was green: lint, gated and informational mypy, gold-set build, small corpus + splits, CI categoriser training (v1-small LR), tests, Postgres tests (not skipped), regression.
- **No LLM was called.** The LLM-with-top-3 path is tested with mocks only. "LLM calls per 1k" is an estimate from routing.
- **τ is a policy floor, not a fitted value.** The synthetic corpus is too easy to calibrate it; re-tune on labelled real data.
- **Near-perfect scores reflect template-generated narrations.** Real-world performance is unknown. The honest D10 comparison is rules_v2 macro-F1 0.66–0.68 vs hybrid 0.96–1.00 on the same synthetic splits.
- **SELF TRANSFER labelling differs between corpus mode and the legacy generator.** It costs 4 gold rows; fixing it needs a generator change and a new corpus version.
- **High-precision rule 8 (`INV-` credit → REVENUE) costs TRANSFER recall** on cross-bank (0.94 → 0.85) and unseen-vendor (0.75 → 0.65), on 54 and 20 rows.
- **The unseen-vendor split has no SALARY, TAX or FEES test rows** (generic counterparties are always train).
- **Grid edges:** cross-bank SVC chose C=0.3, the top of {0.1, 0.3}.
- **Models were trained from an uncommitted working tree on base 0f59c80.** Their metadata says `git_sha 0f59c80` and predates the `git_dirty` field.
- `make categoriser-*` targets weren't run through make (only the underlying commands).


---

## Phase 5: Transaction anomaly detection (`upgrade/p5-anomaly`, 2026-11-02)

This phase adds an unsupervised IsolationForest that flags transactions as unusual for their account, as `kind=ml` "investigate" flags. The flags are a separate stream from the rule detectors (ADR-0005): rule flags, composite score, narrative and gold regression are unchanged. All metrics are on **synthetic data** (corpus v1) and measure detection of *synthetic injections*; see `docs/ml/anomaly.md`. `AGENT_VERSIONS` gains `ml_anomaly: 1.0.0`; `risk` stays 1.0.0.

### What changed

- **Features (`ml/features/transaction_features.py`, new).**
  - The §11 set: log amount; robust z against the same counterparty and against the same predicted category; first-seen counterparty; counterparty share of month-to-date outflows; 1d/7d velocity; day of week; month-end; days since the counterparty's last row.
  - Plus `is_credit`.
  - The pooled model uses a causal account-relative view instead (`acct_amount_z`, `velocity_*_rel`).
  - **No look-ahead:** one ordered pass per account, where each row reads its state before it is added. `tests/test_transaction_features.py` proves it with prefix invariance, appended future rows, same-day ordering and account independence.
  - **No labels:**
    - Category = Phase 4 categoriser predictions (`cat3-svc_cal-company-8a28bbe2-191ad9`, LLM off; v2 rules if no model).
    - Counterparty = `counterparty_from_narration`: aggregator match, otherwise the first 3 name tokens after Phase 4 masking and removal of rail words, legal suffixes and bank codes.
    - `clean_counterparty` alone left reference codes in place: 49k keys against 5k true counterparties on a 60k-row sample. The new key gives purity 0.979 and completeness 0.800 on test.
  - **No `txn_time`:** PDFs and `CanonicalTransaction` have none. So `ml_off_hours_burst` can only be found through its velocity and new-payee traits, and the off-hours property can't be detected from PDF input.
- **Model (`ml/anomaly/`, new): `data, model, train, evaluate, infer, metrics`, `config.yaml`, `README.md`.**
  - **Scoring:**
    - per-account IsolationForest (fitted at scoring time) when the account has ≥ `min_history` rows
    - otherwise the pooled forest, fitted on train companies
    - Scores become percentiles against the scorer's reference distribution, and each flag records its scorer.
  - **Explanations:** top-3 per-feature deviations (|x − median| / IQR). No SHAP.
  - **Reuse:** `ml/registry.py` and the Phase 4 hash gate (`check_gate`).
- **Pipeline.**
  - The `ml_anomaly` stage runs after `risk` (`pipeline/run.py`) and fills `AnalysisResult.anomaly_report`.
  - It is skipped entirely, with no stage entry, when the model is disabled, unset, missing or fails its sha check, or when sklearn is absent.
  - If detection fails, the error is logged and there are no ML flags; it never blocks the rule outputs.
  - `summarise_result` adds `ml_flag_count` and `ml_model_version` only when the stage ran.
- **Persistence and UI.**
  - `persist_analysis` writes ML flags with `kind='ml'` and `score`. The CHECK already allowed it, so **no migration** (and no Postgres-only DDL to check).
  - The workbench lens gets an "Investigate (ML)" sheet and PDF block; the demo risk tab gets an expander. Both appear only when ML flags exist.
  - Angel and VC lenses and the narrative are unchanged.
- **Config.**
  - `config/thresholds.yaml → anomaly`: `model_version, contamination, min_history, severity_high_pct, severity_medium_pct, max_flags_per_statement`, with `AnomalyThresholds`.
  - `config/settings.py` gains `anomaly_model_version` (env override) and `anomaly_ml_enabled`.
  - **Fix that came with this:** Phase 4's `write_thresholds` regex matched `  model_version:` anywhere in the file, and would have broken once a second section had that key. Both writers now use `config.thresholds.update_section`, which edits only lines inside the named section.
- **Infra.**
  - Makefile: `anomaly-train`, `anomaly-eval`.
  - CI trains a 50-tree model on v1-small after the categoriser step and exports `ANOMALY_MODEL_VERSION`.
  - ADR-0005 records the separate flag stream.

### Training and selection: `python -m ml.anomaly.train data/corpus/v1 --n-jobs 3`

- **Hash gate:** before training, the rebuilt corpus gave `corpus_hash 8a28bbe2c231577f9f8f05c21e6a180ce25d26a40bf224c007db0dde5fc4f3d1` and `splits_hash a905e9f115aae4f98f311806d6701e41d5527056164fa2c6a03372dfdc6d9efe`, matching the pins.
- **Pooled forest:** 431,417 rows from 700 train companies, fit in 11.2 s.
- **Selection:** on 150 validation companies (92,849 rows).

| min_history | accounts pooled (val) | micro P@20 | AP |
|---|---|---|---|
| **150** | 2 | **0.0887** | 0.0991 |
| 250 | 16 | 0.0817 | 0.0929 |
| 400 | 51 | 0.0747 | 0.0817 |

| contamination | flagged | TP | precision | recall | F1 |
|---|---|---|---|---|---|
| 0.002 | 258 | 62 | 0.2403 | 0.0963 | 0.1375 |
| **0.005** | 547 | 107 | 0.1956 | 0.1661 | **0.1797** |
| 0.01 | 1009 | 148 | 0.1467 | 0.2298 | 0.1791 |
| 0.02 | 1753 | 193 | 0.1101 | 0.2997 | 0.1610 |
| 0.05 | 2769 | 255 | 0.0921 | 0.3960 | 0.1494 |

- **Severity.**
  - **First run.** The targets were 0.30 / 0.15 and the cutoff grid started at 0.95. MEDIUM landed at 0.95, below the flag threshold, because the whole flagged set (0.196) already met 0.15.
  - **Fix.** The grid now starts at 1 − contamination and the targets are 0.40 / 0.25. This was decided on validation only, before test was scored.
  - **Result.** MEDIUM ≥ 0.9986 (val precision 0.251). HIGH can't reach 0.40 (the highest on val is 0.304), so it falls back to the default 0.999 (val 0.277).
  - The first-run model was deleted.

### Evaluation: `python -m ml.anomaly.evaluate data/corpus/v1 --write-thresholds` (test, once)

- **Test set:** 150 companies, 93,217 rows. Prevalence: any 0.695%, ml_only 0.172%.
- **Scorers used:** per-account for 148 companies, pooled for 2.
- **Reading the table:** "mac" is the per-company mean, "mic" is pooled. The full table is in the model card.

| method | label | positives | P@10 mac | R@10 mac | P@20 mac | R@20 mac | R@20 mic | P@50 mac | R@50 mac | AP |
|---|---|---|---|---|---|---|---|---|---|---|
| iforest | any | 648 | 0.143 | 0.367 | 0.098 | 0.483 | 0.454 | 0.055 | 0.664 | 0.120 |
| zscore_baseline | any | 648 | 0.133 | 0.307 | 0.095 | 0.419 | 0.441 | 0.053 | 0.605 | 0.097 |
| iforest_all_pooled | any | 648 | 0.077 | 0.201 | 0.061 | 0.312 | 0.284 | 0.040 | 0.492 | 0.059 |
| iforest | rule | 342 | 0.061 | 0.281 | 0.045 | 0.411 | 0.395 | 0.025 | 0.567 | 0.037 |
| iforest | compliance | 146 | 0.049 | 0.569 | 0.028 | 0.644 | 0.582 | 0.014 | 0.768 | 0.101 |
| zscore_baseline | compliance | 146 | 0.060 | 0.633 | 0.041 | 0.801 | 0.836 | 0.018 | 0.901 | 0.055 |
| iforest | ml_only | 160 | 0.033 | 0.416 | 0.025 | 0.536 | 0.463 | 0.015 | 0.771 | 0.022 |
| zscore_baseline | ml_only | 160 | 0.022 | 0.189 | 0.020 | 0.341 | 0.375 | 0.013 | 0.596 | 0.021 |
| iforest | ml_first_time_large_counterparty | 41 | 0.015 | 0.672 | 0.009 | 0.759 | 0.659 | 0.004 | 0.862 | 0.015 |
| iforest | ml_off_hours_burst | 100 | 0.013 | 0.183 | 0.013 | 0.352 | 0.390 | 0.009 | 0.668 | 0.010 |
| zscore_baseline | ml_off_hours_burst | 100 | 0.017 | 0.223 | 0.014 | 0.400 | 0.420 | 0.008 | 0.570 | 0.015 |
| iforest | ml_vendor_amount_outlier | 19 | 0.004 | 0.316 | 0.003 | 0.421 | 0.421 | 0.002 | 0.789 | 0.003 |

- **Against the rules.** The rule and compliance detectors were run with predicted categories and pipeline enrichment.
  - They flag 30,320 rows and recall 0.576 of any-anomaly rows. They catch **0 of 160 ml_only rows**, by injector design.
  - The ML top-20 per company catches 74 ml_only rows and 126 any-anomaly rows that the rules miss.
  - The ML flagged set (548 rows, precision 0.221) catches 43 any-anomaly rows the rules miss, 21 of them ml_only.
- **Severity on test:**

  | band | rows | precision |
  |---|---|---|
  | HIGH | 177 | 0.350 |
  | MEDIUM | 22 | 0.136 |
  | LOW | 349 | 0.160 |

  The bands aren't monotone (see Not verified).
- **Inference:** **683.2 ms per 1k rows** on 10 test companies (5,102 rows), median of 3 runs. That covers features, the per-account fit and score, and flags; categorisation is not included.
- **Outputs:** `results/anomaly/anom1-pooled-company-8a28bbe2-a77238/` (gitignored).

### Files

- **Added:**
  - `ml/features/transaction_features.py`
  - `ml/anomaly/{__init__,data,model,train,evaluate,infer,metrics}.py`, `ml/anomaly/config.yaml`, `ml/anomaly/README.md`
  - `models/anomaly/anom1-pooled-company-8a28bbe2-a77238/metadata.json`
  - `docs/ml/anomaly.md`, `docs/adr/0005-anomaly-separate-flag-stream.md`
  - `tests/test_transaction_features.py`, `tests/test_ml_anomaly.py`
- **Modified:**
  - `pipeline/{run,versioning}.py`
  - `config/{settings,thresholds}.py`, `config/thresholds.yaml`
  - `ml/categoriser/evaluate.py` (section-scoped thresholds writer)
  - `db/repositories/results.py`
  - `reports/workbench_lens.py`
  - `demo/{pipeline_runner.py,components/tabs_risk.py}`
  - `Makefile`, `.github/workflows/ci.yml`
  - tests: `tests/test_thresholds.py`, `tests/test_run.py`, `tests/platform/{test_api,test_jobs_worker}.py`
- **Removed:** none

### Existing tests consciously updated

- `test_run.py::test_result_shape`, `platform/test_jobs_worker.py::test_gold_pdf_end_to_end` and `platform/test_api.py::test_dq_flags_summary_executions` pinned the stage list, the execution count (14) and `kind == "rule"`.
  - They now expect `ml_anomaly` right after `risk`, and `kind ∈ {rule, ml}`, only when the stage ran. With no stage, all flags must be `rule`.
  - They pass with the model and with `ANOMALY_ML_ENABLED=false`.
- `test_thresholds.py`: pins the `anomaly` section and adds a section-scoped writer test.

### Verified (run in this session, Windows 11, Python 3.12.2, scikit-learn 1.9.1, pyarrow 26.0.0)

| Check | Before (cc1b990) | After |
|---|---|---|
| corpus / splits rebuild | n/a | `8a28bbe2…f3d1` / `a905e9f1…9efe` (match the pins) |
| `pytest tests/ -q --ignore=tests/regression` | 1172 passed, 5 skipped (Phase 4 entry) | **1204 passed, 5 skipped** (production categoriser + anomaly model configured) |
| `pytest tests/regression` | 7 passed, 1 xfailed, 1 xpassed | 7 passed, 1 xfailed, 1 xpassed (with and without the anomaly model) |
| Risk recall / Compliance recall | 0.8846 (n=13) / 0.9 (n=10) | 0.8846 / 0.9 (unchanged) |
| CI simulation (v1-small LR categoriser + 50-tree anomaly model via env) | n/a | anomaly/run/platform/versioning tests 117 passed, 5 skipped; regression 7 passed, 1 xfailed, 1 xpassed |

- **`gold_regression.json` diff.**
  - **Production models.** The baseline was saved on this branch before any code change. The file is **identical except for `timestamp`**, both with the anomaly model and with `ANOMALY_ML_ENABLED=false`.
  - **CI's v1-small categoriser.** The regression gives risk_recall 0.85, identical with and without the anomaly stage, so it comes from the Phase 4 CI categoriser, not from Phase 5.
- **End to end** (`run_analysis` on `hdfc_healthy_saas_5d2a8b.pdf`):
  - Stages include `ml_anomaly` after `risk`.
  - 291 rows scored per-account, 2 ML flags (HIGH, LOW) with "investigate" wording.
  - Rule flags (1) and composite score (5.0) are unchanged; `tests/test_ml_anomaly.py` asserts equality against the no-model run.
- **Static checks:** `ruff check .` clean. `mypy --follow-imports=silent schema/ pipeline/ config/ llm/ db/ api/ worker/ datasets/ ml/`: `Success: no issues found in 66 source files`.
- **Security (manual; `/security-review` can't run here):**
  - New log lines carry counts, versions, timings and exception types only.
  - Flag descriptions contain no amounts or names.
  - `joblib.load` is reached only via `registry.load`, after the sha256 check.
  - The model version comes from operator config or env.
  - No findings.

### Not verified / known gaps

- **CI passed** in run 41483702499 on 7598042 (Ubuntu, Python 3.11, Postgres 16). Every step was green: lint, gated and informational mypy, gold-set build, small corpus + splits, CI categoriser training, **CI anomaly model training (v1-small)**, tests, Postgres tests (not skipped), regression. Job logs need auth, so CI test counts were not read.
- **Severity bands aren't monotone on test.** MEDIUM is a 22-row slice with precision 0.136, below LOW at 0.160. Only HIGH separates clearly (0.350). HIGH is a validation fallback (0.999).
- **Gain over the single-feature baseline is modest overall** (P@10 0.143 vs 0.133). It is large for first-time large counterparties and vendor amount outliers. The baseline is better on compliance rows and off-hours bursts.
- **The pooled fallback is much weaker** (all-pooled P@10 0.077) and scored only 2 test companies. Real short statements would rely on it.
- **Per-account forests are transductive** (fitted on the whole statement). The features are causal; the model isn't fitted on history only.
- **Latency:** 683 ms per 1k rows, roughly 6× the categoriser. Fewer trees weren't evaluated.
- **Narration counterparty keys split vendors.** There are 82 derived keys per company against 45 true counterparties, which inflates `cp_first_seen`.
- **Training state.** The model was trained from an uncommitted working tree on base cc1b990 (`git_dirty: true`). `make anomaly-*` targets weren't run through make (only the underlying commands).

## Phase 6: Churn model & company segmentation (`upgrade/p6-churn-segmentation`, 2026-11-02)

This phase adds a supervised customer-churn model, exposed as `CustomerAnalyticsReport.churn_risk` for active customers, and unsupervised company segmentation in the Network Lens (segment, nearest cohort peers, 2-D PCA).

- **What doesn't change:** rule churn events, the Phase 5 `ml_anomaly` stage and the gold regression. Without the models, the output is byte-identical to before.
- **Caveat:** all metrics are on **synthetic data** (corpus v1, generator 2.0.0). Churn is generated by the corpus's latent-health model, so a good churn score means the model recovered our simulator. See `docs/ml/churn.md` and `docs/ml/segmentation.md`.
- **`AGENT_VERSIONS`:** `customer_analytics` 1.0.0 → 1.1.0; new `ml_churn` 1.0.0 and `segmentation` 1.0.0.
- **Branch base:** new_improvements was fast-forwarded from cc1b990 to 34f59a8 (Phase 5) and pushed, with approval, before branching.

### Decisions (made in plan mode)

- **Customer identity:** units and labels use the Phase 5 narration counterparty key on predicted-REVENUE, non-aggregator credits. They don't use `CustomerIdentityResolver` ids or `labels/true_customer_id` (ADR-0006).
  - On corpus narrations the resolver keeps reference tokens. Its completeness is 0.195 against the generator's customers, and 95% of its ids have one row, so almost no unit has ≥ 2 payments.
  - The key has completeness 0.871 and purity 0.998.
  - The resolver is untouched, so gold `customer_id_accuracy` stays 0.6662.
  - Labels are used for **evaluation only**: the identity audit, an oracle-identity row, label agreement, and profile slices. Training never opens `labels/`, and a test guards `pd.read_parquet` for this.
- **Revenue rows:** Phase 4 categoriser **predictions** (`cat3-svc_cal-company-8a28bbe2-191ad9`, LLM off). Never `true_category`.
- **Segmentation features:** pipeline outputs (a doc with predicted categories, plus `FinancialAnalyst`), computed by the same function at train time and in the Network Lens.
- **Small validation set:** `churn_temporal` val has 82 company-cutoffs (73 with units; 1,247 active units from 37 companies).
  - All choices come from 5-fold GroupKFold out-of-fold predictions on train (442 companies): model (from a coarse 4-config grid), Platt calibration, threshold (max F1), medium band, `min_history`.
  - Val is a pre-registered check: switch to LR only if the paired bootstrap CI of the PR-AUC difference is entirely below 0.
  - Every val and test metric carries a 95% company-cluster bootstrap CI.
- **Change from §12.1:** "profile one-hot" was replaced by two observable company-context features. `profile` is a label table column.

### What changed

- **Features (new):**
  - `ml/features/churn_features.py`: payment key, 13 features computed on rows ≤ cutoff only, and the label for (cutoff, cutoff+90d].
  - `ml/features/company_features.py`: 13 company features.
- **`ml/churn/` (new):** `data, model, train, evaluate, infer, metrics, identity_audit`, `config.yaml`, `README.md`.
  - Reuses `ml/registry.py`, the hash gate `ml.categoriser.data.check_gate`, the predicted-category cache `ml.anomaly.data.predict_categories`, and `config.thresholds.update_section`.
- **`ml/segmentation/` (new):** `data, model, train, evaluate, infer`, `config.yaml`, `README.md`.
  - K-Means k = 2..10, compared with GMM and agglomerative Ward.
  - Hennig bootstrap Jaccard stability.
  - Clusters are named only if stable.
- **Customer analytics:**
  - `CustomerAnalyticsAnalyst(churn_scorer=None)`; new `ChurnRisk` type.
  - Every rule metric is computed exactly as before; churn risk is added afterwards.
  - A scorer failure is logged and leaves `churn_risk=None`.
  - The field is marked `omit_if_none`, and `db/serialize.to_jsonable` honours that, so the no-model payload is unchanged.
- **Pipeline (`pipeline/run.py`):**
  - Lazy `resolve_churn_scorer()`. Without sklearn, a model, or the env flag, it returns None.
  - No new stage. The `customer_analytics` stage context gains `churn_model_version` and `churn_scored` only when the model ran.
  - `summarise_result` adds `churn_risk_count` and `churn_model_version` only when the model ran.
- **Network Lens:**
  - `CompanySummary.segment_features` and `.segment` (`SegmentDetail`) are filled only when a segmentation model resolves.
  - With segments, the lens adds a "Segments" sheet, a PDF page (PCA scatter plus a table) and a demo table with the plot. Without them, nothing is added.
- **Config:**
  - `thresholds.yaml → churn` (model_version, decision_threshold, band_medium, min_history_payments, active_window_days, rule_silence_days) and `→ segmentation` (model_version, n_peers, stable_jaccard).
  - Matching dataclasses are pinned in `tests/test_thresholds.py`.
  - Settings `CHURN_/SEGMENTATION_MODEL_VERSION` and `*_ML_ENABLED`.
- **Infra:**
  - Makefile `churn-train/eval`, `segmentation-train/eval`.
  - CI trains a small churn LR and a segmentation model on v1-small after the anomaly step and exports both versions.
  - ADR-0006.
  - No Alembic change: no new output kind or column.

### Data

```
python -m datasets.build_corpus --n-companies 1000 --seed 42   -> corpus_hash 8a28bbe2c231577f9f8f05c21e6a180ce25d26a40bf224c007db0dde5fc4f3d1
python -m datasets.splits data/corpus/v1 --seed 42             -> splits_hash a905e9f115aae4f98f311806d6701e41d5527056164fa2c6a03372dfdc6d9efe
```

Both match the pins.

### Churn training: `python -m ml.churn.train data/corpus/v1 --n-jobs 3`

| split | units | active | companies | company-cutoffs with units | base rate all / active |
|---|---|---|---|---|---|
| train | 74,200 | 57,882 | 442 | 3,239 | 0.3256 / 0.1749 |
| val | 1,592 | 1,247 | 41 | 73 | 0.2971 / 0.1355 |
| test | 5,835 | 4,133 | 34 | 205 | 0.3813 / 0.1861 |

| candidate (grouped CV, OOF) | PR-AUC active | PR-AUC all | ROC-AUC active | seconds |
|---|---|---|---|---|
| lr_c0.1 | 0.5064 | 0.8333 | 0.8291 | 1.1 |
| lr_c1 | 0.5058 | 0.8336 | 0.8290 | 0.8 |
| hgb_d3 | 0.5341 | 0.8513 | 0.8397 | 13.9 |
| **hgb_l15** | **0.5352** | 0.8513 | 0.8379 | 18.4 |

- **Val check** (1,247 active units, 37 companies):
  - hgb_l15 PR-AUC 0.560 [0.442, 0.665] against lr_c0.1 0.524 [0.409, 0.640].
  - Paired difference +0.035 [−0.008, 0.084], so no switch to LR.
- **`min_history` buckets** (OOF active AP, model vs days-since rule): [2,3) 0.653 vs 0.582; [3,4) 0.580 vs 0.478; [4,6) 0.427 vs 0.311; [6,∞) 0.282 vs 0.220. So `min_history_payments` = 2.
- **Selected:** threshold 0.275 (OOF F1 0.562), band_medium 0.1749, `rule_silence_days` 60 (fixed in config, not tuned). Version `churn1-hgb_l15-8a28bbe2-2023be`.

### Churn evaluation: `python -m ml.churn.evaluate data/corpus/v1 --model-version churn1-hgb_l15-8a28bbe2-2023be --write-thresholds` (test, once)

The dry run went first: `evaluate data/corpus/v1-small --allow-unpinned --dry-run` with the v1-small CI model ran end to end. Its latency unit was mislabelled (ms per unit, not per 1k); that was fixed before the test run.

Active units (4,133, 33 companies, base rate 0.186; 95% company-bootstrap CIs, B=1000):

| scorer | PR-AUC | ROC-AUC | P | R | F1 | P@top-10% stmt | P@top-10% global |
|---|---|---|---|---|---|---|---|
| model (thr 0.275) | **0.557** [0.504, 0.626] | 0.851 | 0.526 | 0.715 | 0.606 [0.570, 0.644] | 0.562 | 0.645 |
| days since last payment | 0.353 [0.312, 0.410] | 0.686 | | | | 0.408 | 0.498 |
| rule `churn_events` (existing) | | | n/a | 0.000 | 0.000 | | |
| rule regularity alert (existing) | | | 0.209 | 0.042 | 0.069 | | |
| rule silence > 60 d (fallback) | | | 0.503 | 0.282 | 0.362 | | |

All units (5,835, 34 companies, base rate 0.381):

| scorer | PR-AUC | ROC-AUC | P | R | F1 |
|---|---|---|---|---|---|
| model | 0.876 [0.845, 0.910] | 0.916 | 0.730 | 0.902 | 0.807 [0.777, 0.841] |
| days since last payment | 0.824 [0.789, 0.867] | 0.860 | | | |
| rule `churn_events` (existing) | | | 0.855 | 0.648 | 0.737 [0.699, 0.778] |
| rule silence > 60 d | | | 0.784 | 0.752 | 0.768 |

- **Calibration:** Brier (active) 0.107 calibrated vs 0.116 raw.
- **By profile** (active units):

  | profile | units | base rate | model PR-AUC | days-since PR-AUC |
  |---|---|---|---|---|
  | burning_startup | 666 | 0.221 | 0.704 | 0.496 |
  | healthy_saas | 3,108 | 0.188 | 0.542 | 0.337 |
  | services_firm | 237 | 0.156 | 0.299 | 0.227 |
  | restaurant | 122 | 0.000 | n/a | n/a |
  | ecommerce | 0 | n/a | n/a | n/a |

  Restaurant and ecommerce have no customer-level churn in the generator.
- **Identity (evaluation only):**
  - Label agreement between key-built and true-id-built labels is 0.840 (5,783 units).
  - Oracle `true_customer_id` units (active, base rate 0.075) score PR-AUC 0.503 and ROC-AUC 0.852 with the same frozen model.
- **Latency:** 401.2 ms per 1k units (features plus scoring); `ChurnScorer.score` takes 42.8 ms per statement (median, 10 statements × 3 runs).

### Segmentation: `python -m ml.segmentation.train data/corpus/v1` and `evaluate ... --model-version seg1-kmeans-k3-8a28bbe2-4ff229 --write-thresholds`

| K-Means k (fit, 800) | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 |
|---|---|---|---|---|---|---|---|---|---|
| silhouette | 0.372 | **0.468** | 0.396 | 0.382 | 0.403 | 0.415 | 0.402 | 0.400 | 0.401 |
| mean Jaccard (B=100) | 0.897 | 1.000 | 0.996 | 0.881 | 0.905 | 0.854 | 0.948 | 0.938 | 0.906 |
| inertia | 7541.3 | 5245.3 | 4006.7 | 3437.2 | 2923.2 | 2505.1 | 2143.5 | 1932.6 | 1802.4 |

- **Candidates** (silhouette / mean Jaccard): kmeans@3 0.468 / 1.000; agglomerative@3 0.466 / 0.996; gmm@3 0.275 / 0.637; gmm@8 (best BIC) 0.339 / 0.752.
- **Final:** kmeans k=3. Every cluster has Jaccard 1.000, so all three are named.

| metric | fit | holdout (200) |
|---|---|---|
| silhouette | 0.468 | 0.454 |
| ARI vs hidden profile | 0.373 | **0.403** |
| ARI vs bank | 0.001 | 0.000 |

- **Holdout transfer ARI:** 1.000.
- **Segments:**
  - 0 = all saas, startup and services companies (700);
  - 1 = all restaurants (141);
  - 2 = all ecommerce (159).

  They separate revenue models, not the three B2B profiles.
- **ARI by k** (diagnostic, not used for selection): K-Means k=5 would reach 0.626. The label-free rule chose k=3 and was not changed.
- **Latency:** 2.63 ms per company assignment.

### Files

- **Added:**
  - `ml/churn/{__init__,data,model,train,evaluate,infer,metrics,identity_audit}.py`, `config.yaml`, `README.md`
  - `ml/segmentation/{__init__,data,model,train,evaluate,infer}.py`, `config.yaml`, `README.md`
  - `ml/features/{churn_features,company_features}.py`
  - `docs/ml/{churn,segmentation}.md`, `docs/adr/0006-churn-identity-key.md`
  - `models/{churn,segmentation}/<version>/metadata.json` (production models only)
  - `tests/{test_churn_features,test_ml_churn,test_ml_segmentation}.py`
- **Modified:**
  - `analysis/customer_analytics.py`, `pipeline/{run,versioning}.py`
  - `config/{thresholds.py,thresholds.yaml,settings.py}`, `db/serialize.py`
  - `reports/network_lens.py`, `demo/views/network.py`
  - `Makefile`, `.github/workflows/ci.yml`
  - tests: `test_thresholds.py`, `test_network_lens.py`, `test_ml_anomaly.py`
- **Removed:** none

### Existing tests consciously updated

- `test_network_lens.py::test_generates_valid_xlsx_and_pdf` pinned five sheet names. It now expects "Segments" appended exactly when segments were assigned.
- `test_ml_anomaly.py::test_pipeline_ml_stage_changes_nothing_else`: its "on" run points `MODELS_DIR` at tmp_path, so the production churn model resolved only in its "off" run, and the summaries differed by the churn keys. Churn is now disabled in both runs, so the test still compares anomaly on vs off only.
- `test_thresholds.py` pins the `churn` and `segmentation` sections.
- Stage-list and execution-count tests (`test_run.py`, `platform/test_jobs_worker.py`, `platform/test_api.py`) are **unchanged**: no stage was added.

### Verified (run in this session, Windows 11, Python 3.12.2, scikit-learn 1.9.1, pyarrow 26.0.0)

| Check | Result |
|---|---|
| `pytest tests/ -q --ignore=tests/regression` (production categoriser, anomaly, churn, segmentation models) | **1227 passed, 5 skipped** (after the two test updates below; a first run before them: 2 failed = the anomaly parity test and the vc_lens flake) |
| same with `CHURN_ML_ENABLED=false SEGMENTATION_ML_ENABLED=false` | **1227 passed, 5 skipped** (a first run before the fix failed `test_ml_churn::test_resolve_paths`, which inherited `CHURN_ML_ENABLED=false`; it now sets the flag itself) |
| `pytest tests/regression` with / without Phase 6 models | 7 passed, 1 xfailed, 1 xpassed (both) |
| risk_recall / compliance_recall / customer_id_accuracy / categorisation_accuracy | 0.8846 / 0.9 / 0.6662 / 0.9986 (unchanged) |
| `gold_regression.json` vs the baseline saved on this branch before any edit | **only `/timestamp` differs**, with and without the models |
| End to end, `run_analysis` on `hdfc_healthy_saas_5d2a8b.pdf` | Without models, summary + `customer_analytics` payload + stage list are **byte-identical** to the pre-change code (run on a stash of the edits). With models, the only differences are `customer_analytics.churn_risk` (34 active customers, all `source=model`; bands 33 low, 1 medium) and the summary keys `churn_risk_count` / `churn_model_version`. Stages: 14 in both. |
| CI simulation (v1-small: LR categoriser → anomaly → churn LR → segmentation k=2, via env) | affected tests 304 passed, 5 skipped; regression 7 passed, 1 xfailed, 1 xpassed, identical with and without the Phase 6 models (only `timestamp` differs) |
| `ruff check .` | clean |
| `mypy --follow-imports=silent schema/ pipeline/ config/ llm/ db/ api/ worker/ datasets/ ml/` | Success: no issues found in 82 source files |
| informational mypy on `analysis/customer_analytics.py reports/network_lens.py` | 11 errors before and after; all pre-existing, none new |

**Security (manual; `/security-review` can't run here):**
- New log lines carry versions, counts and exception type names only. No customer names, narrations or amounts.
- `joblib.load` is reached only via `registry.load`, after the sha256 check.
- Model versions come from operator config or env.
- The Network Lens shows company names the user uploaded; no other new user-facing data.

### Not verified / known gaps

- **CI, first run (41501636352 on ae8a51f): failed at "Type check (gated)".**
  - Cause: CI's Python 3.11 resolves mypy 1.20.2 and numpy 2.4.6, while this machine has mypy 2.4.1 and numpy 2.5.4. The older stubs flag `ml/churn/model.py::signed_log1p` returning Any.
  - Reproduced with those exact versions in a temporary venv, then fixed with `np.asarray(..., dtype=float)`. Values are unchanged: the production model's test active PR-AUC recomputes to 0.556757, as recorded.
  - Gated mypy is clean under both version sets, and the Phase 6 tests pass in the CI-like venv (178 passed).
  - Lesson: the local gated mypy run doesn't stand in for CI's dependency resolution.
- **CI passed** in run 41505134327 on dc43e81 (Ubuntu, Python 3.11, Postgres 16). Every step was green:
  - lint, gated mypy, informational mypy;
  - gold-set build, small corpus and splits;
  - CI categoriser, CI anomaly, and **CI churn + segmentation model training (v1-small)**;
  - tests, Postgres tests (not skipped), regression.

  Job logs need auth, so CI test counts were not read.
- **Identity noise:**
  - Key splits make some "churn" a narration variant going quiet: label agreement 0.840, oracle base rate 0.075 against 0.186.
  - Two identity notions coexist: rule analytics use resolver ids, `churn_risk` uses narration keys (ADR-0006).
  - Unifying them needs a separate gold-regression review.
- **Small evaluation sets.** Val has 37 companies with active units and test has 33–34, so the CIs are wide. The val check couldn't tell HGB from LR: its CI includes 0.
- **The rule comparison is structurally lopsided.** The existing rule needs 3 months of silence, so it never fires on active customers. On all units the model beats it (F1 0.807 vs 0.737), and the gain over days-since-last alone is smaller (PR-AUC 0.876 vs 0.824).
- **Services firms are weak** (PR-AUC 0.299): lumpy billing.
- **Segments track revenue model**, not the three B2B profiles (ARI 0.403). Template cluster names lean on rail shares; the centroids are the better description.
- **The CI categoriser regression shows risk_recall 0.8846.** The Phase 5 entry recorded 0.85 for the CI categoriser. The value is identical with and without the Phase 6 models, so it isn't caused by this phase; not investigated further.
- **Pre-existing flake:** `test_vc_lens::test_generate_is_idempotent` failed once in the first full run (xlsx size differs by 4 bytes: openpyxl timestamp).
- **Training state.** Models were trained from an uncommitted working tree on base 34f59a8 (`git_dirty: true`). `make churn-*` / `segmentation-*` targets weren't run through make (only the underlying commands).
