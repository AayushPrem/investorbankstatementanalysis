# BSAA — Bank Statement Analysis Agent

An agentic pipeline that ingests Indian bank statement PDFs (HDFC, ICICI) and produces structured investor-grade reports for angel investors and VC funds doing due diligence on early-stage companies.

**Current status: Sprint 1 + Sprint 2 + Sprint 3 complete — 733 tests passing.**

---

## What it does

Upload a company's bank statement — PDF (digital or scanned), CSV, Excel, or a photographed page — and the system:

1. Extracts every transaction and validates data integrity
2. Categorises each transaction (Revenue, Salary, Tax, Vendor Payment, etc.)
3. Detects six classes of suspicious patterns (structuring, round-tripping, founder extraction, etc.)
4. Computes financial health metrics (burn rate, runway, MoM growth)
5. Tracks customer behaviour (NRR, churn, cohort retention, concentration)
6. Identifies related-party transactions automatically from narration text
7. Generates financial health alerts when key metrics cross investor-relevant thresholds
8. Checks Indian regulatory compliance (§269ST cash limits, GST/TDS patterns, PMLA aggregation, related-party transaction limits)
9. Reconciles bank-statement reality against a company's pitch-deck / self-reported claims
10. Produces a 1-page Angel Lens PDF, a 6-sheet VC Lens workbook, and a 10-sheet Workbench XLSX (with fund-specific custom detectors)
11. Tags every run with schema + agent versions and supports replay/diff against the current code

---

## Architecture — 5-Layer Pipeline

```
PDF / CSV / Excel / Scanned PDF / Image / Synthetic Statement
         │
         ▼
┌─────────────────────┐
│  Layer 1: Ingestion │  pdfplumber · openpyxl · pypdfium2 · pytesseract (OCR)
│  adapter + normaliser│  Format-specific adapters extract rows, map to canonical schema
└────────┬────────────┘
         │  StatementDocument (Pydantic)
         ▼
┌─────────────────────┐
│  Layer 2: Validation│  Balance continuity · Duplicate detection
│  + Categorisation   │  Rule-based + Claude Haiku LLM
└────────┬────────────┘
         │  Categorised StatementDocument
         ▼
┌─────────────────────┐
│  Layer 3: Enrichment│  Related-party tagger · Customer identity resolver
│                     │  Regex · sentence-transformers · Claude Haiku LLM
└────────┬────────────┘
         │  Enriched StatementDocument
         ▼
┌─────────────────────┐
│  Layer 4: Analysis  │  Risk flags · Customer analytics · Financial health alerts
│                     │  Compliance (India) · Pitch-deck reconciliation
└────────┬────────────┘
         │  RiskReport · CustomerAnalyticsReport · ComplianceReport · ReconciliationReport
         ▼
┌─────────────────────┐
│  Layer 5: Reports   │  Angel Lens PDF · VC Lens XLSX/PDF · Workbench XLSX/PDF
│                     │  ReportLab · openpyxl · fund-specific custom detectors
└─────────────────────┘
```

Every run is tagged with the schema version and each agent's semver (`pipeline/versioning.py`) and persisted to `.pipeline_state/`, so a past input can be replayed under the current code and diffed against its original result.

---

## Sprint 1 — Foundations

### Canonical Transaction Schema (`schema/canonical.py`)
The data contract shared by every component downstream. Built with **Pydantic v2**.

Each `CanonicalTransaction` holds:
- Core fields: `date`, `description`, `debit`, `credit`, `balance` (all monetary values as `Decimal`)
- Source traceability: `SourceReference` (file path, page, row) so any finding can be traced back to a specific line in the original PDF
- Enrichment slots populated by later agents: `category`, `counterparty`, `customer_id`, `is_related_party`, `related_party_match`, `anomaly_flags`

`StatementDocument` wraps the transaction list with account metadata and `validation_status`.

---

