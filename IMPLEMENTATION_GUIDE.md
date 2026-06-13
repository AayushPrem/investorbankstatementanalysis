# BSAA — Claude Code Implementation Guide

**Project**: Agentic Bank Statement Analysis Platform — Indian market
**Approach**: Step-by-step implementation guide for use with Claude Code
**Target**: 8-week solo build, four 2-week sprints

---

## How to use this document

This file is the operational companion to the explanation PDF and the roadmap PDF. Where those documents explain *what* you're building and *why*, this one tells you *how* to build it, step by step, in a way Claude Code can execute.

**Workflow for each step:**

1. Read the step's "Goal" and "Context" so you understand what you're asking Claude Code to do
2. Copy the **prompt block** into your Claude Code terminal session
3. Review the code Claude Code produces — don't accept blindly
4. Run the **verification commands** at the end of each step before moving on
5. Commit before starting the next step (each step is a clean commit boundary)

**Important rules of engagement with Claude Code:**

- One agent or component per session if possible — keeps context clean
- Always include the canonical schema in context when working on any agent
- Ask Claude Code to write tests **before** the implementation when a gold-standard exists for that component
- After implementing, ask Claude Code to run the tests and report results — don't assume green
- If something feels overcomplicated, ask "is there a simpler way?" — Claude Code will often over-engineer

---

# Pre-Sprint 1: Environment Setup

Before any code, get the environment ready. This takes a few hours; do it carefully.

## Step 0.1 — Repository skeleton

**Goal**: Empty repo with proper structure, dependency management, CI scaffolding, and the working folder tree from the roadmap.

**Prompt to Claude Code:**

```
Set up a new Python 3.11+ monorepo for a project called "bsaa". Use pyproject.toml with the following:

- Tool: uv (or poetry if uv isn't available)
- Code formatter: ruff
- Type checker: mypy in strict mode
- Test runner: pytest

Create this directory structure with empty __init__.py files:
- schema/
- adapters/
- pipeline/
- analysis/
- jurisdictions/india/
- reports/
- profiles/
- tools/
- demo/
- data/{synthetic,real_anonymised,gold}/
- tests/
- results/
- docs/decisions/
- ops/

Add a .gitignore for Python projects plus data/* (except keep the directory structure with .gitkeep files) and results/* (same treatment).

Add a Makefile with these targets:
- install: set up the venv and install deps
- test: run pytest
- lint: run ruff
- typecheck: run mypy
- regression: placeholder — will run the gold-standard test suite later
- all: lint + typecheck + test + regression

Add an initial pyproject.toml with these dependencies:
- pdfplumber, pymupdf, tabula-py (for adapter benchmark)
- pydantic (for canonical schema)
- pandas, numpy
- anthropic (for LLM calls)
- streamlit (for demo UI)
- openpyxl, weasyprint, jinja2 (for report generation)
- sqlalchemy + psycopg2-binary (for result store)
- sentence-transformers (for customer identity resolver later)
- pytest, pytest-cov in dev dependencies

Initialise a git repo, make the first commit "chore: initial repo skeleton".
```

**Verify:**

```bash
make install
make lint
make typecheck
make test  # should pass with zero tests
git log --oneline  # should show initial commit
```

---

## Step 0.2 — CI pipeline

**Goal**: GitHub Actions workflow that runs lint, typecheck, and tests on every push.

**Prompt to Claude Code:**

```
Create .github/workflows/ci.yml that runs on every push and pull request to main.

It should:
- Set up Python 3.11
- Install dependencies via the Makefile
- Run lint, typecheck, and test targets
- Fail the build if any target fails
- Cache the venv between runs

Also create a CONTRIBUTING.md that explains:
- Branch naming: feat/sprint{N}-{component}
- No direct pushes to main
- Each PR must pass CI before merge
- Each PR should include tests
```

**Verify:**

```bash
git checkout -b feat/sprint1-ci
git push -u origin feat/sprint1-ci
# Watch the CI run pass on GitHub
```

---

# Sprint 1: Foundations + Spine

## Step 1.1 — Canonical transaction schema

**Goal**: Lock the data contract that every agent depends on. This is the single most important step in the entire project — get it right and everything else falls into place.

**Context**: The canonical record is the boundary between Layer 1 (ingestion) and everything downstream. Once data is in this shape, no downstream agent needs to know what bank or format it came from.

**Prompt to Claude Code:**

```
Create schema/canonical.py with the canonical transaction model using pydantic.

CanonicalTransaction must have:
- transaction_id: str (stable unique identifier within an analysis)
- date: datetime.date (transaction date, normalised)
- description: str (raw description preserved exactly)
- debit: Decimal | None (positive number, or None if this is a credit row)
- credit: Decimal | None (positive number, or None if this is a debit row)
- balance: Decimal (running balance after this transaction)
- currency: str (default "INR")
- source_reference: SourceReference (where in the original file this came from)

Enrichment slots (all optional, filled by later agents):
- category: TransactionCategory | None (enum: REVENUE, VENDOR_PAYMENT, SALARY, LOAN_IN, LOAN_OUT, TAX, TRANSFER, FOUNDER_WITHDRAWAL, FEES, OTHER)
- counterparty: str | None (cleaned counterparty name)
- is_recurring: bool | None
- is_related_party: bool | None
- related_party_match: str | None (which affiliate it matched)
- customer_id: str | None (stable customer identifier from customer identity resolver)
- anomaly_flags: list[str] (default empty list; risk detectors append here)

SourceReference must have:
- file_path: str
- page: int
- row: int (zero-indexed within the page)
- raw_text: str (the original row text for debugging)

Also create:
- StatementDocument wrapping a list of CanonicalTransaction plus metadata (account_id, statement_period_start, statement_period_end, source_format, bank_name, validation_status)
- A schema/CHANGELOG.md file documenting this as v1, frozen, with the rule that any field changes require a version bump

Write comprehensive tests in tests/test_canonical_schema.py covering:
- All fields populate correctly
- Validation rejects invalid combinations (e.g. both debit and credit set, neither set)
- Serialization to JSON and back preserves data
- The customer_id field defaults to None
```

**Verify:**

```bash
make test
# All schema tests pass
cat schema/CHANGELOG.md  # v1 documented
```

**Commit before moving on.**

---

## Step 1.2 — Synthetic statement generator

