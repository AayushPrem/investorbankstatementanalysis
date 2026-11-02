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