### Synthetic Bank Statement Generator (`tools/synthetic_gen.py`)
Generates realistic Indian bank statement PDFs with paired ground-truth JSON for testing.

**Library:** ReportLab (PDF generation)

**5 company profiles:**
| Profile | Behaviour |
|---|---|
| `healthy_saas` | 28–35 recurring customers (TCS, Wipro, Infosys, etc.), growing MoM, predictable burn |
| `burning_startup` | Declining customers, rapid hiring, heavy cloud/marketing spend |
| `services_firm` | 4–10 large lumpy B2G payments (ONGC, NTPC, HAL), 30-day payment delays |
| `ecommerce` | Daily Razorpay settlements, seasonal peaks (Diwali +18%, April -22%) |
| `restaurant` | Daily cash + UPI deposits, food vendor payments, staff salaries |

**Realism features:**
- Real Indian company names (customers, vendors, cloud providers)
- Indian FY seasonal multipliers (March +20% year-end flush, April -22% new FY dip)
- Payment delays: 15–30% of customers pay 15–45 days late
- April salary hike (10% — Indian FY start)
- Indian tax calendar: GST on 20th, TDS on 7th, advance tax in Jun/Sep/Dec/Mar
- 6 injectable risk flags with probabilistic incidence rates per profile

**3 risk modes:**
- `clean` — no flags
- `realistic` — probabilistically selects flags matching real-world incidence rates per profile
- `custom` — manually specify flags to inject

**Duration:** 3–24 months, configurable.

---

### PDF Adapter (`adapters/digital_pdf.py`)
Extracts raw rows from digital (non-scanned) HDFC and ICICI bank statement PDFs.

**Library:** pdfplumber (benchmarked against PyMuPDF and tabula-py; pdfplumber won on table extraction accuracy for Indian bank formats)

- Detects the bank from header text (HDFC / ICICI)
- Extracts tables page by page, skips repeated header rows on continuation pages
- Preserves `(page, row)` coordinates for every cell — used later by the validator for traceability
- Returns a `RawExtractionResult` with column names exactly as seen in the PDF; no interpretation

---

### Normaliser (`pipeline/normaliser.py`)
Maps raw extracted rows to `CanonicalTransaction` records using bank-specific profiles.

**Profiles** (Python dicts, one per bank):
- Column name mapping (e.g. HDFC `"Narration"` → `description`, `"Withdrawal Amt.(Dr)"` → `debit`)
- Date format (`%d/%m/%y` for HDFC, `%d-%m-%Y` for ICICI)
- Indian number format parsing (`"1,23,456.78"` → `Decimal("123456.78")`)

Each transaction gets a UUID4 `transaction_id` and a `SourceReference` pointing back to its exact page and row in the original PDF.

---

### Validator (`pipeline/validator.py`)
Data-quality guard that runs after normalisation. Sets `validation_status` on the document.

**6 checks:**
| Check | Severity | What it catches |
|---|---|---|
| `BALANCE_BREAK` | ERROR | Running balance doesn't equal `prev_balance + credit − debit` |
| `DUPLICATE_ID` | ERROR | Two transactions share the same `transaction_id` |
| `SPARSE_TRANSACTIONS` | WARNING | Fewer than 0.5 transactions/week for the period |
| `OUT_OF_ORDER` | WARNING | Transaction date is earlier than the preceding row |
| `DATE_OUTSIDE_PERIOD` | WARNING | Transaction date is outside the stated statement period |
| `EMPTY_DESCRIPTION` | WARNING | Blank narration field |

Any ERROR → `ValidationStatus.FAILED`. Warnings are recorded but don't fail validation.

---

### Categoriser (`analysis/categoriser.py`)
Assigns a `TransactionCategory` to every transaction.

**Two-stage approach:**

**Stage 1 — Rule-based (fast, free):** Regex patterns match common Indian bank narrations to categories. Covers ~98% of cases:
- `GST PMT`, `TDS PAYMENT` → `TAX`
- `ECS/SALARY`, `PAYROLL` → `SALARY`
- `NEFT CR-*` with customer narration → `REVENUE`
- `RAZORPAY SETTLEMENT` → `REVENUE`
- etc.