**Goal**: A tool that produces realistic Indian bank statement PDFs with paired ground-truth JSON. This is your test data foundation — without it, you can't validate anything.

**Context**: You can't download bank statements from Kaggle. Synthetic data with known ground truth is how you bootstrap. Build a generator that produces statements for HDFC and ICICI first (the two banks you'll target in Sprint 1).

**Prompt to Claude Code:**

```
Build tools/synthetic_gen.py — a synthetic Indian bank statement generator.

Requirements:

1. Bank templates: HDFC and ICICI initially. For each, define:
   - Header layout (bank logo placement, account number format, statement period)
   - Column layout (HDFC has [Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt., Deposit Amt., Closing Balance]; ICICI has different column order — research from real HDFC and ICICI sample PDFs)
   - Footer (page numbers, balance summary)

2. Company profiles (5 types):
   - healthy_saas: recurring monthly customer payments from 30-50 customers, growing 3% MoM, low burn, runway 18+ months
   - burning_startup: high vendor payments, increasing salaries, customer count flat or declining, runway < 6 months
   - services_firm: 5-15 large customer payments per month, lumpy revenue, variable burn
   - ecommerce: aggregated payment processor payouts (Razorpay/Cashfree), high vendor outflows
   - restaurant: lots of small daily cash deposits, vendor payments to food suppliers, GST outflows

3. Red-flag scenarios (injectable into any profile):
   - structuring: cluster of cash deposits just under ₹2 lakh
   - round_tripping: large outflow followed by inflow from same counterparty 3-7 days later
   - founder_extraction: monthly withdrawals to "founder personal" exceeding declared salary by 3x
   - customer_churn: 20% of recurring customers stop paying in the second half of the statement period
   - revenue_concentration: 70%+ of revenue from one counterparty
   - related_party_leakage: monthly payments to a counterparty with the same surname as the founder

4. Output for each generated statement:
   - A PDF file in data/synthetic/{profile}/{statement_id}.pdf
   - A paired ground_truth JSON in data/synthetic/{profile}/{statement_id}.truth.json containing:
     - All transactions with their TRUE category, customer_id, related_party flag
     - Expected metrics (true burn rate, true runway, true customer count)
     - Injected red flags with the exact transaction IDs that should be detected

5. CLI interface:
   - python -m tools.synthetic_gen --bank hdfc --profile healthy_saas --count 10 --output data/synthetic/

Use reportlab or weasyprint for PDF generation. Match real bank statement layouts closely — they will be parsed by pdfplumber later, so the table structure needs to be extractable.

Write tests in tests/test_synthetic_gen.py:
- Generated PDFs exist and have at least 1 page
- Ground truth JSON parses and contains all expected fields
- Balance continuity holds in generated statements (every running balance is correct)
- Red flag scenarios produce the expected transaction patterns
```

**Verify:**

```bash
python -m tools.synthetic_gen --bank hdfc --profile healthy_saas --count 5 --output data/synthetic/
python -m tools.synthetic_gen --bank icici --profile burning_startup --count 5 --output data/synthetic/
ls data/synthetic/
# Open one of the generated PDFs and visually verify it looks like a bank statement
```

**Generate ~50 synthetic statements across all 5 profiles and both banks before moving on.**

---

## Step 1.3 — Collect real samples + hand-label gold set

**Goal**: Have at least 15 hand-labelled statements (mix of synthetic and real anonymised) as the gold-standard regression set.

**This step is mostly manual work — Claude Code can only help so much.**

**What to do yourself:**

1. Anonymise your own bank statements (last 3 months) and 2-3 friends' (with consent)
2. For each real statement, manually create a ground-truth JSON in `data/gold/labels/{statement_id}.truth.json` with the same structure as the synthetic ones
3. Pick 10 synthetic + 5 real to be the gold set
4. Copy them to `data/gold/` directory

**Prompt to Claude Code for the labelling helper:**

```
Create tools/labelling_helper.py — a CLI tool that:

1. Takes a PDF path
2. Extracts transactions using pdfplumber (rough extraction, will be wrong sometimes)
3. Opens an interactive prompt where I can review each transaction and assign:
   - Category (from the enum)
   - Customer_id (let me type one or pick from already-assigned)
   - Related-party flag
4. Saves the result as a ground_truth JSON matching the schema

This is just to speed up my manual labelling, not to be accurate. I will verify and correct the output.
```

**Verify:**

```bash
ls data/gold/        # 15+ PDFs
ls data/gold/labels/ # 15+ matching .truth.json files
```

---

## Step 1.4 — Gold-standard regression test runner

**Goal**: A test that runs the full pipeline against the gold set and reports accuracy. This becomes the regression guard for the rest of the project.

**Prompt to Claude Code:**

```
Create tests/regression/test_gold_set.py — a pytest test module.

Behaviour:
1. Discover all PDFs in data/gold/
2. For each PDF, run the full pipeline (once Sprint 1 is built — for now, just stub it)
3. Compare the output against data/gold/labels/{statement_id}.truth.json
4. Report:
   - Transaction extraction accuracy (% of true transactions correctly extracted)
   - Categorisation accuracy (% of correctly categorised transactions)
   - Customer ID resolution accuracy (% of true customer groupings preserved)
   - Risk detector recall (for gold statements with injected flags)

The test passes if all accuracies meet the current sprint's targets. Sprint 1 targets:
- Extraction ≥88%
- Categorisation ≥82%
- (Other metrics not yet applicable)

Output a structured report to results/sprint{current}/gold_regression.json with all metrics.

For now, stub the pipeline call — return empty results. The test should run but report 0% accuracy. The test will fail until Sprint 1 components are built; that's expected and fine.

Add a Makefile target: regression: pytest tests/regression/
```

**Verify:**

```bash
make regression
# Test runs, reports 0% accuracy, fails — expected
```

---

## Step 1.5 — PDF digital adapter

**Goal**: Parse a digital (non-scanned) Indian bank statement PDF into raw extracted rows.

**Prompt to Claude Code:**

