# ADR-0005: ML anomaly flags are a separate stream from rule flags

- **Status:** Accepted
- **Date:** 2026-11-02
- **Phase:** 5 (`upgrade/p5-anomaly`)

## Context

Phase 5 adds an unsupervised IsolationForest that marks transactions as "unusual for this account" (UPGRADE_GUIDE §11). The rule detectors in `analysis/risk.py` already produce `RiskReport.flags`, and that list feeds several things:
- `compute_composite_score`, which drives the portfolio high-risk threshold (`portfolio.high_risk_score`)
- the narrative (`severity_counts`, `flags[:5]`)
- `summarise_result.risk_flag_count`
- the workbench config
- every report lens
- the gold-set regression (risk_recall 88.5%, compliance_recall 90.0%)

An unsupervised score has no named pattern behind it. On synthetic data its precision is measured in tens of percent, not near 100%. Mixing it into the rule list would change the risk score and the investor-facing text for reasons nobody can point to.

## Decision

- **Where the flags go.** The `ml_anomaly` stage (after `risk` in `pipeline/run.py`) writes `AnalysisResult.anomaly_report`, a list of `AnomalyFlag`s with `kind="ml"`, a `score`, and evidence (percentile, scorer, model version, top deviating features). Nothing is added to `risk_report.flags`.
- **What doesn't change.** The composite score, the narrative, the angel and VC lenses, the workbench config and the gold regression ignore ML flags.
- **Where they appear:**
  - the `flags` table with `kind='ml'` (the constraint already allowed it; no migration)
  - a separate "Investigate (ML)" sheet and PDF block in the workbench lens
  - a separate expander in the demo's risk tab
  - All of these show only when ML flags exist.
- **Wording.** Every ML flag says "investigate", and the evidence says it is not a finding. Nothing says "fraud".
- **No-model path.** With no model (disabled, unset, missing, sha mismatch, or no `[ml]` extra) the stage is skipped entirely: no stage entry, `anomaly_report=None`, and summaries, lenses and persisted rows are byte-for-byte what they were before Phase 5.
- **Failures.** If the stage fails, the error is logged and no ML flags are produced. It never blocks the rule outputs.

## Alternatives rejected

- **Append ML flags to `RiskReport.flags` with a low severity weight.** That changes `risk_flag_count`, the narrative's top-5 list, the lenses' "6 automated detectors" text and, through any non-zero weight, the composite score. It would also need every consumer to filter by kind.
- **A seventh detector inside `RiskAnalyst`.** It would couple an optional, artifact-dependent model to the deterministic detectors and their tests (e.g. `test_risk.py`'s clean-document score of 0).

## Consequences

- Investor-facing lenses (angel, VC) don't show ML flags yet. Adding them is a product decision for a later phase.
- Any consumer that wants "all flags" has to read both `risk_report.flags` and `anomaly_report.flags`. The API returns both from `/runs/{id}/flags` with their `kind`.