**Stage 2 — LLM fallback (Claude Haiku):** Transactions that don't match any rule are batched (up to 20 per API call) and sent to Claude Haiku with a few-shot prompt. Results are cached in `.cache/categoriser_cache.json` keyed by a hash of the normalised description — repeated narrations never make a second API call.

**Library:** `anthropic` SDK

---

### Financial Analyst (`analysis/financial_analyst.py`)
Pure arithmetic — no LLM. Computes investor-relevant metrics from categorised transactions.

**Outputs (`FinancialMetrics`):**
- `total_revenue`, `total_burn`
- `avg_monthly_revenue`, `avg_monthly_burn`
- `runway_months` — `closing_balance / avg_monthly_burn`
- `mom_revenue_growth_rates` — month-over-month revenue change for each consecutive pair
- `avg_mom_growth` — mean of the above
- `revenue_hhi` — Herfindahl–Hirschman Index measuring revenue concentration
- `top_customer_revenue_pct` — share of revenue from the single largest customer
- `monthly_stats` — per-month breakdown of revenue, burn, and net cash flow

All monetary arithmetic uses Python `Decimal` throughout.

---

### Angel Lens Report (`reports/angel_lens.py`)
Produces a **1-page investor PDF** — the "quick read" for angel investors.

**Library:** ReportLab (direct PDF construction, no HTML intermediate)

**Contents:**
- Verdict banner: `INVESTABLE` / `MONITOR` / `CAUTION` with colour-coded background, derived from runway + growth combination rules
- Verdict rationale: one-line explanation ("cash-flow positive · 14mo runway · NRR 104%")
- 6 KPI tiles: Revenue, Burn, Runway, MoM Growth, Active Customers, NRR
- Monthly revenue/burn table (up to 12 months)
- Up to 8 health signals — tuples of (severity prefix, investor-facing text) covering revenue trend, burn rate, runway, NRR, churn, concentration, and any HIGH-severity risk flags
- Risk mini-section if risk flags exist: composite score badge + flag count + severity breakdown

---

## Sprint 2 — Risk, Analytics, VC Lens

### Related-Party Tagger (`pipeline/related_party.py`)
Tags transactions where the counterparty is related to the investee company.

**Two-stage matching:**

**Stage 1 — Deterministic (no API cost):**
- Strips payment prefixes (`NEFT/`, `RTGS/`, bank routing codes), trailing reference numbers, legal suffixes (`PVT LTD`, `LLP`, `PRIVATE LIMITED`)
- Lowercases and normalises whitespace
- Compares cleaned narration against every affiliate name + alias

**Stage 2 — LLM fuzzy match (Claude Haiku):**
- Unmatched narrations are batched (30 per call) with the affiliate list
- Claude returns `{match, confidence}` JSON per item
- Matches with confidence ≥ 0.8 are tagged as related-party

**Auto-detection (no manual input):**
`extract_counterparties(doc)` scans all narrations using the same cleaning logic and returns a ranked list of `(entity_name, txn_count, total_volume)`. The Streamlit UI presents this as a multi-select — users pick which counterparties are affiliates instead of typing names manually.

---

### Customer Identity Resolver (`pipeline/customer_identity.py`)
Groups revenue transactions from the same underlying customer even when names appear differently across narrations.

**Three-stage resolution:**

**Stage 1 — Normalise:** `clean_counterparty()` strips prefixes, reference numbers, legal suffixes. Same logic as the related-party cleaner.

**Stage 2 — Exact-match cluster:** Transactions with the same cleaned name get the same `customer_id` (`"cust_" + 8-char hash`).

**Stage 3 — Embedding merge:**
- Computes sentence-transformers embeddings (`all-MiniLM-L6-v2`) for each cluster representative
- Pairs with cosine similarity > 0.85 are merged via union-find
- Embeddings cached in `.cache/embeddings.json`

