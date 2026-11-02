# ml/categoriser: supervised transaction categoriser (Phase 4)

> Trained and evaluated on **SYNTHETIC data only** (generator v2.0.0, corpus v1). See `docs/ml/corpus_card.md` and the model card `docs/ml/categoriser.md`.

## Commands

```
make corpus                                               # data/corpus/v1 (hash-gated)
python -m ml.categoriser.rule_audit data/corpus/v1        # D10 rule precision audit
python -m ml.categoriser.train      data/corpus/v1 --split all
python -m ml.categoriser.evaluate   data/corpus/v1 --write-thresholds
```

`train.py` and `evaluate.py` refuse to run unless `corpus_hash` and `splits_hash` match `config.yaml → pinned_corpora`. CI uses `--allow-unpinned --profile ci` on `v1-small`.

## Files

| file | role |
|---|---|
| `config.yaml` | pinned hashes, feature caps, model grids, rule-audit and routing targets |
| `data.py` | loads the features (`features/`), labels (`true_category` only) and split membership; the hash gate |
| `rule_audit.py` | per-rule precision on company train+val; decides the high-precision rule list |
| `train.py` | fits LR / calibrated LinearSVC / LightGBM (+ MiniLM evaluation) per split; picks on validation; registers models |
| `evaluate.py` | τ from validation; test metrics, routing, plots, latency → `results/categoriser/<model_version>/` |
| `infer.py` | `CategoriserModel`, `resolve_model()` (used by `analysis.categoriser.Categoriser`) |
| `metrics.py` | macro and per-class metrics over supported classes, ECE |
| `../features/categoriser.py` | the one feature builder, shared by training and serving |
| `../registry.py` | `models/<name>/<version>/{model.joblib, metadata.json}` with a sha256 check |

## Leakage rules

- Features: description, debit/credit, date. Never bank, balance, ref_no, txn_time, company_id or any label column.
- Splits are grouped (company / bank / counterparty). Hyper-parameters, the algorithm and τ are chosen on **validation**. Test is evaluated once.
- `counterparty_key` and `template_id` are read from labels only to assign the unseen-vendor split and to slice reports.

## Routing (analysis/categoriser.py v3)

```
high-precision rule → ML p_max ≥ τ_accept → LLM picks from the ML top-3 → review queue
no LLM: p_max ≥ τ_review → ML (degraded accept), else review (ML best guess kept)
no model: v2 rules-only behaviour, unchanged
```