```
First, benchmark pdfplumber, pymupdf, and tabula-py on 5 sample PDFs from data/synthetic/hdfc/ and data/synthetic/icici/. Write tools/benchmark_pdf_libs.py that:
- Runs each library on each PDF
- Reports rows extracted, time taken, and whether the extraction looks structurally correct
- Prints a recommendation

Then implement adapters/pdf_digital.py based on the benchmark winner (likely pdfplumber for table-heavy bank statements).

Requirements:

1. Define an Adapter protocol/abstract base class in adapters/__init__.py:
   - extract(file_path: str) -> RawExtractionResult
   - RawExtractionResult contains: raw_rows (list of dicts with detected column names as keys), source_metadata (file path, page count, detected bank if possible)

2. Implement DigitalPdfAdapter:
   - Detect tables per page
   - Extract cells and group into rows by vertical alignment
   - Handle multi-page statements (skip repeated headers on continuation pages)
   - Preserve source coordinates so traceability is possible later
   - Detect bank name from the header if possible (HDFC, ICICI, SBI, Axis, Kotak — string-match in the first page text)
   - Return raw rows with column names as detected (no normalisation yet — that's the normaliser's job)

3. Tests in tests/test_adapters.py:
   - Adapter extracts the correct number of rows from each synthetic PDF in the gold set
   - Multi-page statements are handled (no duplicate header rows)
   - The bank is detected correctly for HDFC and ICICI synthetic statements

The adapter MUST NOT interpret meaning. It must not decide which column is "date" or "debit". It only extracts.
```

**Verify:**

```bash
python -m tools.benchmark_pdf_libs
pytest tests/test_adapters.py -v
```

---

## Step 1.6 — Normaliser + first 2 Indian bank profiles

**Goal**: Map raw extracted rows from HDFC and ICICI statements into canonical transaction records.

**Prompt to Claude Code:**

```
Implement pipeline/normaliser.py + profiles/ for Indian banks.

Architecture:

1. profiles/ contains one YAML or Python file per bank. Each profile defines:
   - bank_name: "HDFC" or "ICICI"
   - column_mapping: dict mapping raw column names (as extracted) to canonical fields
     - e.g. for HDFC: {"Date": "date", "Narration": "description", "Withdrawal Amt.": "debit", "Deposit Amt.": "credit", "Closing Balance": "balance"}
   - date_format: e.g. "%d/%m/%y" for HDFC
   - debit_credit_convention: "separate_columns" (most common) or "signed_amount"
   - currency: "INR"

2. profiles/hdfc.yaml and profiles/icici.yaml — create these based on the synthetic generator templates

3. pipeline/normaliser.py:
   - Normaliser class with method: normalise(raw_result: RawExtractionResult, bank_name: str | None = None) -> StatementDocument
   - If bank_name is given, load that profile directly
   - If bank_name is None or unknown, fall back to model-assisted profile inference (placeholder for now — just raise NotImplementedError with a TODO comment; we'll implement this later in Sprint 1 or Sprint 2)
   - Apply the column mapping to each raw row, parse dates, parse amounts, generate transaction_ids (UUID4), populate source_reference
   - Return a StatementDocument with all canonical transactions

4. Tests in tests/test_normaliser.py:
   - HDFC synthetic statement normalises to canonical with all transactions present
   - ICICI synthetic statement normalises correctly with the different column order
   - Date parsing handles both DD/MM/YY and DD/MM/YYYY formats
   - Amount parsing handles Indian number format ("1,00,000.50")
   - transaction_id is unique across all transactions in a statement
   - source_reference points back to the correct page and row

Currency formatting note: Indian statements use ₹ and comma-separated lakhs/crores like "1,00,000.50" (one lakh fifty paise). Parsing must handle this.
```

**Verify:**

```bash
pytest tests/test_normaliser.py -v
```

---

## Step 1.7 — Validator

**Goal**: The primary defence against silent data corruption. Verify balance continuity.

**Prompt to Claude Code:**

```
Implement pipeline/validator.py.

The Validator class:
- validate(document: StatementDocument) -> ValidationResult
- ValidationResult has: passed (bool), errors (list of ValidationError), warnings (list of ValidationWarning)
- ValidationError has: error_type, description, transaction_id (optional), row_reference (optional)

Checks to perform:

1. Balance continuity (CRITICAL):
   - For each transaction after the first, check that balance == previous_balance + (credit or 0) - (debit or 0)
   - If mismatch beyond a tolerance of 0.01 (paise rounding), record a ValidationError with the exact discrepancy

2. Date continuity:
   - Dates should be non-decreasing
   - Gaps > 60 days within the statement period are flagged as warnings (might indicate missing pages)

3. Duplicate detection:
   - Identical (date, description, debit, credit) tuples are warnings (not errors — could be legitimate repeated transactions)

4. Missing critical data:
   - Transaction with neither debit nor credit set: error
   - Transaction with both debit and credit set: error
   - Date in the future: error

If any error is recorded, validation fails. Pipeline must halt with the validation result so the user sees the precise issue.

Tests in tests/test_validator.py:
- Clean synthetic statement passes validation
- Deliberately corrupt a transaction's amount and verify the validator catches the balance break
- Test all error types above with synthetic counterexamples
- Test that the validator produces row references that point back to the correct source
```

**Verify:**

```bash
pytest tests/test_validator.py -v
```

---

## Step 1.8 — Categoriser

**Goal**: LLM-driven categorisation with constrained vocabulary and caching.

**Prompt to Claude Code:**

```
Implement pipeline/categoriser.py.

The Categoriser class:
- categorise(document: StatementDocument) -> StatementDocument (with category field populated on each transaction)

Implementation:

1. Cache layer:
   - Hash each transaction's description (after stripping common prefixes like "NEFT/", "RTGS/", "UPI/" and reference numbers)
   - If hash exists in cache (file-based JSON cache at .cache/categoriser_cache.json), use cached category
   - Otherwise, call the LLM and cache the result

2. LLM call:
   - Use anthropic SDK with Claude Sonnet
   - System prompt explains the task: categorise an Indian bank transaction description into one of REVENUE, VENDOR_PAYMENT, SALARY, LOAN_IN, LOAN_OUT, TAX, TRANSFER, FOUNDER_WITHDRAWAL, FEES, OTHER
   - Include 10-15 few-shot examples covering Indian transaction descriptions (NEFT, IMPS, UPI, ECS, salary credits, GST payments, etc.)
   - For efficiency, batch transactions: send up to 20 descriptions per LLM call, parse the JSON response

3. Counterparty extraction:
   - In the same LLM call, ask for the counterparty name (stripped of prefixes and noise)
   - Populate the counterparty field

4. Recurring detection:
   - After categorisation, group by (counterparty, category) and mark transactions as recurring if the same combination appears 3+ times with regular intervals (monthly, quarterly)

Tests in tests/test_categoriser.py:
- Categoriser produces results for all transactions in a statement
- Cache works: second run on the same statement makes zero LLM calls (mock the LLM to verify)
- Categories are within the constrained vocabulary
- Spot-check: salary credits, GST debits, founder withdrawals get the right category

Add Anthropic API key handling via environment variable ANTHROPIC_API_KEY.
```

