# Model card: transaction anomaly detector (Phase 5)

> **It detects synthetic injections.** Every number here comes from `python -m ml.anomaly.train` / `evaluate` on **synthetic corpus v1** (`corpus_hash 8a28bbe2…f3d1`, `splits_hash a905e9f1…9efe`). The positives are anomalies that our own generator injected (`docs/ml/corpus_card.md`). That says nothing about how well it finds real-world irregularities. An ML flag means "investigate"; it is not a finding of fraud or wrongdoing.

| | |
|---|---|
| Model version | `anom1-pooled-company-8a28bbe2-a77238` (artifact 710 KB, gitignored; `metadata.json` committed) |
| Algorithm | scikit-learn `IsolationForest` (200 trees, max_samples 256, seed 42, n_jobs 1) |
| Category input | `cat3-svc_cal-company-8a28bbe2-191ad9` predictions, LLM off. Labels are never used. |
| Counterparty | Derived from the narration (`counterparty_from_narration`). Never `labels/counterparty_key`. |
| Fit data | Pooled forest: 431,417 rows from 700 **train** companies, 11.2 s |
| Selection data | 92,849 rows from 150 **validation** companies |
| Test data | 93,217 rows from 150 **test** companies (company split). Evaluated once. |
| Pipeline stage | `ml_anomaly`, after `risk`. `AGENT_VERSIONS["ml_anomaly"] = "1.0.0"` |

## How it works

- **Per-account first.** An account with at least `min_history` = 150 rows gets its own IsolationForest, fitted at scoring time on that account's rows. This is transductive over the statement being analysed.
- **Pooled fallback.** Shorter accounts are scored by the pooled forest. It was fitted on train companies using a standardised feature view, where amounts and velocities are account-relative and causal. Each flag records which scorer produced it (`evidence.scorer`).
- **Percentiles, not raw scores.** Scores from different forests aren't comparable, so each score becomes a percentile against its scorer's reference distribution: the account's own scores, or the pooled training scores.
  - A row is flagged when its percentile is ≥ 1 − contamination, with at most 20 flags per statement.
  - Severity comes from percentile cutoffs.
- **Explanations.** The top 3 features are ranked by |x − reference median| / IQR on the scorer's reference data. SHAP isn't used; it isn't a dependency.
- **Separate from the rules.** ML flags never enter `RiskReport.flags`, the composite score or the narrative (ADR-0005). They are persisted with `kind='ml'`, and only the workbench lens and the demo risk tab show them.

### Features (`ml/features/transaction_features.py`, FEATURE_VERSION 1)

| feature | definition (rows at or before this one in `(date, row_seq)` order; "prior" = strictly earlier) |
|---|---|
| `log_amount` | log1p(amount) |
| `is_credit` | direction. Added beyond §11, so credits and debits don't share one amount scale. |
| `cp_robust_z` | (log amount − median of prior same-counterparty amounts) / max(IQR/1.349, 0.10). 0 with fewer than 3 priors. |
| `cat_robust_z` | the same, against prior rows with the same predicted category and direction |
| `cp_first_seen` | 1 if the counterparty has no prior row |
| `cp_share_mtd_out` | counterparty's month-to-date debits / all month-to-date debits, including this row |
| `velocity_1d`, `velocity_7d` | rows on this date / in the last 7 days, up to and including this row |
| `dow`, `month_end` | day of week; within the last 3 days of the month |
| `days_since_cp` | days since the previous row with this counterparty (400 if first seen) |
| pooled view only | `acct_amount_z` (robust z against prior same-direction rows) and `velocity_*_rel` (window count / expanding mean rows per active day) replace `log_amount` and `velocity_*` |

**No look-ahead.**
- **How it's built.** One pass per account in `(date, row_seq)` order. Each row reads the accumulated state (sorted prior values, last-seen dates, a 7-day window, month-to-date sums) before it is added. There are no group transforms over the whole frame.
- **How it's tested** (`tests/test_transaction_features.py`):
  - prefix invariance (`features(rows[:k]) == features(all)[:k]`, exact, at six cut points)
  - appending future rows (a huge new payee and a burst) leaves every earlier row unchanged
  - a later same-day row can't change an earlier one
  - accounts are independent
  - corpus and serving paths give identical frames
  - label columns are ignored

