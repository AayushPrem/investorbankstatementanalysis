# docs/

Supporting artifacts for BSAA. For the full project write-up — architecture, every
component, the demo UI, and how to run everything — see the [root README](../README.md).

## Contents

- **`system_tour.pdf`** — an 11-slide system tour: the problem, the 5-layer
  architecture, one slide per layer, one slide per report lens (Angel, VC,
  Workbench, Network), current validation numbers, and what's next. Regenerate
  with:
  ```
  python -m tools.generate_deck --output docs/system_tour.pdf
  ```
- **`architecture_diagram.png`** — the 5-layer pipeline diagram embedded in the
  deck's architecture slide and referenced from the root README. Regenerate with:
  ```
  python -m tools.generate_architecture_diagram --output docs/architecture_diagram.png
  ```
- **`decisions/`** — architecture decision records, added as significant design
  choices come up.

## The four lenses at a glance

| Lens | Who it's for | Output |
|---|---|---|
| Angel | Individual angel investors | 1-page PDF: verdict, KPIs, runway gauge, top health signals |
| VC | VCs / institutional investors | 6-sheet XLSX + 3-page PDF: financial health, risk, customer analytics, transactions, related parties |
| Workbench | In-house finance teams | 10-sheet XLSX + 4-page PDF: everything above plus compliance, reconciliation, customer master, raw data export, and fund-specific custom detectors (`workbench_config.py`) |
| Network | Angel investor networks | Cross-company comparison workbook — sortable/filterable table, risk distribution, sector breakdown, cohort dashboard PDF |

One pipeline produces every finding once; each lens is presentation only — see
`reports/` for the four lens implementations and `pipeline/batch.py` for how the
Network lens's inputs are produced concurrently across many statements.
