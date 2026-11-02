# Model card: company segmentation (Phase 6)

> **Synthetic.** Every number here comes from `python -m ml.segmentation.train` / `evaluate`, run on 2026-11-02 on **synthetic corpus v1** (`corpus_hash 8a28bbe2…f3d1`, `splits_hash a905e9f1…9efe`). The hidden "profiles" it is checked against are the generator's five hand-designed company types. Segments are a grouping aid in the Network Lens, not a rating.

| | |
|---|---|
| Model version | `seg1-kmeans-k3-8a28bbe2-4ff229` (artifact gitignored; `metadata.json` committed) |
| Algorithm | K-Means, k = 3 (n_init 10, seed 42), on log-transformed and standardised features; PCA(2) for the plot |
| Data | `splits/segmentation_holdout.parquet`: 800 **fit** companies, 200 **holdout** companies (evaluated once) |
| Inputs | Pipeline outputs only: StatementDocument with predicted categories (`cat3-svc_cal-company-8a28bbe2-191ad9`, LLM off) plus `FinancialAnalyst`. No label column. |
| Serving | Network Lens: "Segments" sheet, PDF page with a PCA plot, and a demo table. All absent without a model. `AGENT_VERSIONS["segmentation"] = "1.0.0"` |

## Features (`ml/features/company_features.py`, FEATURE_VERSION 1)

- **Growth and burn:**
  - Average MoM revenue growth: clipped to ±10, then signed log1p.
  - Burn-to-revenue ratio: log1p, capped at 100. It is named honestly: a true burn multiple (net burn ÷ net new ARR) is unstable on monthly bank data.
  - Runway: months, capped at 60; profitable → 60; log1p.
- **Customers:**
  - Customer HHI and customer count (keys with ≥ 2 payments, log1p). Both use the narration key, for the identity reasons in `docs/ml/churn.md`.
  - CV of monthly revenue (log1p).
- **Founder withdrawals:** predicted FOUNDER_WITHDRAWAL share of debits.
- **Rail mix:** share of rows that are UPI / NEFT / RTGS / IMPS / cash.
- **Aggregator share of REVENUE.** This is an addition to §12.2; it is already a pipeline output.

The `StandardScaler` is fitted on fit companies only.

## Selection (fit set only; pre-registered in `ml/segmentation/config.yaml`; labels never read)

| k | inertia | silhouette | mean bootstrap Jaccard | min Jaccard |
|---|---|---|---|---|
| 2 | 7541.3 | 0.372 | 0.897 | 0.846 |
| **3** | 5245.3 | **0.468** | **1.000** | 1.000 |
| 4 | 4006.7 | 0.396 | 0.996 | 0.991 |
| 5 | 3437.2 | 0.382 | 0.881 | 0.733 |
| 6 | 2923.2 | 0.403 | 0.905 | 0.825 |
| 7 | 2505.1 | 0.415 | 0.854 | 0.553 |
| 8 | 2143.5 | 0.402 | 0.948 | 0.845 |
| 9 | 1932.6 | 0.400 | 0.938 | 0.834 |
| 10 | 1802.4 | 0.401 | 0.906 | 0.728 |

- **k\* = 3.** It has the highest silhouette among k with mean Jaccard ≥ 0.75. The elbow is soft: inertia falls by 2,296 from k=2 to 3, by 1,239 from 3 to 4, then by 569 or less per step.
- **Stability method:** Hennig clusterboot, B = 100 resamples of the fit set, refit each time, mean of the best Jaccard per original cluster.
- **Comparison with other algorithms:**

  | candidate | silhouette | mean Jaccard |
  |---|---|---|
  | **K-Means k=3** | **0.468** | 1.000 |
  | Agglomerative (Ward) k=3 | 0.466 | 0.996 |
  | GMM k=3 | 0.275 | 0.637 |
  | GMM k=8 (best BIC) | 0.339 | 0.752 (min 0.189) |

  K-Means wins on silhouette among the stable candidates.

## Validation

| metric | fit (800) | holdout (200) |
|---|---|---|
| silhouette | 0.468 | 0.454 |
| ARI vs hidden profile | 0.373 | **0.403** |
| ARI vs bank (feature table; a template check) | 0.001 | 0.000 |

- **Holdout transfer.** Assignments by `predict` vs a K-Means refit on the holdout alone give ARI 1.000.
- **Bootstrap Jaccard** is 1.000 for every cluster, so all three are named. The names come from the top-2 centroid z-scores by a fixed template; profile names are never used.

| segment | name (template) | fit | holdout | profile composition (all 1,000) |
|---|---|---|---|---|
| 0 | high IMPS share, high RTGS share | 564 | 136 | 285 healthy_saas, 217 burning_startup, 198 services_firm |
| 1 | high cash share, high UPI share | 114 | 27 | 141 restaurant |
| 2 | high aggregator-settled revenue, low customer count | 122 | 37 | 159 ecommerce |

**Interpretation.** The segments separate **revenue models**: B2B invoiced, cash/UPI retail, and aggregator-settled. They don't separate the three B2B profiles (saas, startup and services all fall in segment 0). That is why ARI is 0.40, not higher.
- Segments are independent of the bank layout (ARI ≈ 0), so they don't just reflect statement templates.
- Segment 0's template name points at rails. Its raw centroid shows what actually distinguishes it: about 38 customers and almost no aggregator revenue.

**Fit-set centroids** (raw units):

| | rev growth | burn/revenue | runway (mo) | HHI | customers | rev CV | founder share | UPI | NEFT | RTGS | IMPS | cash | aggregator |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | 0.125 | 4.60 | 20.1 | 0.099 | 38.3 | 0.454 | 0.005 | 0.073 | 0.349 | 0.108 | 0.182 | 0.001 | 0.020 |
| 1 | 0.012 | 0.93 | 2.7 | 0.222 | 5.9 | 0.206 | 0.012 | 0.315 | 0.120 | 0.025 | 0.088 | 0.226 | 0.002 |
| 2 | 0.024 | 1.82 | 0.0 | 0.341 | 0.4 | 0.214 | 0.005 | 0.001 | 0.467 | 0.043 | 0.092 | 0.003 | 0.968 |

**ARI by k (diagnostic only; it played no part in selection).** K-Means ARI vs profile on the fit set is 0.322 / 0.373 / 0.582 / **0.626** / 0.587 / 0.583 / 0.532 / 0.495 / 0.464 for k = 2…10.
- A k = 5 model would match the generator's profiles better.
- Choosing it because of that would mean fitting to hidden labels, and k = 5 is less stable (min Jaccard 0.733).
- The pre-registered, label-free rule stands.

**Latency.** 2.63 ms per company assignment (transform, scale, predict, PCA; median). Feature computation runs on outputs the pipeline has already produced.

## Peers and integration

- **Nearest peers:** the 3 nearest companies **in the uploaded cohort**, by Euclidean distance in scaled feature space (`segmentation.n_peers`).
- **Plot:** the PCA plot uses the fit-set PCA (two components). Explained variance is recorded in metadata.
- **No model:** `SEGMENTATION_ML_ENABLED=false`, `model_version: "unset"`, or a missing artifact all mean no features are computed and no sheet or page is added. `tests/test_ml_segmentation.py` checks both paths.

## Reproduce

```
make corpus
python -m ml.segmentation.train data/corpus/v1          # ~1 min (company features cached)
python -m ml.segmentation.evaluate data/corpus/v1 --write-thresholds
```
