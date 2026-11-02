# Baseline — 2026-11 (end of upgrade Phase 0)

This is the "before" snapshot for the upgrade described in `UPGRADE_GUIDE.md`. Every number on this page comes from a command run on 2026-11-02 in this repo. The real output is pasted below.

> **All accuracy numbers are on SYNTHETIC data** from this repo's own generator (`tools/synthetic_gen.py` at commit `74799d1` plus the Phase 0 changes). No real bank statement was used. Real-world performance is unknown.

## Environment

| | |
|---|---|
| OS | Windows 11 (10.0.26200) |
| Python | 3.12.2, fresh `.venv`, `pip install -e ".[dev]"` |
| Key packages | pytest 9.2.0, ruff 0.17.3, mypy 2.4.1, streamlit 1.66.1, reportlab 5.1.0, pdfplumber 0.11.10, plotly 7.1.0 |
| Base commit | `74799d1` (`sprint-4-prototype`), branch `upgrade/p0-baseline` |

The Linux CI run has **not** been observed yet. It runs when the branch is pushed.

## Gold set (rebuilt)

The original 22-statement gold set was never committed (`data/gold/**` is gitignored), and no record of its composition survived. It was therefore **rebuilt** from an explicit manifest, `GOLD_MANIFEST` in `scripts/build_gold_set.py`. That composition was fixed *before* any regression run and has not been tuned since.

- 22 statements, 6 months each, `risk_mode="manual"`, fixed seeds 1001–1022.
- Every profile appears with both HDFC and ICICI.
  - healthy_saas ×5, burning_startup ×5, services_firm ×4, ecommerce ×4, restaurant ×4.
- Every injector in `_FLAG_INJECTORS` is used at least once. There are 6 clean statements.
- The two filenames that tests reference are pinned: `hdfc_healthy_saas_5d2a8b`, `icici_burning_startup_4eb7a0`.
- **Deterministic:** three independent builds produced byte-identical files (44 files: 22 PDFs + 22 truth JSONs). One of those builds used a different `PYTHONHASHSEED`.

**Consequence:** these regression numbers are **not comparable** to the README/FEATURES.md figures (risk recall 73.3%, customer-ID 82.3%, run 2026-07-26). Those came from a different, lost 22-statement composition.

## Test suite (`tests/`, excluding regression)

```
$ .venv/Scripts/python -m pytest -q --ignore=tests/regression
909 passed, 2 warnings in 192.71s (0:03:12)
```

0 failures, 0 errors, 0 skips. The 2 warnings are pytest deprecation notices: class-scoped fixtures defined as instance methods, in `test_batch.py` and `test_versioning.py`.

**Intermediate run in this session:** the run after the gold-set and D3 fixes, but before `plotly` was declared, gave `6 failed, 903 passed`. All 6 were `tests/test_demo_ui.py` failing with `No module named 'plotly'`. `demo/app.py` imports plotly, but it was never declared as a dependency, and a fresh venv exposed that.

The defect counts in UPGRADE_GUIDE §2.4 (D2: 17 errors + 8 failures; D3: 9 failures) were **not** re-measured here. No venv existed at the start of this session, so the pre-fix state was never run.

## Regression suite (`tests/regression/`)

```
$ PYTHONUTF8=1 .venv/Scripts/python -m pytest tests/regression -q -rxX -s
────────────────────────────────────────────────────────────
  Gold-Set Regression — 22 statements
  Extraction:         100.0%  (target: 88%)
  Categorisation:     99.9%  (target: 88%)
  Risk recall:        88.5%  (target: 85%, n=13)
  Compliance recall:  90.0%  (target: 90%, n=10)
  Customer ID acc.:   66.6%  (target: 90%, pairs=176521)
  Reconciliation:     NOT MEASURED — Not measured — no ground-truth 'declared claims' fixtures exist in the gold set yet (no CompanyClaims paired with a known intentional mismatch). This is an open item, not a computed 0%.
  Report saved:       results\sprint1\gold_regression.json
────────────────────────────────────────────────────────────
XFAIL tests/regression/test_gold_set.py::test_customer_id_accuracy - D12 (UPGRADE_GUIDE.md §2.4): known below-target metric, tracked for a later phase. Target is unchanged; this becomes XPASS once fixed.
XPASS tests/regression/test_gold_set.py::test_risk_recall - D12 (UPGRADE_GUIDE.md §2.4): known below-target metric, tracked for a later phase. Target is unchanged; this becomes XPASS once fixed.
7 passed, 1 xfailed, 1 xpassed in 61.93s (0:01:01)
```

Without `-s`, the result is the same: `7 passed, 1 xfailed, 1 xpassed`. `PYTHONUTF8=1` is only needed on Windows when `-s` output is redirected to a file, because otherwise the console encoding (cp1252) can't print the `─` characters.

### Reading these numbers

- **Customer-ID 66.6% comes entirely from the `restaurant` profile.** The four restaurant statements score 25.4%, 16.8%, 0.0% and 0.0%; every other statement scores 100%. Restaurant statements contribute about 16.5k pairs each, so they dominate the pairwise metric. This matches the weakness the README already notes for anonymous-retail narrations.
- **Risk recall 88.5% (n=13)** is above target on this composition. It still sits under the D12 xfail marker, so a regression would show as XFAIL rather than a hard failure. The marker should be removed once this has been stable across phases.
  - Misses: `hdfc_burning_startup_0e0f3f` (round_tripping, 0/1) and `icici_healthy_saas_a97649` (1 of 2 flags).
- **Compliance recall 90.0% (n=10)** is exactly at target. One miss: `hdfc_services_firm_29a49f`, compliance recall 0.0.
- n is small for both recall metrics. Treat them as smoke signals, not accuracy claims.

## Static checks

```
$ .venv/Scripts/python -m ruff check .
All checks passed!

$ .venv/Scripts/python -m mypy --follow-imports=silent schema/ pipeline/
Success: no issues found in 10 source files
```

Full-scope mypy is informational only and not gated. It was measured after the Phase 0 fixes:

```
$ .venv/Scripts/python -m mypy schema/ pipeline/ analysis/ adapters/ reports/ tools/ jurisdictions/
Found 219 errors in 19 files (checked 41 source files)
```

Per package, checking each on its own with imports followed. These counts include errors in imported modules, so they overlap:

| Package | Result |
|---|---|
| schema/ | clean |
| pipeline/ | 0 errors in its own files (gated with `--follow-imports=silent`). 158 when imports are followed. |
| adapters/ | 2 errors in 2 files |
| tools/ | 42 errors in 2 files |
| jurisdictions/ | 72 errors in 11 files |
| analysis/ | 91 errors in 12 files |
| reports/ | 161 errors in 15 files |
