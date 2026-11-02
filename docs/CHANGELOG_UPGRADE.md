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

Task 6 (splitting `demo/app.py`) is **deferred** to a follow-up session on this branch. When it's done, the target is `demo/views/` + `demo/components/`, not `demo/pages/`, because Streamlit auto-discovers a `pages/` folder next to the entry script and would add sidebar navigation, which is a UI change.

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

### Files

- **Added:** `config/__init__.py`, `config/settings.py`, `config/thresholds.py`, `config/thresholds.yaml`, `config/logging.py`, `llm/__init__.py`, `llm/client.py`, `pipeline/run.py`, `.env.example`, `tests/test_thresholds.py`, `tests/test_llm_client.py`, `tests/test_logging_config.py`, `tests/test_run.py`
- **Modified:** `analysis/{categoriser,compliance,customer_analytics,financial_health_alerts,portfolio_narrative,reconciliation,report_narrative,risk}.py`, `jurisdictions/india/rules.py`, `pipeline/{batch,customer_identity,orchestrator,related_party}.py`, `reports/angel_lens.py`, `demo/pipeline_runner.py`, `demo/app.py` (only the `configure_logging()` call), `tests/conftest.py`, `pyproject.toml`, `Makefile`, `.github/workflows/ci.yml`, `docs/CHANGELOG_UPGRADE.md`
- **Removed:** none

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

### Not verified / known gaps

- **The real LLM path was not run.** No API key was used in this session. Its request shape is checked against a stubbed SDK client (`tests/test_llm_client.py`), not against the live API.
- **The `[embeddings]` extra was not installed in a fresh venv** (torch is large). The existing `.venv`, which has `sentence-transformers`, ran the full suite.
- **OCR on a real scanned PDF wasn't run.** The tesseract binary isn't checked; only adapter *selection* is tested.
- **Linux CI:** results are in the push report for this phase.
- **Task 6 (UI split) is pending.**
- `jurisdictions/india/rules.py` still defines `_LAKH`, which is now unused there.
- **Pre-existing flaky tests, found this session and not caused by Phase 1:**
  - `tests/test_vc_lens.py::TestEdgeCases::test_generate_is_idempotent` compares XLSX byte lengths. openpyxl writes wall-clock `created`/`modified` timestamps into `docProps/core.xml`, and that was confirmed to be the only differing part. Two calls in different seconds can change the compressed size (seen once: 12087 vs 12084). `reports/vc_lens.py` is not modified in this phase. Fix in a later phase by pinning workbook properties or comparing sheet contents; it isn't fixed here because pinning would change report output.
  - `tests/test_demo.py::TestRunIsolation::test_temp_files_cleaned_up` globs the *system* temp dir, so it can fail if another process creates `tmp*` entries during the run. It failed once while a second pytest was running alongside, and passed on rerun.