**`txn_time` is not a feature.** `CanonicalTransaction` and PDF statements have no time column, and the model has to see the same features in training and serving. As a result, `ml_off_hours_burst` can only be caught through its burst and new-payee traits (velocity, first-seen, days since). **The off-hours property itself can't be detected from PDF input.**

## Choices made on validation (`python -m ml.anomaly.train data/corpus/v1`)

**min_history**, chosen by micro P@20 on any-anomaly rows:

| min_history | accounts pooled | rows pooled | micro P@20 | AP |
|---|---|---|---|---|
| **150** | 2 | 258 | **0.0887** | 0.0991 |
| 250 | 16 | 3,335 | 0.0817 | 0.0929 |
| 400 | 51 | 14,850 | 0.0747 | 0.0817 |

**Contamination**, chosen by F1 of the flagged set against any-anomaly rows, with at most 20 flags per statement:

| contamination | flagged | TP | precision | recall | F1 |
|---|---|---|---|---|---|
| 0.002 | 258 | 62 | 0.2403 | 0.0963 | 0.1375 |
| **0.005** | 547 | 107 | 0.1956 | 0.1661 | **0.1797** |
| 0.01 | 1,009 | 148 | 0.1467 | 0.2298 | 0.1791 |
| 0.02 | 1,753 | 193 | 0.1101 | 0.2997 | 0.1610 |
| 0.05 | 2,769 | 255 | 0.0921 | 0.3960 | 0.1494 |

0.005 and 0.01 are within 0.0006 F1 of each other.

**Severity.** The rule: the lowest percentile cutoff (from 0.995 up) whose val flagged-row precision reaches the target.
- **First targets, 0.30 / 0.15.** The whole flagged set already reached 0.196 on val, so MEDIUM covered every flag and LOW was empty.
- **Raised to 0.40 / 0.25.** This was done on val only, before test was scored. It is recorded in `ml/anomaly/config.yaml`.
- **Result:**
  - MEDIUM ≥ 0.9986 (val precision 0.251).
  - HIGH: no cutoff reaches 0.40 (the highest on val is 0.304 at 0.9996), so it **falls back to the default 0.999** (val precision 0.277). Recorded as `severity_fallback.high = true`.

All of these are written to `config/thresholds.yaml → anomaly` by `evaluate.py --write-thresholds`.

## Test results (`python -m ml.anomaly.evaluate data/corpus/v1 --write-thresholds`, run once)

- **Methods:**
  - `iforest`: the production hybrid (148 companies per-account, 2 pooled)
  - `zscore_baseline`: |robust z| of log amount against the account's prior same-direction rows
  - two ablations, all-pooled and all-per-account
  - expected random precision (= prevalence)
- **Reading the table.** "mac" is the mean over companies; recall skips companies without positives. "mic" is pooled (Σhits / Σk, Σhits / Σpositives). Test prevalence: any 0.695%, ml_only 0.172%.