**Library:** `sentence-transformers`

Result: every REVENUE transaction has `customer_id` set, enabling downstream churn and NRR calculations.

---

### Risk Analyst (`analysis/risk.py`)
Six independent pattern detectors, each returning `list[Flag]`. A `Flag` has `detector_name`, `Severity` (HIGH / MEDIUM / LOW), `triggering_transaction_ids`, and `description`.

| Detector | What it catches | Severity |
|---|---|---|
| `structuring` | ≥4 credits between ₹1.5L–₹2L within 7 days (§269ST avoidance) | HIGH |
| `round_tripping` | Matching outflow + inflow from same counterparty within 3–14 days, amounts within 5% | HIGH |
| `spike_drain` | Monthly net flow with z-score > 2.5σ vs leave-one-out baseline | HIGH/MEDIUM |
| `customer_concentration` | Top-3 customers > 80% revenue (HIGH) or > 60% (MEDIUM) | HIGH/MEDIUM |
| `round_amount_clustering` | > 30% of revenue transactions are exact ₹1,000 multiples | HIGH/MEDIUM |
| `founder_over_extraction` | Monthly founder withdrawals > 3× declared salary or > ₹5L/month | HIGH/MEDIUM |

**Composite score:** `min(100, Σ weights)` where HIGH=10, MEDIUM=5, LOW=2.

**Narrative:** Claude Haiku generates a 3–5 sentence investor-facing plain-English risk summary from all active flags. Falls back to a rule-based summary if no API key.

---

### Customer Analytics Analyst (`analysis/customer_analytics.py`)
Computes retention and growth metrics from the `customer_id`-tagged revenue transactions.

**Outputs (`CustomerAnalyticsReport`):**

| Metric | How computed |
|---|---|
| `monthly_active_customers` | Distinct `customer_id`s with REVENUE in each month |
| `monthly_active_trend` | Linear regression slope of active count over time |
| `churn_events` | Customers who paid in month M but not in M+1, M+2, or M+3 (3-month silence rule) |
| `nrr_per_month` | For consecutive months M→M+1: `(M+1 revenue from M's customers) / (M revenue from M's customers)` |
| `cohort_retention` | Assigns each customer to first-payment-month cohort; tracks % still paying at N months offset |
| `concentration_trajectory` | Per month: top-3 and top-10 customer revenue shares |
| `payment_regularity_alerts` | Customers with recent payment gap > 2× their modal gap |

All monetary arithmetic uses `Decimal`; ratios use `float`.

---

### Financial Health Alerts (`analysis/financial_health_alerts.py`)
Metric-based alerts that flag investor-relevant financial health issues — separate from the transaction-pattern risk flags.

**13 alert types across 6 categories:**

| Category | Alert | Severity |
|---|---|---|
| Runway | < 2 months | HIGH |
| Runway | 2–4 months | MEDIUM |
| Runway | 4–6 months | LOW |
| Burn vs Revenue | Burn > 3× revenue | HIGH |
| Burn vs Revenue | Burn > 1.5× revenue | MEDIUM |
| Revenue Trend | Declining 3+ consecutive months | HIGH |
| Revenue Trend | Declining 2 consecutive months | MEDIUM |
| Revenue Trend | Flat < 2% for 3+ months | LOW |
| Burn Acceleration | Costs growing > 20% and > 15pp faster than revenue | MEDIUM |
| Cash Flow | Net negative every single month | MEDIUM |
| NRR | < 70% | HIGH |
| NRR | 70–85% | MEDIUM |
| NRR | 85–100% | LOW |
| Churn | > 30% of peak customer base | HIGH |
| Churn | > 15% of peak customer base | MEDIUM |
| Customer Count | Declining 3 consecutive months | MEDIUM |

**LLM polishing:** All detected alerts are batched into a single Claude Haiku call with full financial context. Claude rewrites each description as 2 investor-facing sentences with specific numbers. Falls back to rule-based text without an API key.

