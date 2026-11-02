# Corpus card: synthetic bank-statement corpus v1

> **Everything here is SYNTHETIC.** It comes from this repo's generator (`tools/synthetic_gen.py` in corpus mode, `tools/synthetic_corpus.py`). Any metric trained or evaluated on it must be reported as "on synthetic data (generator v2.0.0, corpus v1)". It says nothing about real-world performance.

All numbers below come from one build run on 2026-11-02 (Windows 11, Python 3.12.2, pyarrow 26.0.0, DuckDB 2.0.0):

```
python -m datasets.build_corpus --n-companies 1000 --seed 42      # run twice, same hash
python -m datasets.splits data/corpus/v1 --seed 42                # run twice, same hash
python -m datasets.duckdb_stats data/corpus/v1 --bench
```

## Identity

| | |
|---|---|
| Corpus version | `v1` |
| Generator version | `2.0.0` (`synthetic_gen.GENERATOR_VERSION`) |
| Seed / companies / max months | 42 / 1,000 / 24 |
| `corpus_hash` | `e086b173e6422d3e5c4d02ce27f543d653e50fc09d7445286f520efef8e5cf30` |
| `splits_hash` | `a905e9f115aae4f98f311806d6701e41d5527056164fa2c6a03372dfdc6d9efe` |
| Reproducibility | Two builds with 4 workers and one with 1 worker gave the same `corpus_hash`; two split runs gave the same `splits_hash`. |
| Build time | 26.8 s with 4 workers, 67.0 s with 1 worker |
| Size on disk | features 28 MB, labels 15 MB, PDFs 2.3 MB |

The hash is reproducible for a fixed pyarrow version (recorded in `manifest.json`), because a different pyarrow can write different Parquet bytes.

## Layout