**Verify:**

```bash
export ANTHROPIC_API_KEY=...
pytest tests/test_categoriser.py -v
```

---

## Step 1.9 — Financial-health analyst

**Goal**: Compute burn rate, runway, revenue trend. Pure arithmetic, no LLM.

**Prompt to Claude Code:**

```
Implement analysis/financial_health.py.

The FinancialHealthAnalyst class:
- analyse(document: StatementDocument) -> FinancialHealthMetrics

FinancialHealthMetrics contains:
- statement_period: tuple[date, date]
- months_covered: int
- monthly_burn_rate: Decimal (average monthly outflow categorised as non-transfer expenses)
- monthly_revenue: Decimal (average monthly inflow categorised as REVENUE)
- revenue_trend_slope: float (slope of monthly revenue regressed against month number; positive = growing)
- revenue_mom_growth_rate: float (most recent month vs previous month, as a fraction)
- current_balance: Decimal (final balance in the statement)
- runway_months: float (current_balance / monthly_burn_rate, or infinity if burn is zero or negative)
- monthly_breakdown: list of MonthlyMetrics (per-month inflow, outflow, net, end-of-month balance)
- seasonality_score: float (coefficient of variation of monthly revenue — high = lumpy)

Implementation notes:
- Use Decimal for all monetary arithmetic, not float
- Burn rate excludes inter-account transfers (category TRANSFER) and loan inflows (LOAN_IN)
- Revenue is strictly category REVENUE
- For periods less than 2 months, set revenue_trend_slope to None
- Linear regression: use numpy.polyfit with degree 1

Tests in tests/test_financial_health.py:
- Hand-calculate burn rate and runway for a small synthetic statement, verify exact match
- Test with a multi-month statement that the monthly breakdown adds up to the totals
- Test that revenue trend is positive for the "healthy_saas" profile and roughly flat for "burning_startup"
- Test edge cases: zero revenue, zero burn, single month of data
```

**Verify:**

```bash
pytest tests/test_financial_health.py -v
```

---

## Step 1.10 — Orchestrator v1

**Goal**: Sequential execution of all Sprint 1 components with intermediate state persistence.

**Prompt to Claude Code:**

```
Implement pipeline/orchestrator.py.

The Orchestrator class:
- run(file_path: str) -> AnalysisResult

Behaviour:
1. Detect file type (only PDF supported in Sprint 1; raise NotImplementedError for others)
2. Select adapter (DigitalPdfAdapter)
3. Run extract → persist raw result to .pipeline_state/{run_id}/01_raw.json
4. Run normaliser → persist canonical document to .pipeline_state/{run_id}/02_canonical.json
5. Run validator → if validation fails, halt with the ValidationResult and persist .pipeline_state/{run_id}/02b_validation_failure.json
6. Run categoriser → persist .pipeline_state/{run_id}/03_categorised.json
7. Run financial-health analyst → persist .pipeline_state/{run_id}/04_health.json
8. Return AnalysisResult containing all intermediate outputs

AnalysisResult has:
- run_id: str (UUID4)
- timestamp: datetime
- input_file: str
- statement_document: StatementDocument
- financial_health: FinancialHealthMetrics
- (other fields will be added in later sprints)

Error handling:
- Any exception during a stage attributes the error to that stage in the AnalysisResult
- Validation failures are NOT exceptions — they're a clean halt with an explanatory result

Tests in tests/test_orchestrator.py:
- End-to-end test: run on a synthetic statement, verify all intermediate state files exist
- Test that validation failure halts the pipeline at stage 02b and stages 03+ never run
- Test that the run_id is unique per invocation
```

**Verify:**

```bash
pytest tests/test_orchestrator.py -v
python -c "from pipeline.orchestrator import Orchestrator; r = Orchestrator().run('data/synthetic/hdfc/healthy_saas_001.pdf'); print(r.financial_health)"
```

---

## Step 1.11 — Angel lens report generator

**Goal**: One-page PDF + XLSX showing key numbers and findings.

**Prompt to Claude Code:**

```
Implement reports/angel_lens.py.

The AngelLensReport class:
- generate(result: AnalysisResult) -> tuple[bytes, bytes]  # (pdf_bytes, xlsx_bytes)

The PDF (one page):
- Header: company name (extracted from statement metadata if available, else "Statement Analysis"), statement period
- Key numbers section: monthly burn, monthly revenue, current balance, runway in months
- Runway gauge: a simple bar visualization (red < 6 months, yellow 6-12, green > 12)
- Revenue trend mini-chart: monthly revenue line chart, last 6 months
- Top findings section: top 3 items from the financial health metrics that the user should know about (e.g., "burn rate up 20% over the period", "revenue declined in last 2 months", "high cash balance — 24 months runway")

Use Jinja2 for HTML template + weasyprint for PDF conversion. Keep the design clean and professional — single-page, single-accent-colour, plenty of whitespace.

The XLSX (matching content but structured for editing):
- Sheet 1: Summary (key numbers)
- Sheet 2: Monthly breakdown (one row per month with inflow, outflow, net, balance)

Use openpyxl. Format numbers as Indian rupees with commas.

Tests in tests/test_angel_lens.py:
- Report generates without error on a sample AnalysisResult
- PDF is a valid PDF file (check magic bytes)
- XLSX opens and contains expected sheets

Save to a templates/ subdirectory under reports/ so the HTML template is editable.
```

**Verify:**

```bash
pytest tests/test_angel_lens.py -v
# Generate a real angel lens and open it
python -c "
from pipeline.orchestrator import Orchestrator
from reports.angel_lens import AngelLensReport
r = Orchestrator().run('data/synthetic/hdfc/healthy_saas_001.pdf')
pdf, xlsx = AngelLensReport().generate(r)
open('out.pdf', 'wb').write(pdf)
open('out.xlsx', 'wb').write(xlsx)
"
open out.pdf
```

