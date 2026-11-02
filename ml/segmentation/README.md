# ml/segmentation: company segmentation (Phase 6)

> Fitted and evaluated on **SYNTHETIC data only** (corpus v1). See `docs/ml/segmentation.md`.

## Commands

```
make corpus
python -m ml.segmentation.train    data/corpus/v1                     # label-free selection on fit companies
python -m ml.segmentation.evaluate data/corpus/v1 --write-thresholds  # holdout once; ARI vs hidden profile
```

CI: `--profile ci --allow-unpinned` on v1-small (k = 2..4, 10 bootstraps) prints `SEGMENTATION_MODEL_VERSION=…`.

## Files

| file | role |
|---|---|
| `config.yaml` | pinned hashes, k range, bootstrap count, stability threshold, the pre-registered selection rule |
| `data.py` | company features from pipeline outputs (predicted categories + `FinancialAnalyst`), cached |
| `model.py` | `SegmentationModel`, candidate fitting, Hennig bootstrap Jaccard, cluster naming |
| `train.py` | K-Means k sweep, GMM / agglomerative comparison, final choice, PCA, registration |
| `evaluate.py` | holdout silhouette, ARI vs profile and vs bank, crosstabs, centroids, PCA plot, latency |
| `infer.py` | `assign()` (segment + cohort peers + PCA position), `resolve_model()` |
| `../features/company_features.py` | the feature function shared with the Network Lens |

## Rules this package follows

- **Labels.** `labels/companies.parquet:profile` is read only by `evaluate.py`. ARI never drives a choice.
- **Naming.** A cluster is named only if its bootstrap Jaccard is ≥ `stable_jaccard` (0.75).
- **No model.** The Network Lens output has no Segments sheet or page.