| Path | Rows | Contents |
|---|---|---|
| `features/transactions.parquet` | 617,483 | Statement rows: `txn_id, company_id, account_id, bank, date, txn_time, value_date, description, ref_no, debit, credit, balance, row_seq` |
| `features/companies.parquet` | 1,000 | Header facts: bank, account, holder name, period, opening balance |
| `labels/transactions.parquet` | 617,483 | `true_category, template_id, anomaly_type, anomaly_kind, anomaly_flags, true_customer_id, counterparty_key, is_related_party` |
| `labels/customers.parquet` | 23,233 | Latent `health`, `acquired_month`, `churn_month`, `churn_month_start`, `signaller`, `lead_months`, payment count, last payment date |
| `labels/companies.parquet` | 1,000 | `profile`, seed, `n_months`, `revenue_scale`, `n_customers`, injected rule and ml_only flags |
| `labels/flags.parquet` | 1,710 | One row per injected flag, with its triggering txn IDs |
| `pdf/*.pdf` + `labels/pdf/*.truth.json` | 30 | Rendered statements for ingestion checks |
| `splits/*.parquet` | n/a | See [Splits](#splits) |

**Label isolation.** Feature and label tables share only the join keys `txn_id`, `company_id` and `bank`. No feature column name contains a label token (`category`, `customer`, `template`, `anomaly`, `profile`, `health`, `churn`, …). `datasets.duckdb_stats.connect()` exposes the feature views only; label views have to be asked for and are prefixed `label_`. Narrations contain no category, flag or label word. `txn_id`s are opaque random hex. Every row has a `txn_time`, so time of day isn't a marker of the injected rows. `tests/test_corpus.py` asserts all of this.

**Ingestion check.** All 30 rendered PDFs went through `pipeline.run.run_analysis` (digital-PDF adapter → validator → …). 30/30 passed validation, and the row counts matched the feature table.

## Companies

| profile | hdfc | icici | rows | rows/company |
|---|---|---|---|---|
| burning_startup | 112 | 105 | 125,503 | 578.4 |
| ecommerce | 64 | 95 | 68,064 | 428.1 |
| healthy_saas | 146 | 139 | 238,781 | 837.8 |
| restaurant | 74 | 67 | 134,476 | 953.7 |
| services_firm | 92 | 106 | 50,659 | 255.9 |

- Profile mix: 0.30 / 0.20 / 0.20 / 0.15 / 0.15 (saas / startup / services / ecommerce / restaurant). Banks are 50/50 HDFC/ICICI.
- Statement length is uniform on 12–24 months (median 18). Start months are uniform on 2022-04 to 2024-12, so dates run from 2022-04-01 to 2026-11-30.
- `revenue_scale ~ LogNormal(0, 0.5)`, observed range 0.24–5.50 (median 1.03). It multiplies every amount and preserves each amount's original roundness (₹1,000 / ₹100 / ₹1 multiples).
- B2B customer count is log-uniform from 5 up to 200 (saas), 80 (startup) and 40 (services). Observed range 5–196, median 20. Customer names come from a shared universe of 1,800 combinatorial names, so names recur across companies.
- Company name, founder, account number, branch, vendor names (from 12 vendor-type pools) and employee names are drawn per company.

## Class balance (`true_category`)

| true_category | rows | % | hdfc | icici |
|---|---|---|---|---|
| REVENUE | 340,553 | 55.15 | 172,971 | 167,582 |
| SALARY | 157,192 | 25.46 | 78,847 | 78,345 |
| VENDOR_PAYMENT | 91,468 | 14.81 | 45,451 | 46,017 |
| TAX | 23,932 | 3.88 | 11,885 | 12,047 |
| FEES | 2,872 | 0.47 | 1,177 | 1,695 |
| OTHER | 706 | 0.11 | 314 | 392 |
| FOUNDER_WITHDRAWAL | 588 | 0.10 | 282 | 306 |
| TRANSFER | 98 | 0.02 | 44 | 54 |
| LOAN_IN | 74 | 0.01 | 31 | 43 |
| LOAN_OUT | 0 | 0 | 0 | 0 |

The classes are heavily imbalanced, and `LOAN_OUT` never occurs: no profile or realistic-mode injector generates it. Phase 4 must report macro and per-class metrics, and say that `LOAN_OUT` can't be evaluated.

## Narration diversity

- 95 templates (50 HDFC, 45 ICICI) across rails NEFT, RTGS, IMPS, UPI, ECS/ACH, cash, tax and internet banking, with random UTR/reference/VPA formats.
- Merchant-name variants (`PVT LTD` / `PRIVATE LIMITED` / `PVT. LTD.` / dropped suffix, `TECHNOLOGIES→TECH`, `&→AND`, first two words). Each company has a canonical form per party, and 15% of rows use another variant.
- About 5% of rows are title-cased and about 5% have spacing noise. Narrations are truncated to 60 characters (HDFC) or 80 (ICICI). Median length is 50 (HDFC) and 42 (ICICI).
- 590,384 distinct narrations across 617,483 rows; 5,812 distinct counterparty keys.
- 6.9% of rows are built inline in the original generator (restaurant Swiggy/Zomato UPI credits, legacy cash and FX injectors) and are **not** diversified (`template_id = <bank>:inline:<kind>`).
- Every row's `template_id` is in the labels, so evaluation can be grouped by template. This makes evaluation less of a template-matching exercise. **The narrations are still template-generated**, so performance on real statements is unknown.

## Anomalies

| kind | anomaly_type | rows | companies |
|---|---|---|---|
| rule | revenue_concentration | 630 | 210 |
| rule | founder_over_extraction | 588 | 196 |
| rule | related_party_leakage | 477 | 159 |
| rule | structuring | 405 | 81 |
| rule | round_tripping | 98 | 49 |
| compliance | compliance_gst_gap | 327 | 109 |
| compliance | compliance_pmla_cash | 303 | 101 |
| compliance | compliance_rpt_concentration | 134 | 134 |
| compliance | compliance_cash_loan | 74 | 74 |
| compliance | compliance_269st | 71 | 71 |
| ml_only | ml_off_hours_burst | 706 | 153 |
| ml_only | ml_first_time_large_counterparty | 208 | 143 |
| ml_only | ml_vendor_amount_outlier | 132 | 132 |

- 0.673% of rows carry any anomaly label, and 0.169% are `ml_only`.
- `compliance_tds_gap` removes rows, so it labels no row (it appears in `labels/flags.parquet`).
- **Rule and compliance injectors** are the original ones, with realistic-mode incidence per profile. The exception is `customer_churn`, which is **excluded** so that latent churn is the only churn source.
- In corpus mode, the constant counterparty names those injectors used (e.g. the concentration "whale", the round-trip advisory firm) are remapped per company, so a model can't memorise one name as "anomaly".
- **`ml_only` injectors** target patterns the six rule detectors in `analysis/risk.py` don't look for. Each one is picked with p = 0.15 per company:
  - `ml_vendor_amount_outlier`: one payment of 3–6× an existing vendor's median (the vendor has ≥ 4 prior payments), near its usual day of month. Selected for 140 companies, injected in 132.
  - `ml_first_time_large_counterparty`: 1–2 debits, totalling 1.5–3× the company's p95 debit, to a counterparty from a held-back name set, in the second half of the period. Selected for 174, injected in 143.
  - `ml_off_hours_burst`: 3–6 UPI/IMPS debits of ₹8k–₹60k to distinct new payees between 01:00 and 04:30 on one night. Selected for 153, injected in 153.
- **Guards**, built into all three injectors:
  - debits only, never cash
  - non-round amounts with paise, never a multiple of ₹1,000
  - new counterparties have no return flow, so round-tripping can't match
  - the spike_drain month set must be unchanged and the touched month's leave-one-out |z| must stay ≤ 2.0, below the detector's 2.5
  - If a guard can't be met in 10 tries, the anomaly isn't injected. That's why injected < selected, and the shortfall is mostly in ecommerce.
- `tests/test_corpus.py` runs the real detectors on generated companies and asserts that no ml_only row appears in any rule flag. It also asserts that removing the ml_only injections leaves the rest of the statement byte-for-byte the same.
- Organic night-time rows (00:00–06:00) are 0.84% of non-burst rows, so time of day alone doesn't give the burst away.

## Customer churn (latent health)

Applies to `healthy_saas`, `burning_startup` and `services_firm`. Ecommerce and restaurant revenue is aggregated (Razorpay settlements / daily sales), so those profiles have no per-customer churn labels.

- `health_i ~ N(0, 1)`. 60% of customers are active at the start, with 1–24 months of pre-period tenure. The other 40% are acquired at a uniform month inside the period.
- Monthly hazard from the month after acquisition: `λ = sigmoid(a_p − 1.0·health − 0.35·ln(1 + tenure_months) + ε)`, `ε ~ N(0, 0.5²)`, with `a_p` = −3.0 (saas), −2.6 (services), −2.2 (startup). Churn is absorbing: from `churn_month` on, the customer makes no payment.
- Precursors are noisy and partial:
  - 55% of churners are "signallers", with a lead window of 1–3 months before churn. In that window they get +0.35 late-payment probability, a 0.5/month downgrade probability (×0.70–0.95) and +0.25 skip probability.
  - Everyone, churner or not, has a base late-payment probability of `p_profile·(1 + sigmoid(−health))` (p = 0.10 / 0.20 / 0.08), a base downgrade probability of `0.03 + 0.03·sigmoid(−health)`, and a base skip probability of 0.03 (saas), 0.35 (services, lumpy billing) or 0.05 (startup).

| profile | customers | churned | % churned in period | % churners that signal | avg months |
|---|---|---|---|---|---|
| burning_startup | 5,791 | 2,869 | 49.5 | 54.9 | 18.0 |
| healthy_saas | 14,180 | 4,300 | 30.3 | 56.1 | 17.9 |
| services_firm | 3,262 | 1,282 | 39.3 | 55.5 | 17.8 |

| latent health | customers | % churned |
|---|---|---|
| < −1 | 3,682 | 74.2 |
| −1 … 0 | 7,957 | 44.8 |
| 0 … 1 | 7,886 | 23.0 |
| ≥ 1 | 3,708 | 9.3 |

How visible the precursors are in **observable** rows (labels are used only to form the groups):

| payments | n | % gap since previous payment > 45 days | % amount drop > 5% |
|---|---|---|---|
| churners, last 90 days before churn | 14,173 | 27.1 | 29.0 |
| churners, earlier | 21,076 | 16.5 | 20.0 |
| non-churners | 168,398 | 17.2 | 22.1 |

The signal exists but is weak. That's intended, so a churn model has to do better than a single rule. Labels never appear in narrations.

## Splits

From `python -m datasets.splits data/corpus/v1 --seed 42`:

| split file | unit | train | val | test |
|---|---|---|---|---|
| `company.parquet` (categoriser / anomaly / churn) | company | 700 | 150 | 150 |
| `categoriser_cross_bank.parquet` | company | 414 (HDFC) | 74 (HDFC) | 512 (ICICI) |
| `categoriser_unseen_vendor.parquet` | counterparty key | 4,666 | 382 | 764 |
| `churn_temporal.parquet` | (company, cutoff) | 3,610 | 82 | 232 |
| `segmentation_holdout.parquet` | company | 800 fit | — | 200 holdout |

- **Churn temporal:**
  - A cutoff is a month end with ≥ 6 months of history and 90 days after it inside the statement. T1 = 2024-10-31 and T2 = 2025-04-30 (60th/80th percentiles of eligible cutoffs).
  - Train label windows close by T1. Val cutoffs are after T1 with windows closing by T2. Test cutoffs are after T2.
  - Companies come from the shared partition. Val is small (82 units), because a val company needs a statement that spans T1…T2.
- **Unseen vendor:** generic keys (taxes, cash, aggregators, self transfers, employees) are always train. Test counterparties never occur in train, in any company.

## Known biases and limitations

- **Synthetic and template-generated.** Grammar, amounts and seasonality come from hand-written rules. Real narration noise (OCR errors, bank-specific abbreviations, multi-line wraps) is only approximated.
- Category priors are inherited from five hand-designed profiles. REVENUE is 55% of rows, LOAN_OUT is absent, and TRANSFER and LOAN_IN are tiny.
- The rule categoriser in `analysis/categoriser.py` was partly written against the original generator's narrations (D10). Narration diversity reduces, but doesn't remove, that circularity.
- Churn is **generated from the very latent model the card describes.** A churn model that does well here has recovered our simulator, not real customer behaviour.
- Statement dates are synthetic and run past "today" (up to 2026-11-30).
- The PDF subset has no time column, so `ml_off_hours_burst` can't be seen from PDFs.
- Aggregated-revenue profiles (ecommerce, restaurant) have no customer-level churn.
- Parquet bytes, and so `corpus_hash`, depend on the pyarrow version (recorded in the manifest).

## Storage benchmark (this machine)

The same aggregation (feature–label join on `txn_id`, then a group-by of company × month × category over 617,483 rows), best of 3:

| engine | seconds |
|---|---|
| DuckDB 2.0.0 (Parquet scan) | 0.52 |
| pandas (read_parquet + merge + groupby) | 1.05 |