---

## Step 1.12 — Streamlit demo UI

**Goal**: A minimal upload-and-see-results interface.

**Prompt to Claude Code:**

```
Implement demo/streamlit_app.py.

A single Streamlit page that:
1. Has a file uploader accepting PDF
2. On upload, runs the orchestrator
3. Shows a progress indicator per pipeline stage
4. On success, displays the angel lens PDF embedded in the page
5. Provides download buttons for the PDF and XLSX
6. On validation failure, displays the error clearly with the row reference

Style: simple, no heavy theming. Focus on functionality.

Add a make target: demo: streamlit run demo/streamlit_app.py
```

**Verify:**

```bash
make demo
# Browser opens, upload data/synthetic/hdfc/healthy_saas_001.pdf, see the angel lens
```

---

## Sprint 1 closeout

Run the regression suite:

```bash
make regression
```

Check that the gold-standard accuracy targets are met:
- Extraction ≥ 88%
- Categorisation ≥ 82%

If not, iterate on the categoriser prompt or the PDF adapter until they are. Do not move to Sprint 2 with red metrics.

Record a 60-second demo video and save to `results/sprint1/demo.mp4`. Generate `results/sprint1/validation.json` from the regression run.

Tag the commit: `git tag sprint1-complete`

---

# Sprint 2: Risk + Customer Analytics + VC Lens

## Step 2.1 — Related-party tagger

**Goal**: Identify transactions where the counterparty is related to the investee company.

**Prompt to Claude Code:**

```
Implement pipeline/related_party.py.

The RelatedPartyTagger class:
- tag(document: StatementDocument, affiliates: list[Affiliate]) -> StatementDocument (with is_related_party and related_party_match populated)

Affiliate is a pydantic model with:
- name: str
- relationship: str (e.g. "director", "spouse_of_founder", "sister_company")
- aliases: list[str] (alternate spellings)

Two-stage matching:

1. Deterministic exact match (case-insensitive, after stripping common noise):
   - Strip prefixes (NEFT/, RTGS/, IMPS/, UPI/), reference numbers, legal-form suffixes (PVT LTD, LLP, PRIVATE LIMITED, CO LTD)
   - Strip extra whitespace, lowercase
   - Compare cleaned counterparty against cleaned affiliate names + aliases
   - If exact match, set is_related_party=True, related_party_match=affiliate.name

2. LLM fuzzy match for unmatched transactions:
   - Batch the unmatched (counterparty, affiliates_list) pairs
   - Ask Claude: "Is this counterparty plausibly the same entity as one of these affiliates? Respond JSON: {match: affiliate_name or null, confidence: 0-1}"
   - If confidence >= 0.8, mark as related party

Tests:
- Exact match catches "Acme Pvt Ltd" against affiliate "Acme Private Limited"
- Fuzzy match catches "ACME PL" against affiliate "Acme Private Limited"
- Non-matching counterparties remain is_related_party=False
- Empty affiliates list produces no flags
```

**Verify:**

```bash
pytest tests/test_related_party.py -v
```

---

## Step 2.2 — Customer identity resolver (NEW)

**Goal**: Group all revenue transactions from the same underlying customer, even when names appear in different forms.

**Prompt to Claude Code:**

```
Implement pipeline/customer_identity.py.

The CustomerIdentityResolver class:
- resolve(document: StatementDocument) -> StatementDocument (with customer_id populated on all REVENUE transactions)

Three-stage resolution:

Stage 1 — Normalisation:
- Define a function clean_counterparty(name: str) -> str
- Strip prefixes (NEFT/, RTGS/, IMPS/, UPI/, BY TRANSFER FROM, INWARD CLEARING)
- Strip trailing reference numbers and invoice patterns (regex for /INV\d+, /TXN\d+, etc.)
- Strip legal-form suffixes (PVT LTD, LLP, PRIVATE LIMITED, CO LTD, INC, LTD)
- Collapse multiple spaces, lowercase, trim

Stage 2 — Exact match clustering:
- For all REVENUE transactions, compute cleaned_name
- Group by cleaned_name — all transactions in the same group get the same customer_id
- Generate customer_id as "cust_" + short hash of the canonical cleaned_name

Stage 3 — Embedding-based merging:
- For each remaining cleaned_name (one per cluster from stage 2), compute a sentence-transformers embedding using the model "all-MiniLM-L6-v2" (lightweight, free, runs locally)
- Compute pairwise cosine similarity between all cluster representatives
- For pairs with similarity > 0.85, merge clusters (assign the same customer_id)
- Use a simple union-find or networkx connected_components for the merging

Cache embeddings to .cache/embeddings.json keyed by cleaned_name string.

Output: every REVENUE transaction has customer_id set.

Tests in tests/test_customer_identity.py:
- "Acme Pvt Ltd", "Acme Private Limited", "ACME PL" all resolve to the same customer_id
- "NEFT/SBIN0000001/ACME PVT LTD/INV-447" resolves to the same as plain "Acme Pvt Ltd"
- Different customers ("Acme" vs "Apex") get different customer_ids
- Non-revenue transactions have customer_id=None
- Validate against gold set's hand-labelled customer groupings — accuracy ≥ 90%
```

**Verify:**

```bash
pytest tests/test_customer_identity.py -v
```

---

## Step 2.3 — Risk / red-flag analyst

**Goal**: Six independent detectors for suspicious patterns.

**Prompt to Claude Code:**

