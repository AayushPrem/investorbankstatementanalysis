# Model card: hybrid transaction categoriser (categoriser 3.0.0)

> **Every number here is on SYNTHETIC data** (generator v2.0.0, corpus v1, `corpus_hash 8a28bbe2…f3d1`, `splits_hash a905e9f1…`). It is template-generated text. Performance on real bank statements is **unknown**. All tables were produced in one session on 2026-11-02 by the commands below (Windows 11, Python 3.12.2, scikit-learn 1.9.1, LightGBM 4.7.0). Full outputs: `results/categoriser/cat3-svc_cal-company-8a28bbe2-191ad9/`.

```
python -m datasets.build_corpus --n-companies 1000 --seed 42      # corpus_hash 8a28bbe2…f3d1 ✓
python -m datasets.splits data/corpus/v1 --seed 42                # splits_hash a905e9f1…    ✓
python -m ml.categoriser.rule_audit data/corpus/v1
python -m ml.categoriser.train data/corpus/v1 --split {company,cross_bank,unseen_vendor} --n-jobs 3|4
python -m ml.categoriser.evaluate data/corpus/v1 --write-thresholds
```

## What it is

`analysis/categoriser.py` v3 routes each transaction:

```
1. high-precision rule (audited subset of the v2 rules)   → source=rule
2. model p_max ≥ τ_accept                                  → source=ml
3. LLM configured: LLM picks among the model's top-3       → source=llm  (answer ∉ top-3 → review)
4. no LLM / LLM failed: p_max ≥ τ_review → ml (degraded accept); else → review (best guess kept)
no model artifact → the v2 rule list (with catch-alls), unchanged
```