---

### VC Lens Report (`reports/vc_lens.py`)
The full institutional-grade output. Produced by `VCLensReport().generate(analysis_result)`.

**XLSX workbook (6 sheets)** — built with **openpyxl**:
| Sheet | Contents |
|---|---|
| Summary | Key KPIs, top findings, risk score badge, narrative — each KPI has a "What this means" explanation column |
| Financial Health | Monthly revenue/burn/net table with interpretation column; cells conditionally formatted |
| Red Flags | One row per risk flag with severity, description, triggering transaction IDs, and "What This Means for Investors" |
| Customer Analytics | NRR table with signal + implication columns; cohort retention matrix (green ≥80%, amber ≥50%, red below); concentration trajectory |
| All Transactions | Full enriched transaction ledger |
| Related Parties | Filtered view of related-party transactions, aggregated by affiliate |

**PDF (3 pages)** — built with **ReportLab**:
- Page 1: Financial summary — KPIs, monthly breakdown, risk score
- Page 2: Customer analytics — NRR table with signals, active customer counts, churn list
- Page 3: Risk flags — each flag has a severity-coloured header row + "Why it matters" body row; tailored due-diligence checklist at the bottom

---

## Sprint 3 — Compliance, Reconciliation, Workbench, Format Breadth

### Indian Jurisdiction Module (`jurisdictions/india/rules.py`)
Five compliance rules, each citing the exact regulatory basis and returning `ComplianceException`s with the triggering transaction IDs:

| Rule | Citation | What it catches |
|---|---|---|
| `cash_transaction_limit_269st` | Income Tax Act §269ST | Cash receipts ≥ ₹2L per day per counterparty |
| `gst_payment_consistency` | GST Act | Revenue > ₹40L/yr with no regular GST outflows |
| `tds_pattern_expectation` | Income Tax Act (TDS) | Salary outflows above threshold with no matching TDS |
| `pmla_high_value_aggregation` | PMLA — STR threshold | Cash transactions aggregating ≥ ₹10L in a 30-day window |
| `related_party_transaction_limit` | Companies Act §188 | Related-party transaction volume exceeding board-approval limits |

`jurisdictions/__init__.py` defines a `JurisdictionModule` protocol so a new market is a new module, not a rewrite.

### Compliance Analyst (`analysis/compliance.py`)
Runs every rule in a jurisdiction module and produces a `ComplianceReport` (exceptions, counts by severity). Each exception's technical description is reframed by Claude Haiku into `investor_risk_framing` — "this exposes investors to X" — with a rule-based fallback when no API key is set.

### Reconciliation Analyst (`analysis/reconciliation.py`)
Compares a company's declared claims (`CompanyClaims`: revenue, customer count, NRR, loans, headcount × avg salary, "never lost a customer") against what the bank statement actually shows — declared revenue vs. summed `REVENUE` transactions, declared customer count vs. distinct `customer_id`s, declared NRR vs. computed NRR, claimed loans vs. `LOAN_IN` transactions, and more. Each mismatch is a `ReconciliationFinding` with magnitude, direction (over/under-reported), and an LLM-generated investor framing.

### Workbench Lens (`reports/workbench_lens.py`)
The deepest report: **10-sheet XLSX + 4-page PDF** — Executive Summary, Financial Health, Risk Flags, Customer Analytics, Compliance Findings, Reconciliation, All Transactions, Related Parties, Customer Master (one row per resolved customer), and a Raw Data Export sheet for the fund's own modelling.

**Custom detector hook (`workbench_config.py`):** analysts can toggle any of the six built-in risk detectors off, or add fund-specific detectors as plain `(StatementDocument) -> list[Flag]` functions — without touching `analysis/risk.py`. `workbench_config.apply()` re-filters the risk report and recomputes the composite score before the Workbench lens renders.