```
Implement analysis/risk.py.

Each detector is a separate function returning list[Flag]. A Flag has:
- detector_name: str
- severity: Severity enum (LOW, MEDIUM, HIGH)
- triggering_transaction_ids: list[str]
- description: str (plain English)
- evidence: dict (extra metadata like the threshold breached, the cluster size, etc.)

Detectors:

1. detect_structuring(document) — look for clusters of ≥4 transactions within a 7-day window, all amounts between ₹1.5L and ₹2L (just under §269ST limit)

2. detect_round_tripping(document) — find pairs of (outflow, inflow) where:
   - Same counterparty (counterparty field match)
   - Inflow occurs 3-14 days after outflow
   - Amounts match within 5% tolerance
   - Flag the pair

3. detect_spikes_drains(document) — for each month, compute z-score of net flow vs baseline. Months with |z| > 2.5 are flagged. Baseline = mean and stdev of all months EXCEPT the one being tested.

4. detect_customer_concentration(document) — group revenue by customer_id, compute top-3 share. If > 60%, flag MEDIUM. If > 80%, flag HIGH.

5. detect_round_amount_clustering(document) — count transactions where amount % 1000 == 0. If this proportion > 30% of revenue transactions, flag (real businesses have irregular amounts).

6. detect_founder_over_extraction(document, declared_founder_salary: Decimal | None) — sum FOUNDER_WITHDRAWAL transactions per month. If > 3x declared salary (or > ₹5L/month if no salary declared), flag.

Plus a RiskAnalyst class that runs all detectors and produces a RiskReport:
- flags: list[Flag]
- composite_score: float (weighted sum: HIGH=10, MEDIUM=5, LOW=2, capped at 100)
- narrative: str (LLM-generated plain English explanation composing the flags into a story — implement this with a Claude call that gets all the flags and writes a coherent summary)

Tests:
- Each detector tested independently with synthetic statements that should trip it
- False positive rate on healthy synthetic statements ≤ 8%
- Recall on injected red-flag scenarios ≥ 85%
```

**Verify:**

```bash
pytest tests/test_risk.py -v
```

---

## Step 2.4 — Customer analytics analyst (NEW)

**Goal**: Compute churn, NRR, cohort retention, concentration trajectory.

**Prompt to Claude Code:**

```
Implement analysis/customer_analytics.py.

The CustomerAnalyticsAnalyst class:
- analyse(document: StatementDocument) -> CustomerAnalyticsReport

CustomerAnalyticsReport contains:
- monthly_active_customers: dict[str, int]  # month_key -> distinct customer count
- monthly_active_trend: float  # linear regression slope
- churn_events: list[ChurnEvent]
- new_acquisitions: dict[str, list[str]]  # month_key -> list of customer_ids first seen that month
- nrr_per_month: dict[str, float]  # month_key -> NRR ratio (1.0 = flat, > 1 = expansion)
- cohort_retention: dict[str, dict[int, float]]  # cohort_month -> {months_since_first_payment: retention_pct}
- concentration_trajectory: list[ConcentrationPoint]  # one per month, with top3_share and top10_share
- payment_regularity_alerts: list[RegularityAlert]

ChurnEvent:
- customer_id: str
- last_payment_date: date
- previous_avg_monthly_spend: Decimal
- months_active_before_churn: int

Algorithm:

1. Filter REVENUE transactions with customer_id set
2. Build customer-month matrix: dict[(customer_id, month_key), total_payment]
3. Active customer count per month = count of customers with payment > 0 in that month
4. Churn detection: a customer is churned if they paid in some month M but not in any of months M+1, M+2, M+3 (3-month silence)
5. NRR: for each consecutive month pair (M, M+1), filter to customers active in M; sum payments in M; sum payments in M+1 from those same customers; ratio = M+1 sum / M sum
6. Cohort retention: assign each customer to a cohort by their first-payment month; for each cohort, compute retention curve (% still paying at N months after first payment)
7. Concentration trajectory: per month, sort customers by that month's payment desc, compute top-3 and top-10 share
8. Payment regularity: for customers active >= 6 months, detect cadence (modal gap between payments). Alert if recent gap > 2x modal gap.

Use Decimal for monetary, but float for ratios/percentages.

Tests in tests/test_customer_analytics.py:
- Synthetic statement with 30 customers, 5 of whom churn → churn list contains exactly those 5
- Manually construct a customer-month matrix, hand-compute NRR, verify exact match
- Cohort retention for a "healthy_saas" profile shows high retention
- Cohort retention for "burning_startup" with injected churn shows declining cohorts
- Concentration trajectory rises over time in a profile where a big customer's share grows
```

**Verify:**

```bash
pytest tests/test_customer_analytics.py -v
```

---

## Step 2.5 — VC lens report generator

**Goal**: Multi-sheet workbook covering financial health, risk, customer analytics, related parties.

**Prompt to Claude Code:**

```
Implement reports/vc_lens.py.

The VCLensReport class:
- generate(result: AnalysisResult) -> tuple[bytes, bytes]  # (xlsx_bytes, pdf_summary_bytes)

The XLSX has six sheets:

Sheet 1 — Summary:
- Key financial metrics (burn, runway, revenue, growth)
- Composite risk score (out of 100)
- Customer health snapshot (active customers, churn rate, NRR)
- Top 5 findings highlights

Sheet 2 — Financial Health:
- Monthly breakdown table: month, inflow, outflow, net, end balance, revenue, burn
- Charts: revenue trend, burn trend, balance trend

Sheet 3 — Red Flags:
- One row per detected flag
- Columns: detector_name, severity, description, triggering transactions (as a comma-separated list of transaction_ids), date range

Sheet 4 — Customer Analytics:
- Active customer count per month (chart)
- Churn list: customer_id, last_payment_date, previous_avg_monthly_spend, months_active
- NRR per month
- Cohort retention matrix (pivot table)
- Concentration trajectory chart (top-3 share per month)

Sheet 5 — All Transactions:
- Full enriched transaction list with category, customer_id, related-party flag, anomaly flags

Sheet 6 — Related Parties:
- Filtered view of is_related_party=True transactions
- Aggregated by related_party_match (which affiliate)

PDF summary is a 2-page condensed version for partners who don't want the workbook.

Use openpyxl for XLSX. Format Indian currency throughout. Apply conditional formatting (red for negative growth, green for NRR > 100%, etc.).

Tests in tests/test_vc_lens.py:
- Generates without error on a sample AnalysisResult
- All 6 sheets present
- Numbers in summary sheet match the underlying data
```

**Verify:**

```bash
pytest tests/test_vc_lens.py -v
```

---

## Step 2.6 — Categoriser refinement + Sprint 2 closeout

**Goal**: Bump categorisation accuracy to ≥88% using Sprint 1 errors as training input.

**Prompt to Claude Code:**