| method | label | positives | P@10 mac | R@10 mac | R@10 mic | P@20 mac | R@20 mac | R@20 mic | P@50 mac | R@50 mac | R@50 mic | AP |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| iforest | any | 648 | **0.143** | 0.367 | 0.330 | 0.098 | 0.483 | 0.454 | 0.055 | 0.664 | 0.634 | 0.120 |
| zscore_baseline | any | 648 | 0.133 | 0.307 | 0.309 | 0.095 | 0.419 | 0.441 | 0.053 | 0.605 | 0.619 | 0.097 |
| iforest_all_pooled | any | 648 | 0.077 | 0.201 | 0.177 | 0.061 | 0.312 | 0.284 | 0.040 | 0.492 | 0.458 | 0.059 |
| iforest_all_per_account | any | 648 | 0.143 | 0.370 | 0.332 | 0.099 | 0.488 | 0.457 | 0.055 | 0.667 | 0.636 | 0.122 |
| random_expected | any | 648 | 0.007 | | | 0.007 | | | 0.007 | | | |
| iforest | rule | 342 | 0.061 | 0.281 | 0.266 | 0.045 | 0.411 | 0.395 | 0.025 | 0.567 | 0.550 | 0.037 |
| zscore_baseline | rule | 342 | 0.051 | 0.233 | 0.225 | 0.035 | 0.311 | 0.304 | 0.023 | 0.522 | 0.497 | 0.035 |
| iforest | compliance | 146 | 0.049 | 0.569 | 0.507 | 0.028 | 0.644 | 0.582 | 0.014 | 0.768 | 0.733 | 0.101 |
| zscore_baseline | compliance | 146 | 0.060 | 0.633 | 0.616 | 0.041 | 0.801 | 0.836 | 0.018 | 0.901 | 0.938 | 0.055 |
| iforest | ml_only | 160 | 0.033 | **0.416** | 0.306 | 0.025 | 0.536 | 0.463 | 0.015 | 0.771 | 0.725 | 0.022 |
| zscore_baseline | ml_only | 160 | 0.022 | 0.189 | 0.206 | 0.020 | 0.341 | 0.375 | 0.013 | 0.596 | 0.588 | 0.021 |
| iforest | ml_first_time_large_counterparty | 41 | 0.015 | **0.672** | 0.561 | 0.009 | 0.759 | 0.659 | 0.004 | 0.862 | 0.805 | 0.015 |
| zscore_baseline | ml_first_time_large_counterparty | 41 | 0.005 | 0.224 | 0.195 | 0.005 | 0.379 | 0.341 | 0.003 | 0.552 | 0.537 | 0.009 |
| iforest | ml_off_hours_burst | 100 | 0.013 | 0.183 | 0.200 | 0.013 | 0.352 | 0.390 | 0.009 | 0.668 | 0.680 | 0.010 |
| zscore_baseline | ml_off_hours_burst | 100 | 0.017 | 0.223 | 0.250 | 0.014 | 0.400 | 0.420 | 0.008 | 0.570 | 0.600 | 0.015 |
| iforest | ml_vendor_amount_outlier | 19 | 0.004 | 0.316 | 0.316 | 0.003 | 0.421 | 0.421 | 0.002 | 0.789 | 0.789 | 0.003 |
| zscore_baseline | ml_vendor_amount_outlier | 19 | 0.000 | 0.000 | 0.000 | 0.001 | 0.211 | 0.211 | 0.002 | 0.632 | 0.632 | 0.002 |

The pooled (micro) P@k equals macro P@k, because every company contributes exactly k rows. The full table, including the ablations for every label, is in `results/anomaly/anom1-pooled-company-8a28bbe2-a77238/report.md` and `at_k.csv`.

### Operating point vs the rule detectors (test, row level)

The rules were run with predicted categories and the pipeline's own enrichment, LLM off: Categoriser → RelatedPartyTagger → CustomerIdentityResolver → RiskAnalyst, plus the compliance rules. Their flags cover every triggering row (e.g. every row of a GST gap), so row-level precision is low by construction.

| label | set | flagged | TP | precision | recall |
|---|---|---|---|---|---|
| any | ML flagged (c=0.005, ≤20) | 548 | 121 | 0.221 | 0.187 |
| any | ML top-20 per company | 3,000 | 294 | 0.098 | 0.454 |
| any | z-score top-20 per company | 3,000 | 286 | 0.095 | 0.441 |
| any | rule detectors | 10,982 | 279 | 0.025 | 0.431 |
| any | compliance rules | 20,546 | 171 | 0.008 | 0.264 |
| any | rules ∪ compliance | 30,320 | 373 | 0.012 | 0.576 |
| ml_only | ML flagged | 548 | 21 | 0.038 | 0.131 |
| ml_only | ML top-20 | 3,000 | 74 | 0.025 | 0.463 |
| ml_only | rules ∪ compliance | 30,320 | 0 | 0.000 | 0.000 |

### What ML finds that the rules miss (test)

| label | ML set | positives | caught by rules | caught by ML | ML ∧ rules | **ML ∧ ¬rules** | rules ∧ ¬ML | neither |
|---|---|---|---|---|---|---|---|---|
| any | flagged | 648 | 373 | 121 | 78 | **43** | 295 | 232 |
| any | top-20 | 648 | 373 | 294 | 168 | **126** | 205 | 149 |
| rule | top-20 | 342 | 230 | 135 | 85 | **50** | 145 | 62 |
| compliance | top-20 | 146 | 143 | 85 | 83 | 2 | 60 | 1 |
| ml_only | flagged | 160 | 0 | 21 | 0 | **21** | 0 | 139 |
| ml_only | top-20 | 160 | 0 | 74 | 0 | **74** | 0 | 86 |
| ml_first_time_large_counterparty | top-20 | 41 | 0 | 27 | 0 | 27 | 0 | 14 |
| ml_off_hours_burst | top-20 | 100 | 0 | 39 | 0 | 39 | 0 | 61 |
| ml_vendor_amount_outlier | top-20 | 19 | 0 | 8 | 0 | 8 | 0 | 11 |