### Versioning & Replay (`pipeline/versioning.py`)
Every run is tagged (`VersionTag`) with the frozen schema version, a semver per agent, and the jurisdiction module version, then persisted via `ResultStore` to `.pipeline_state/{run_id}/run.json`. Because each run gets its own `run_id`, re-analysing the same input after an agent is upgraded never overwrites the historical record. `replay(store, run_id)` re-runs the original input file through the *current* code, and `diff_results()` shows exactly which metrics moved.

### Additional Format Adapters (`adapters/`)
The pipeline now ingests five input formats through the same `RawStatement`/`RawRow` contract as the digital PDF adapter:

| Adapter | Handles |
|---|---|
| `csv.py` | Delimiter + encoding auto-detection, header row not at row 1, column-keyword mapping (`adapters/_column_mapping.py`) |
| `excel.py` | Multi-sheet workbooks (picks the sheet with a detectable header), merged cells, header row not at row 1 |
| `scanned_pdf.py` | Tesseract OCR (`pytesseract`) — greyscale → deskew → denoise → Otsu binarise → word-level OCR with confidence; debit/credit is inferred from the running-balance direction rather than fragile column position |
| `image.py` | Single JPG/PNG statement page, reusing the scanned-PDF OCR pipeline |

CSV/Excel column headers are matched by keyword (`Narration`/`Particulars`/`Description`, `Withdrawal`/`Debit`, `Deposit`/`Credit`, …) since — unlike HDFC/ICICI PDFs — a spreadsheet export has no fixed per-bank column layout. OCR adapters record per-row `ocr_confidence` (0–100) so low-confidence rows can be flagged for manual review.

---

## Demo UI (`demo/app.py`)

Built with **Streamlit**. Run with:
```
.venv\Scripts\streamlit.exe run demo/app.py
```

### Two input modes (sidebar toggle):

**Upload PDF** — Drop any HDFC or ICICI bank statement PDF.

**Generate Synthetic** — Create a statement inside the app:
- Profile: Healthy SaaS / Burning Startup / Services Firm / E-Commerce / Restaurant
- Bank: HDFC or ICICI
- Duration: 3–24 months (slider)
- Risk mode: Clean / Realistic (probabilistic) / Custom (pick flags manually)
- Optional fixed seed for reproducibility
- Download the generated statement PDF directly from the sidebar

### Smart affiliate detection (no manual typing):
After the first pipeline pass, `extract_counterparties()` scans all narration text and presents a multi-select of auto-detected entities ranked by transaction volume. Select any to mark as affiliates → pipeline re-runs and tags their transactions automatically.

### 8 analysis tabs:
| Tab | Contents |
|---|---|
| 📈 Financial | Revenue vs burn bar chart · Transaction category pie chart · Financial Health Alerts section |
| 🚩 Risk Flags | Composite risk score gauge · Expandable flag cards with evidence · Analyst narrative |
| 👥 Customers | Active customer trend · NRR chart · Revenue concentration trajectory · Cohort retention heatmap · Churn table |
| ⚖️ Compliance | Indian regulatory exceptions (§269ST, GST, TDS, PMLA, related-party limits) with investor risk framing |
| 🔎 Reconciliation | Declared pitch-deck claims vs. bank-statement reality, with delta % and direction |
| 📋 Transactions | Full ledger with category and related-party filters |
| 🔗 Related Parties | Auto-detected counterparty table · Tagged related-party transactions · Aggregated by affiliate |
| 📄 Downloads | Angel Lens PDF · VC Lens XLSX/PDF · Workbench XLSX/PDF |

Note: the demo UI currently accepts PDF upload only (`type=["pdf"]`); the CSV/Excel/scanned-PDF/image adapters are available programmatically (see Sprint 3 section above) but not yet wired into the Streamlit uploader.

---

## Technical Stack