```
Improve pipeline/categoriser.py based on Sprint 1 errors:

1. Run the regression suite and identify misclassified transactions
2. Add those as additional few-shot examples in the categoriser prompt
3. If a category is consistently wrong, consider:
   - Expanding the category vocabulary (e.g. split FEES into BANK_FEES and OTHER_FEES)
   - Adding category-specific keyword hints in the prompt
4. Re-run regression and verify categorisation accuracy ≥ 88%
5. Document the categoriser as v2 in a comment header
```

Update the orchestrator to include all Sprint 2 stages in sequence.

Run the regression suite. Verify Sprint 2 targets:
- Risk detector recall ≥ 85%
- Customer identity accuracy ≥ 90%
- Customer churn detection ≥ 90%
- Related-party recall ≥ 92%
- Categorisation accuracy ≥ 88%

Tag the commit: `git tag sprint2-complete`

---

# Sprint 3: Compliance + Reconciliation + Workbench + Formats

## Step 3.1 — Indian jurisdiction module

**Prompt to Claude Code:**

```
Implement jurisdictions/india/rules.py.

Define a JurisdictionModule interface in jurisdictions/__init__.py:
- name: str (e.g. "india")
- rules: list[ComplianceRule]

ComplianceRule has:
- rule_id: str
- name: str
- regulatory_citation: str (e.g. "Income Tax Act §269ST")
- severity: Severity
- description: str
- evaluate(document: StatementDocument) -> list[ComplianceException]

Implement Indian rules:

1. cash_transaction_limit_269st: flag cash transactions > ₹2 lakh per day per counterparty (§269ST)
2. gst_payment_consistency: companies with revenue > ₹40 lakh should have monthly GST outflows. Flag absence.
3. tds_pattern_expectation: salary outflows should have matching TDS outflows. Flag absence if salaries > threshold.
4. pmla_high_value_aggregation: aggregate cash transactions ≥ ₹10 lakh in a 30-day window — STR reporting threshold
5. related_party_transaction_limit: Companies Act limits on related-party transactions without board approval

For each rule, write a thorough evaluate() function with comments explaining the regulatory basis. Cite section numbers explicitly.

Tests in tests/test_india_compliance.py:
- Each rule tested with a synthetic statement that violates it
- Each rule tested with a healthy statement (should produce zero flags)
```

---

## Step 3.2 — Compliance analyst

**Prompt to Claude Code:**

```
Implement analysis/compliance.py.

The ComplianceAnalyst class:
- analyse(document: StatementDocument, jurisdiction_module: JurisdictionModule) -> ComplianceReport

ComplianceReport:
- exceptions: list[ComplianceException]
- by_severity: dict[Severity, int]
- jurisdiction: str
- module_version: str

Each ComplianceException has:
- rule_id, regulatory_citation, severity, description (from the rule)
- triggering_transaction_ids
- investor_risk_framing: str (the LLM-generated "this exposes investors to X" version of the description)

Generate investor_risk_framing via Claude — take the technical compliance description and reframe it as investor risk.

Tests: verify Indian rules trip on injected violations.
```

---

## Step 3.3 — Reconciliation analyst

**Prompt to Claude Code:**

```
Implement analysis/reconciliation.py.

The ReconciliationAnalyst class:
- analyse(document: StatementDocument, claims: CompanyClaims, customer_analytics: CustomerAnalyticsReport) -> ReconciliationReport

CompanyClaims (pydantic):
- declared_revenue_total: Decimal | None
- declared_customer_count: int | None
- declared_nrr: float | None
- declared_loans: list[LoanClaim] (each with amount, date, lender)
- declared_headcount: int | None
- declared_avg_salary: Decimal | None
- never_lost_customer: bool | None
- pitch_deck_pdf_path: str | None  # if provided, extract claims from this

If pitch_deck_pdf_path is provided:
- Use Claude to extract claims from the deck via a structured prompt
- Populate the CompanyClaims fields from the extraction

Reconciliation checks:
1. Declared revenue vs sum of REVENUE transactions — flag if delta > 10%
2. Declared customer count vs distinct customer_ids — flag if delta > 15%
3. Declared NRR vs computed NRR — flag if absolute delta > 0.1
4. "Never lost a customer" vs detected churn list — flag any churn if claimed
5. Declared loans vs LOAN_IN transactions — flag missing or extra loans
6. Declared headcount × avg salary × 12 vs annual SALARY outflows — flag if delta > 20%

Each finding has magnitude and direction (over- or under-reported).

Tests: verify each check trips on synthetic claim mismatches.
```

---

## Step 3.4 — Workbench lens

**Prompt to Claude Code:**

```
Implement reports/workbench_lens.py.

The deepest lens. Multi-sheet XLSX with:

Sheet 1 — Executive Summary
Sheet 2 — Financial Health (same as VC lens)
Sheet 3 — Red Flags (same)
Sheet 4 — Customer Analytics (same) + customer-by-customer drill-down sub-section
Sheet 5 — Compliance Findings
Sheet 6 — Reconciliation Findings
Sheet 7 — All Transactions
Sheet 8 — Related Parties
Sheet 9 — Customer Master (one row per resolved customer_id with: first payment date, last payment date, total paid, payment count, status)
Sheet 10 — Raw Data Export (json column with full enriched transaction object, for fund's own modelling)

Plus:
- Custom detector spec: workbench includes a hook for adding fund-specific rules via a config file. Implement a basic version where the analyst can supply additional risk detectors as Python lambdas in workbench_config.py.
- Toggle which detectors run via the workbench_config.py.

Tests: workbench generates with all sheets, custom detector mechanism works.
```

---

## Step 3.5 — Versioning system

**Prompt to Claude Code:**

```
Implement pipeline/versioning.py.

Tag every AnalysisResult with:
- schema_version: str (from schema/CHANGELOG.md)
- agent_versions: dict[str, str] (per-agent semver from a registry)
- jurisdiction_module_version: str (if used)
- timestamp, run_id

Persistent result store: extend the .pipeline_state/ to be keyed by (document_hash, version_tuple). Old runs are preserved.

Replay capability:
- replay(run_id: str) -> AnalysisResult
- Loads the original input file, re-runs the pipeline with the CURRENT agent versions
- Returns a new AnalysisResult that can be diffed against the original

Diff function: diff_results(old: AnalysisResult, new: AnalysisResult) -> ResultDiff showing exactly what changed.

Tests: replay reproduces the original output if no code has changed; diff highlights when categoriser is updated.
```

