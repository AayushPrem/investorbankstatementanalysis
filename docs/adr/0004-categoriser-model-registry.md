# ADR-0004: scikit-learn categoriser with a local file registry

- **Status:** Accepted
- **Date:** 2026-11-02
- **Phase:** 4 (`upgrade/p4-categoriser-ml`)

## Context

Phase 4 adds a supervised categoriser that sits between the rule layer and the LLM fallback (UPGRADE_GUIDE §10). The pipeline has to load one specific, versioned model, and a run has to record which model produced each categorisation. A model must be traceable to the exact corpus (`corpus_hash`, `splits_hash`), code (git sha), parameters and library versions that produced it. The pipeline must keep working, unchanged, when no model is present (fresh clone, demo, no `[ml]` extra).

## Decision

- **Model:** a scikit-learn `Pipeline`: a `ColumnTransformer` (char_wb 3–5 TF-IDF ∪ word 1–2 TF-IDF on a normalised narration, plus rail one-hot, day-of-month bucket, log amount and round-amount flags) feeding a classifier. The classifier is chosen on validation macro-F1 from LogisticRegression, calibrated LinearSVC and LightGBM (`ml/categoriser/train.py`). Training and serving share the same feature code (`ml/features/categoriser.py`).
- **Registry:** `ml/registry.py` writes `models/<name>/<version>/model.joblib` and `metadata.json`. The metadata holds git sha, corpus identity, split, params, metrics, library versions, created_at and the artifact's sha256. Binaries are gitignored; `metadata.json` is committed.
- **Selection at runtime:** `config/thresholds.yaml → categoriser.model_version` names the production model, next to the τ thresholds tuned for it. The `CATEGORISER_MODEL_VERSION` env var overrides it (CI uses this for its small-corpus model). If the model is missing or fails verification, the categoriser logs a warning and uses the v2 rules-only path.
- **New dependencies** (in the `[ml]` extra): `joblib` (already a scikit-learn dependency), `matplotlib` (evaluation plots), `lightgbm` (baseline comparison only; not needed at runtime unless it is the selected model).

## Alternatives rejected

- **MLflow / a model registry server:** a tracking server and a backend store for one model family and one developer is new infrastructure without a problem it solves here (rule 8). The metadata file records everything an MLflow run would for this use, and the format could be imported later.
- **Committing the artifact to git:** the model is several MB of binary that changes on every retrain. CI trains its own model on the small corpus instead (decision recorded in docs/CHANGELOG_UPGRADE.md, Phase 4).
- **ONNX export:** would remove the pickle concern but adds a converter dependency and a second inference runtime. The sklearn pipeline is fast enough (latency in docs/ml/categoriser.md).

## Consequences

- **joblib uses pickle.** Loading an untrusted file can execute code. `registry.load` reads only from the local `models/` directory, rejects path components containing separators, and refuses any artifact whose sha256 differs from `metadata.json`. The sha256 check protects against corruption and accidental swaps, not against an attacker who can write both files. Never load models from uploads or URLs.
- A pickled model is tied to the scikit-learn version it was trained with, which metadata records. After an upgrade, retrain (`make categoriser-train categoriser-eval`).
- τ values are only meaningful for the model they were tuned on, so `evaluate.py --write-thresholds` writes the version and both τ values together.