| Purpose | Library |
|---|---|
| Data modelling | Pydantic v2 |
| PDF extraction | pdfplumber (digital text) · pypdfium2 (page rendering for OCR) |
| OCR | pytesseract (Tesseract OCR binary required on PATH) |
| CSV/Excel extraction | Python `csv` + openpyxl |
| PDF generation | ReportLab |
| XLSX generation | openpyxl |
| LLM calls | anthropic SDK (Claude Haiku 4.5) |
| Customer embeddings | sentence-transformers (`all-MiniLM-L6-v2`) |
| UI | Streamlit |
| Charts (UI) | Plotly (pinned to `<6.0` for Streamlit compatibility) |
| Testing | pytest (733 tests) |

---

## Project Structure

```
investorproject/
├── schema/
│   └── canonical.py          # CanonicalTransaction + StatementDocument (Pydantic)
├── adapters/
│   ├── digital_pdf.py        # pdfplumber extraction for HDFC + ICICI
│   ├── csv.py                 # Delimiter/encoding auto-detect + header-keyword mapping
│   ├── excel.py                # Multi-sheet, merged-cell aware
│   ├── scanned_pdf.py           # Tesseract OCR: deskew → denoise → binarise → OCR
│   ├── image.py                  # Single JPG/PNG page, reuses scanned_pdf's OCR pipeline
│   └── _column_mapping.py         # Shared header-keyword matcher (csv.py + excel.py)
├── pipeline/
│   ├── normaliser.py          # Raw rows → CanonicalTransaction
│   ├── validator.py           # Balance continuity + data quality checks
│   ├── related_party.py       # Affiliate tagging + counterparty extraction
│   ├── customer_identity.py   # Customer deduplication (embeddings)
│   └── versioning.py          # Schema/agent version tags, ResultStore, replay, diff
├── analysis/
│   ├── categoriser.py         # Rule-based + LLM transaction categorisation
│   ├── financial_analyst.py   # Burn, runway, MoM growth metrics
│   ├── risk.py                # 6 pattern detectors + composite score
│   ├── customer_analytics.py  # NRR, churn, cohort, concentration
│   ├── financial_health_alerts.py  # Metric-based investor alerts
│   ├── compliance.py          # Runs a JurisdictionModule's rules
│   └── reconciliation.py      # Declared claims vs. bank-statement reality
├── jurisdictions/
│   └── india/rules.py         # §269ST, GST, TDS, PMLA, related-party limit rules
├── reports/
│   ├── angel_lens.py          # 1-page PDF for angel investors
│   ├── vc_lens.py             # 6-sheet XLSX + 3-page PDF for VCs
│   └── workbench_lens.py      # 10-sheet XLSX + 4-page PDF for in-house finance teams
├── workbench_config.py        # Fund-specific detector toggles + custom detectors
├── tools/
│   └── synthetic_gen.py       # Realistic bank statement PDF generator
├── demo/
│   ├── app.py                 # Streamlit UI
│   └── pipeline_runner.py     # Pipeline orchestration for the demo
├── tests/                     # 733 pytest tests
└── .cache/                    # Categoriser + embedding caches
```

---

## Running the Project

**Install dependencies (virtual environment):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
```

**Run tests:**
```powershell
python -m pytest tests/ -q
```

**Start the demo:**
```powershell
.venv\Scripts\streamlit.exe run demo/app.py
```

**Generate synthetic statements from CLI:**
```powershell
python -m tools.synthetic_gen --bank hdfc --profile healthy_saas --months 12 --risk-mode realistic --output data/synthetic/
```

**Environment variable** (optional — enables LLM features):
```
ANTHROPIC_API_KEY=sk-ant-...
```
Without it, the categoriser uses rule-only mode, the risk narrative uses fallback text, and financial health alert descriptions use rule-based text. All core analysis still runs.

**Tesseract OCR binary** (optional — only needed for `adapters/scanned_pdf.py` and `adapters/image.py`): install from https://github.com/tesseract-ocr/tesseract and ensure `tesseract` is on PATH. Every other adapter and the full test suite work without it.
