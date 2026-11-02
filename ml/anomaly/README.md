# ml/anomaly: unsupervised transaction anomaly detection (Phase 5)

> Trained and evaluated on **SYNTHETIC data only** (corpus v1). It detects synthetic injections. See the model card `docs/ml/anomaly.md` and `docs/ml/corpus_card.md`. Flags say "investigate", never "fraud".

## Commands

```
make corpus                                          # data/corpus/v1 (hash-gated)
python -m ml.anomaly.train    data/corpus/v1         # pooled forest + validation selection -> registry
python -m ml.anomaly.evaluate data/corpus/v1 --write-thresholds   # test once; writes thresholds.yaml
```

Both refuse to run unless `corpus_hash` and `splits_hash` match `config.yaml → pinned_corpora`.

**CI** builds its own small model:

```
python -m ml.anomaly.train data/corpus/v1-small --profile ci --allow-unpinned
```

It prints `ANOMALY_MODEL_VERSION=…`, which CI exports so the env value overrides `thresholds.yaml`.

## Files

| file | role |
|---|---|
| `config.yaml` | pinned hashes, forest params, validation grids and targets, CI profile, evaluation ks |
| `data.py` | corpus rows (`features/` only), predicted categories (Phase 4 categoriser, cached), labels for selection/evaluation, rule detectors on corpus companies |
| `model.py` | `AnomalyModel` (the registered object): per-account vs pooled scoring, percentiles, top-feature deviations, `flag_mask` |
| `train.py` | features (cached), pooled fit on train companies, selection of min_history / contamination / severity on val companies |
| `evaluate.py` | test metrics (P@k, R@k, AP by kind and type), z-score baseline, ablations, rule overlap, severity, latency → `results/anomaly/<model_version>/` |
| `metrics.py` | per-company and pooled @k, AP, set metrics |
| `infer.py` | `AnomalyDetector`, `AnomalyFlag`, `resolve_model()` / `resolve_detector()` (used by `pipeline/run.py`) |
| `../features/transaction_features.py` | causal per-transaction features, plus the narration counterparty key |

## Rules this package follows

- **Inputs.** Narration, amount, direction and date from `features/`, plus **predicted** categories. Never `true_category`, `counterparty_key`, `anomaly_*` or `txn_time`.
- **Labels.** Read only for validation selection (val companies) and the single test evaluation.
- **No look-ahead.** Every feature uses rows at or before the current one. `tests/test_transaction_features.py` proves it.
- **Parallelism.** One level only: a process pool over companies or accounts, with `n_jobs=1` inside each forest.
- **Pipeline.** No model means no `ml_anomaly` stage and no change to any output. ML flags never feed the risk score (ADR-0005).