### Severity bands on test (flagged rows)

| severity | n | precision (any) | precision (ml_only) |
|---|---|---|---|
| HIGH (≥ 0.999) | 177 | 0.350 | 0.040 |
| MEDIUM (0.9986–0.999) | 22 | 0.136 | 0.000 |
| LOW | 349 | 0.160 | 0.040 |

**The bands aren't monotone on test.** MEDIUM is a thin slice, 22 rows, and its precision is below LOW. Only HIGH separates clearly. Treat MEDIUM and LOW as one band until the selection is reworked (e.g. a wider MEDIUM, or bands chosen on more validation data).

### Other measurements

- **Inference:** **683.2 ms per 1k rows**, measured on 10 test companies, 5,102 rows, median of 3 runs, single process. That covers features, the per-account forest fit and score, and flag building; categorisation is not included. The per-account fit dominates. Fewer trees would cut this, but wasn't evaluated.
- **Narration counterparty key vs label key** (test, diagnostic only):
  - purity 0.979, completeness 0.800
  - 82.1 derived keys per company vs 45.2 true counterparties
  - Narration variants of one vendor (truncated UPI names, "postman lim" vs "postman") split into several keys, which inflates `cp_first_seen`.

## Reading the results honestly

- **IsolationForest vs the baseline.** Overall it beats the single-feature z-score only modestly (P@10 0.143 vs 0.133; AP 0.120 vs 0.097). The gain is concentrated where the extra features matter:
  - first-time large counterparties: R@10 0.672 vs 0.224
  - vendor amount outliers: R@10 0.316 vs 0.000
  - ml_only overall: R@10 0.416 vs 0.189
- **Where the baseline wins:**
  - compliance rows: probably because many are large cash or loan amounts, which a pure amount z ranks well (not verified row by row)
  - off-hours bursts: the burst amounts are ₹8k–₹60k, so amount alone barely separates them, and the time of day that defines them isn't a feature
- **The rules find no ml_only row, by construction.** The injectors are guarded to evade them: |z| ≤ 2.0, non-round amounts, no cash.
  - The ML top-20 finds 74 of 160 ml_only rows.
  - It also finds 50 rule-type rows that the rules (run on predicted categories) missed.
- **The pooled fallback is much weaker.** All-pooled P@10 is 0.077 vs 0.143. With min_history 150 it scores only 2 of 150 test companies here. Real statements shorter than 150 rows (e.g. 3–6 months of a small company) would mostly use it.
- **Precision at the operating point.** On test, 0.221 of flagged rows are injected anomalies, so about 4 in 5 flags are clean rows that look unusual. That is why the wording is "investigate".

## Caveats and known limitations

- **Synthetic generator.**
  - 0.673% of corpus rows carry any anomaly label; 0.169% are ml_only.
  - Real anomaly rates, and what counts as "unusual", are unknown.
- **Missing labels.**
  - `compliance_tds_gap` removes rows, so it labels no row and can't be measured row-wise.
  - `revenue_concentration` and similar rule types label many ordinary-looking rows (e.g. all of a whale customer's payments). Those count as positives here, although no transaction-level outlier detector should find them.
- **Off-hours.** `ml_off_hours_burst` is only detectable through velocity and new payees. Time of day isn't a feature, and PDF input has no time.
- **Transductive per-account fitting.** The forest sees the whole statement being analysed, including rows after the one being scored. The *features* are causal; the *model* is fitted on the full statement, as an analyst reviewing a historical statement would. A streaming deployment would need forests fitted on history only.
- **Selection uses labelled validation companies.** Unsupervised training doesn't remove the need for labels to choose thresholds. Re-tune on real labelled cases before trusting the bands.
- **Models were trained from an uncommitted working tree** on base cc1b990 (`git_dirty: true` in the metadata).

## Reproduce

```
python -m datasets.build_corpus --n-companies 1000 --seed 42
python -m datasets.splits data/corpus/v1 --seed 42
make anomaly-train        # python -m ml.anomaly.train data/corpus/v1
make anomaly-eval         # python -m ml.anomaly.evaluate data/corpus/v1 --write-thresholds
```

Training needs the Phase 4 categoriser model (`make categoriser-train categoriser-eval`). Without it, predicted categories fall back to the v2 rules, and the cache and metadata record `v2-rules`.