---

## Step 3.6 — Additional format adapters

**Prompt to Claude Code (run as separate sessions for each adapter):**

```
Implement adapters/csv.py. Auto-detect delimiter and encoding. Detect header row. Return raw rows matching the Adapter protocol.
```

```
Implement adapters/excel.py. Handle multi-sheet workbooks. Detect which sheet has transactions (look for date-amount-balance patterns). Detect header row (often not row 1). Tolerate merged cells.
```

```
Implement adapters/scanned_pdf.py. Use Tesseract for OCR. Preprocess images: deskew, denoise, threshold. Report confidence per extracted cell. Same interface as other adapters.
```

```
Implement adapters/image.py. JPG/PNG of single statement page. Reuse the scanned_pdf OCR pipeline.
```

---

## Sprint 3 closeout

Verify all Sprint 3 targets in the regression suite:
- Compliance violation recall ≥ 90%
- Reconciliation mismatch recall ≥ 85%
- All 5 formats ingest correctly
- Workbench drill-down functional

Tag the commit: `git tag sprint3-complete`

---

# Sprint 4: Network Lens + AA Sandbox + Batch + Polish

## Step 4.1 — Network batch lens

**Prompt to Claude Code:**

```
Implement reports/network_lens.py.

Takes a list of AnalysisResults (one per company) and produces a cross-company comparison workbook:

Sheet 1 — Comparison Table:
- One row per company
- Columns: company_name, statement_period, monthly_burn, monthly_revenue, runway_months, revenue_growth, active_customers, churn_rate, NRR, top_customer_share, composite_risk_score, compliance_status (clean/warnings/violations), # of red flags

All columns sortable and filterable (Excel autofilter).

Sheet 2 — Risk Distribution: histogram of risk scores across the cohort
Sheet 3 — Sector Breakdown (if company sector metadata available)
Sheet 4 — Individual Company Links (each row links to per-company detail report)

Plus a 1-page cohort dashboard PDF summarising:
- Total companies analysed
- Median burn / runway / NRR
- Outliers (top and bottom 10% on key metrics)
- High-risk companies highlighted
```

---

## Step 4.2 — Account Aggregator sandbox adapter

**Prompt to Claude Code:**

```
Implement adapters/api_aa.py.

Connect to the AA sandbox (Sahamati ecosystem). This is essentially a proof-of-concept — production AA integration is post-Sprint 4.

Steps:
1. Use the AA sandbox endpoint URLs and test credentials
2. Implement the consent flow (consent handle + artifact for sandbox)
3. Fetch transactions for the sandbox account
4. Map the AA standard transaction schema to the canonical transaction record
5. Return a StatementDocument

The adapter must work end-to-end in the orchestrator: orchestrator.run(aa://test_account_123) detects the AA URL scheme, calls this adapter, runs the rest of the pipeline as normal.

This adapter is the proof that the pattern works for live data — not a production-ready integration.

Note: research the current Sahamati sandbox URLs and auth flow. If they require sign-up, document that as a prerequisite in docs/aa_setup.md.
```

---

## Step 4.3 — Batch processing infrastructure

**Prompt to Claude Code:**

```
Implement pipeline/batch.py.

The BatchOrchestrator class:
- run_batch(input_paths: list[str], concurrency: int = 4) -> BatchResult

Implementation:
- Use asyncio with a semaphore for concurrency control
- Each statement runs through the full orchestrator independently
- Failures in one statement do not affect others
- Progress reporting via a callback

BatchResult:
- results: dict[input_path, AnalysisResult | Exception]
- summary: aggregate statistics

Optional: Postgres-backed job queue for resumability. Implement only if straightforward — otherwise, in-memory is fine for the 8-week build.

Tests: batch of 20 statements completes; failure in one doesn't kill others.
```

---

## Step 4.4 — Final polish

```
Polish the demo UI:
- Lens selection dropdown (angel, VC, workbench, network)
- For network lens: multi-file upload
- AA sandbox connection button (separate from file upload)
- Clean error messages with specific row references where applicable
- Download buttons for all output formats
```

```
Record 4 demo videos (one per lens) using the cleanest synthetic statements as input.
```

```
Generate the system tour deck: a 10-12 slide PDF using reportlab or python-pptx walking through:
- Cover slide
- The problem
- The architecture diagram
- One slide per layer
- Live analysis screenshots (one per lens)
- Validation report summary
- What's next
```

```
Write docs/README.md and docs/architecture_diagram.png (generate the diagram using mermaid or graphviz).
```

---

## Sprint 4 closeout

Final regression run. Generate `results/sprint4/final_validation.md` with all metrics.

Tag the commit: `git tag sprint4-complete`

You now have the full technical product built.

---

# Working with Claude Code — Patterns That Work

**Before each major task, set context:**

```
Read schema/canonical.py and the existing tests in tests/. The pattern this project follows is: one agent per file, each agent has a clear input → output contract, all monetary arithmetic uses Decimal, all dates are datetime.date. Tests are pytest, run via `make test`.
```

**When asking for code, be specific about interfaces:**

> "Create class X with method y(arg: Type) -> ReturnType. The method should..."

Not:
> "Build something that does Y."

**When debugging, share both the error and the input that caused it:**

> "Running `pytest tests/test_categoriser.py::test_recurring_detection` produces this error: [paste full traceback]. The input transaction was: [paste]. Expected behaviour: [explain]. Actual behaviour: [explain]."

**When tests fail, ask Claude Code to investigate before fixing:**

> "These tests are failing: [list]. Before changing code, read the relevant files and tell me what you think is wrong. Don't fix yet — explain first."

**For complex changes, ask for a plan first:**

> "I want to add feature X. Before writing code, propose a plan: which files change, what new types are introduced, what tests would prove it works."

**Avoid asking Claude Code to do too much in one prompt:**

If a step has 8+ sub-tasks, split it into 8 prompts. Quality drops sharply as a single task grows.

**Always run tests after each change:**

```bash
make test
make regression
```

If either fails, fix before continuing. Compounding broken state is the fastest way to a stuck project.

---

# Reference Documents

- **Explanation PDF** — what the project is and why
- **Roadmap PDF** — what gets built in each sprint
- **This file** — how to build it, step by step

Update this file as you go. If a step doesn't match reality once you start, fix the step, don't fight the document.
