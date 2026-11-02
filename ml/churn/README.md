# ml/churn: customer churn model (Phase 6)

> Trained and evaluated on **SYNTHETIC data only** (corpus v1). Doing well means recovering our own simulator. See `docs/ml/churn.md` and `docs/ml/corpus_card.md`.

**Label:** customer makes no payment in (cutoff, cutoff+90d] given ≥2 payments before cutoff.

## Commands

```
make corpus
python -m ml.churn.train    data/corpus/v1                     # grouped CV on train -> registry
python -m ml.churn.evaluate data/corpus/v1 --write-thresholds  # test once; writes thresholds.yaml
python -m ml.churn.identity_audit data/corpus/v1               # resolver vs narration key (evaluation only)
```

- Both commands refuse to run unless the corpus and split hashes match `config.yaml → pinned_corpora`.
- CI: `python -m ml.churn.train data/corpus/v1-small --profile ci --allow-unpinned` prints `CHURN_MODEL_VERSION=…`, which CI exports.
- A dry run (`evaluate … --dry-run`) runs the whole evaluation on any corpus and writes to `results/churn/dryrun-<corpus>/`.

## Files

| file | role |
|---|---|
| `config.yaml` | pinned hashes, candidate grid, CV, selection rules, CI profile |
| `data.py` | payments from `features/` + predicted categories, units per split (never reads `labels/`) |
| `model.py` | `ChurnModel` (estimator + Platt calibrator), estimator factory |
| `train.py` | grouped-CV selection on train, pre-registered val check, registration |
| `evaluate.py` | test metrics with company-bootstrap CIs, rule comparison via the real analyst, profile slices, identity oracle, calibration and PR plots, latency |
| `metrics.py` | PR-AUC, P/R/F1, precision@top, bootstrap CIs, calibration table |
| `identity_audit.py` | evaluation-only identity quality |
| `infer.py` | `ChurnScorer`, `resolve_model()` / `resolve_scorer()` (used by `pipeline/run.py`) |
| `../features/churn_features.py` | payment key, features ≤ cutoff, label |

## Rules this package follows

- **Inputs.** Statement rows plus **predicted** categories, and the narration counterparty key. Never `true_category` or `true_customer_id` for training.
- **No look-ahead.** Features only see rows ≤ cutoff (`tests/test_churn_features.py`).
- **Selection.** Every choice is made on train by GroupKFold out-of-fold predictions. Val is a pre-registered check, and test runs once.
- **Parallelism.** One process pool (feature building); estimators are single-process.
- **Pipeline.** No model means no `churn_risk`, and nothing else changes. Rule `churn_events` are never touched.