| | |
|---|---|
| Production model | `cat3-svc_cal-company-8a28bbe2-191ad9` (3.1 MB joblib, not committed; `models/categoriser/<ver>/metadata.json` is) |
| Algorithm | `CalibratedClassifierCV(LinearSVC(C=0.1, class_weight="balanced"), method="sigmoid", cv=3)` |
| Features (`ml/features/categoriser.py`) | char_wb 3–5 TF-IDF ∪ word 1–2 TF-IDF on the normalised narration (lowercase; UTR/refs, VPAs, IFSC, masked account numbers replaced by tokens), rail one-hot (from the narration), day-of-month bucket, standardised log1p(amount), is_credit, round-₹1000 / round-₹100 / has-paise flags. 17,621 columns after `min_df=2`. |
| Not used | bank, balance, ref_no, txn_time (not available from PDFs), company_id, any label column |
| Training data | company split: 431,417 train / 92,849 val / 93,217 test rows (700/150/150 companies) |
| Routing thresholds | τ_accept = 0.90, τ_review = 0.50 (policy floor; see [τ](#how-τ-was-chosen)); high-precision rules `[0, 1, 4, 5, 8, 9, 10]` |
| Code state | trained from a working tree based on `0f59c80` with uncommitted Phase 4 code. `metadata.json` records `git_sha=0f59c80`; later runs also record `git_dirty`. |

## Rule precision audit (D10)

Company-split train + val. A rule is "high precision" if it is ≥ 0.99 precise with ≥ 50 first-match rows, isn't a generic fallback, and isn't concentrated on ≤ 2 generator templates. Test precision is shown, but wasn't used for the choice.

| id | rule | category | first-match n | precision | standalone precision | templates | top template share | flags | decision | test precision |
|---|---|---|---|---|---|---|---|---|---|---|
| 0 | salary_keyword | SALARY | 89365 | 1.0000 | 1.0000 | 6 | 0.1780 |  | high_precision | 1.0000 |
| 1 | tax_keyword | TAX | 19616 | 1.0000 | 1.0000 | 16 | 0.1295 |  | high_precision | 1.0000 |
| 2 | founder_withdrawal_keyword | FOUNDER_WITHDRAWAL | 0 | n/a | n/a | 0 | n/a |  | low_support | n/a |
| 3 | personal_suffix | FOUNDER_WITHDRAWAL | 81 | 1.0000 | 1.0000 | 1 | 1.0000 | known_generator_fitted, template_concentrated | flagged_generator_fit | 1.0000 |
| 4 | bank_fees_keyword | FEES | 1239 | 1.0000 | 1.0000 | 9 | 0.1396 |  | high_precision | 1.0000 |
| 5 | loan_in_keyword | LOAN_IN | 61 | 1.0000 | 1.0000 | 4 | 0.3279 |  | high_precision | 1.0000 |
| 6 | loan_out_emi | LOAN_OUT | 0 | n/a | n/a | 0 | n/a |  | low_support | n/a |
| 7 | self_transfer | TRANSFER | 91 | **0.0000** | 0.0000 | 1 | 1.0000 | template_concentrated | below_precision | 0.0000 |
| 8 | revenue_invoice_credit | REVENUE | 40708 | 0.9998 | 0.9998 | 3 | 0.4162 |  | high_precision | 1.0000 |
| 9 | revenue_deposit_credit | REVENUE | 21965 | 1.0000 | 0.9979 | 7 | 0.2041 |  | high_precision | 1.0000 |
| 10 | gateway_debit_fee | FEES | 1389 | 1.0000 | 1.0000 | 10 | 0.1562 |  | high_precision | 1.0000 |
| 11 | rail_debit_vendor | VENDOR_PAYMENT | 92091 | **0.6689** | 0.4159 | 28 | 0.1700 |  | excluded_by_construction | 0.6707 |
| 12 | catchall_credit_revenue | REVENUE | 227874 | 0.9998 | 0.9996 | 24 | 0.0750 |  | excluded_by_construction | 0.9999 |
| 13 | catchall_debit_vendor | VENDOR_PAYMENT | 29786 | **0.5163** | 0.3294 | 7 | 0.4643 |  | excluded_by_construction | 0.5899 |

Findings:
- `rail_debit_vendor` and `catchall_debit_vendor` label 29,520 and 13,829 SALARY rows as VENDOR_PAYMENT. Those are salary narrations without the word SALARY, e.g. `IMPS-…-NAME-…-SAL`. This is where v2 loses most of its accuracy on the corpus.
- `self_transfer` is wrong on all 91 of its rows: in corpus v1 "SELF TRANSFER" narrations are labelled FOUNDER_WITHDRAWAL. The legacy (gold-set) generator labels the same narration TRANSFER (see [gold set](#gold-set-regression)). This is a generator labelling inconsistency, to fix in a later corpus version.
- `personal_suffix` (`\bPERSONAL\b$`) is precise, but it fires on a single template and its code comment says it was written for the generator, so it was dropped.

## Why the 99.9% was circular, and the honest number

The gold-set categorisation figure (0.9991) comes from the generator the v2 rules were written against. On the corpus's diversified narrations, the same v2 rules score **accuracy 0.9172 and macro-F1 0.6648** on the company test split.

## Test results (τ frozen from validation; each split evaluated once)

| split | variant | n | accuracy | macro-P | macro-R | macro-F1 |
|---|---|---|---|---|---|---|
| company | rules_v2 | 93217 | 0.9172 | 0.7392 | 0.6561 | 0.6648 |
| company | ml_only | 93217 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| company | hybrid_no_llm | 93217 | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| cross_bank | rules_v2 | 306481 | 0.9335 | 0.7438 | 0.6725 | 0.6846 |
| cross_bank | ml_only | 306481 | 0.9999 | 0.9999 | 0.9936 | 0.9967 |
| cross_bank | hybrid_no_llm | 306481 | 1.0000 | 1.0000 | 0.9835 | 0.9911 |
| unseen_vendor | rules_v2 | 58925 | 0.9950 | 0.6639 | 0.5153 | 0.5266 |
| unseen_vendor | ml_only | 58925 | 0.9999 | 1.0000 | 0.9583 | 0.9762 |
| unseen_vendor | hybrid_no_llm | 58925 | 0.9999 | 1.0000 | 0.9417 | 0.9646 |

- Macro averages are over the classes that have test rows. **LOAN_OUT has 0 rows in the corpus and can't be evaluated**. The model never sees it and can't predict it.
- The unseen-vendor val/test sets contain only named counterparties. SALARY, TAX and FEES rows have generic counterparty keys, which are always in train, so those classes have no test support on that split.
- Hybrid is slightly below ML-only on cross-bank and unseen-vendor. The whole gap is TRANSFER recall: high-precision rule 8 (`INV-` reference on a credit → REVENUE) catches round-trip TRANSFER credits that the model would have got right. TRANSFER recall: cross-bank 0.94 ML-only vs 0.85 hybrid; unseen-vendor 0.75 vs 0.65, on 54 and 20 test rows. A 0.9998-precise rule still costs recall on a class this rare.

### Per-class test F1 (rules_v2 → hybrid)

| class | company n | rules_v2 | hybrid | cross_bank n | rules_v2 | hybrid | unseen_vendor n | rules_v2 | hybrid |
|---|---|---|---|---|---|---|---|---|---|
| REVENUE | 50053 | 1.0000 | 1.0000 | 167582 | 0.9999 | 1.0000 | 42022 | 0.9999 | 1.0000 |
| VENDOR_PAYMENT | 14491 | 0.7899 | 1.0000 | 46017 | 0.8196 | 1.0000 | 16581 | 0.9919 | 0.9999 |
| SALARY | 24478 | 0.8212 | 1.0000 | 78345 | 0.8594 | 1.0000 | 0 | n/a | n/a |
| LOAN_IN | 13 | 1.0000 | 1.0000 | 43 | 1.0000 | 1.0000 | 17 | 1.0000 | 1.0000 |
| LOAN_OUT | 0 | n/a | n/a | 0 | n/a | n/a | 0 | n/a | n/a |
| TAX | 3747 | 0.9832 | 1.0000 | 12047 | 0.9826 | 1.0000 | 0 | n/a | n/a |
| TRANSFER | 4 | 0.0000 | 1.0000 | 54 | 0.0000 | 0.9200 | 20 | 0.0000 | 0.7879 |
| FOUNDER_WITHDRAWAL | 87 | 0.3889 | 1.0000 | 306 | 0.5000 | 1.0000 | 120 | 0.1679 | 1.0000 |
| FEES | 244 | 1.0000 | 1.0000 | 1695 | 1.0000 | 1.0000 | 0 | n/a | n/a |
| OTHER | 100 | 0.0000 | 1.0000 | 392 | 0.0000 | 1.0000 | 165 | 0.0000 | 1.0000 |

Rare classes have tiny supports (TRANSFER: 4 / 54 / 20 test rows), so their F1 values are noisy. Confusion matrices: `confusion_<split>.{png,csv}` (hybrid) and `confusion_rules_v2_<split>.{png,csv}`.

### Slices

| split | slice | hybrid acc | hybrid macro-F1 | rules_v2 acc | rules_v2 macro-F1 |
|---|---|---|---|---|---|
| company | inline templates (n=6043) | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| company | diversified templates (n=87174) | 1.0000 | 1.0000 | 0.9114 | 0.6648 |
| cross_bank | inline templates (n=20404) | 1.0000 | 1.0000 | 1.0000 | 1.0000 |
| cross_bank | diversified templates (n=286077) | 1.0000 | 0.9911 | 0.9288 | 0.6846 |
| unseen_vendor | diversified templates (n=58925) | 0.9999 | 0.9646 | 0.9950 | 0.5266 |

The 6.9% of corpus rows built inline by the original generator (restaurant Swiggy/Zomato credits, legacy cash and FX injectors) aren't diversified. Both rules and model are perfect on them, so they carry no signal about generalisation.

## Model comparison (chosen on validation macro-F1; test shown, never used to choose)

| split | candidate | val macro-F1 | test macro-F1 | val acc | test acc | chosen |
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

- **LightGBM is not kept.** It doesn't beat calibrated SVC on cross-bank or unseen-vendor validation. The guide's rule says "wins on the cross-bank split"; validation is used here so the test set never drives a choice.
- **MiniLM is not kept.** LR + all-MiniLM-L6-v2 embeddings of the normalised narration is worse on unseen-vendor validation than LR alone (0.9429 vs 0.9698).
- **Grid edges.** LR picked C=10 inside a {1, 3, 10, 30, 100} grid. Cross-bank SVC picked C=0.3, the top of its {0.1, 0.3} grid. C=1 was dropped after a ConvergenceWarning on the first run. Validation macro-F1 is dominated by classes with < 60 rows, so differences in the third decimal are within noise.

## How τ was chosen

On company-split **validation**, over rows no high-precision rule catches:
- **τ_accept** = the lowest τ on the 0.30–0.99 grid with ML-accepted precision ≥ 0.97.
- **τ_review** = the lowest τ whose band [τ_review, τ_accept) has precision ≥ 0.90.

Result: **τ_accept = τ_review = 0.30**, the grid floor. Precision is ≥ 0.97 at every τ, so the synthetic corpus can't calibrate τ. Validation top-label ECE is 0.0001.

Production uses **max(validation τ, policy floor 0.90 / 0.50)**. That's a policy choice, not a fitted value, because τ = 0.30 would be too permissive for real narrations. Test routing at both settings, no LLM configured:

| split | τ set | τ_accept | τ_review | % rule | % ml | % ml (0.5–0.9 band) | % review | precision auto-accepted | coverage | true ∈ top-3 (band) | LLM rows /1k | LLM calls /1k |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| company | val | 0.30 | 0.30 | 34.71 | 65.29 | 0.000 | 0.000 | 1.0000 | 1.0000 | n/a | 0.00 | 0.00 |
| company | policy | 0.90 | 0.50 | 34.71 | 65.28 | 0.008 | 0.000 | 1.0000 | 0.9999 | 1.0000 | 0.08 | 0.08 |
| cross_bank | val | 0.30 | 0.30 | 35.06 | 64.94 | 0.000 | 0.000 | 1.0000 | 1.0000 | n/a | 0.00 | 0.00 |
| cross_bank | policy | 0.90 | 0.50 | 35.06 | 64.92 | 0.017 | 0.000 | 1.0000 | 0.9998 | 1.0000 | 0.17 | 0.14 |
| unseen_vendor | val | 0.30 | 0.30 | 15.38 | 84.62 | 0.000 | 0.000 | 0.9999 | 1.0000 | n/a | 0.00 | 0.00 |
| unseen_vendor | policy | 0.90 | 0.50 | 15.38 | 84.60 | 0.025 | 0.000 | 0.9999 | 0.9997 | 1.0000 | 0.25 | 0.22 |

"LLM rows/calls per 1k" are what *would* go to the LLM if `ANTHROPIC_API_KEY` were set: rows below τ_accept, batched ≤ 50 per statement. No LLM was called in this session. Before Phase 4 the v2 catch-alls meant the LLM was never called either, so the saving isn't "fewer LLM calls than v2". It is that a rare uncertain row now gets an LLM decision constrained to the model's top-3, instead of a direction default. Precision–coverage curves: `precision_coverage_<split>.{png,csv}`.

## Latency

Production model on a 1,000-row document, CPU, single process, median of 5, feature building included:

| path | ms per 1k rows |
|---|---|
| v3 `Categoriser.categorise` | 112.0 |
| model predict only | 108.8 |
| v2 rules only | 27.8 |

## Gold-set regression

`pytest tests/regression -s`, before (branch base 0f59c80) and after (v3, this model, policy τ, no LLM):

| metric | before | after | delta |
|---|---|---|---|
| extraction_accuracy | 1.0 | 1.0 | 0 |
| categorisation_accuracy | 0.9991 | 0.9986 | −0.0005 |
| risk_recall | 0.8846 | 0.8846 | 0 |
| compliance_recall | 0.9 | 0.9 | 0 |
| customer_id_accuracy | 0.6662 | 0.6662 | 0 |

Per-row inspection of the 4,371 matched rows: v2 4,367 correct, v3 4,365.
- **+2:** round-trip debits to "ZENITH CAPITAL ADVISORY". Truth TRANSFER; v2 said VENDOR_PAYMENT, v3 says TRANSFER (p 0.61 / 0.64).
- **−4:** `NEFT … SELF TRANSFER XXXX3425` in `icici_services_firm_80e19b`. Truth TRANSFER. v3 says FOUNDER_WITHDRAWAL (debits, p 0.99) or REVENUE (credits, p 0.56–0.57).

The SELF TRANSFER rows are the corpus/gold label inconsistency above. Re-including rule 7 would probably recover those 4 rows (not measured), but it would fit a rule to the gold-set generator against the corpus evidence (0/91): the D10 circularity. It stays excluded (decision recorded in the CHANGELOG). The fix belongs in the generator and a new corpus version.

## Limitations

- **Synthetic, template-generated text:** near-perfect scores mean the model has learned the generator's narration grammar. On real statements (OCR noise, unseen banks and formats), expect lower accuracy and far more rows below τ.
- **τ can't be calibrated on this corpus.** Re-tune it on a labelled real sample before any real use.
- LOAN_OUT is absent. TRANSFER, LOAN_IN, OTHER and FOUNDER_WITHDRAWAL have < 0.15% of rows each.
- The model is a pickle tied to scikit-learn 1.9.1 (ADR-0004). Load only from the local registry.
- The CI model is a separate LR (C=1) trained on `v1-small`. CI numbers aren't comparable to this card.
