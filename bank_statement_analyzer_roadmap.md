# BSAA — Execution Roadmap
## Bank Statement Analysis Agent

*Implements Section 8 phasing of the product plan. Solo execution. Market product, not research.*
*Style mirrors the MMGTFFF research roadmap, with paper sections replaced by go-to-market and customer phases.*

---

## Immediate Next Steps (Pre-Phase 0)

Five decisions unblock all subsequent work. They are sequenced by dependency.

### Step 1 — Decide the first market (India vs US)
Drives the first jurisdiction module, the regulatory homework, and the first customer pool.
- Discuss the trade-off with mentor
- Lock the choice in writing
- **Output:** `docs/decisions/01-market.md` — choice + reasoning + reversal cost

### Step 2 — Competitive survey of the chosen market
Validate that the agentic + multi-lens + investor-specific framing has actual whitespace.
- Catalogue ≥15 existing tools: lender statement analysers, diligence platforms, accounting tools, fintech parsers
- For each: capability, gaps, price, target customer
- Identify the closest competitor and articulate the differentiator in one sentence
- **Output:** `docs/competitive-landscape.md` — table format, 1-2 pages

### Step 3 — Data-security planning session
- Storage: where, how long, encryption at rest and in transit
- Access: per-user, per-document, audit logging
- Consent: how captured from the investee company whose statements are being analysed
- Processing mode: cloud vs in-customer-environment
- Retention: zero-retention option for sensitive customers
- **Output:** `docs/security-model.md` (v0) — revisit with legal counsel before paid customers

### Step 4 — Lock the canonical transaction schema
The single most important data decision; every downstream agent depends on it.
- Define the fields exhaustively (Section 7.2 of the plan)
- Pin a version number; schema_v1 freezes when Phase 1 begins
- Include enrichment fields (category, counterparty, flags) so later agents have an obvious place to write
- **Output:** `schema/canonical.py` + `schema/CHANGELOG.md`

### Step 5 — Specify Phase 1 in detail
Before the spine is built, define what "useful summary" means precisely.
- Mock the angel-lens one-pager: every field, every chart, every red-flag callout
- Write measurable validation criteria (categorisation accuracy, extraction rate, runway-calc precision)
- **Output:** `docs/phase1-spec.md` + a Figma/sketch mockup of the angel lens

---

## Phase 0 — Foundations
**Goal:** every prerequisite to start building is in place — datasets, repo, stack, environment. This is the longest pre-code phase because the dataset work is non-trivial.

### 0A — Dataset Acquisition (the long pole)

Bank statements aren't a downloadable Kaggle dataset. The strategy is three-pronged: synthetic for volume and ground truth, real samples for fidelity, real consented statements for end-to-end realism.

#### Source 1: Synthetic statement generator
Build this first. It is the foundation of the test set because ground truth is known by construction.
- Collect 5-10 real bank sample PDFs from official bank websites (most banks publish customer-facing templates)
- For India-first: HDFC, ICICI, SBI, Axis, Kotak. For US-first: Chase, BofA, Wells Fargo, Citi
- Build a generator that produces PDFs matching each bank's layout exactly
- Generate distributions of investee profiles: healthy SaaS, burning startup, services firm, e-commerce, restaurant — each with characteristic transaction patterns
- Generate red-flag scenarios deliberately: round-tripping, structuring, related-party leakage, customer concentration, founder over-withdrawal
- Every generated statement comes with a paired ground-truth JSON: every transaction's correct category, every legitimate red flag, every expected metric
- **Target:** ≥100 generated statements, ≥3 bank templates, ≥5 investee profiles, ≥5 red-flag scenarios
- **Output:** `data/synthetic/` + paired `data/synthetic/ground_truth/`

#### Source 2: Real bank sample PDFs (templates only)
- Download official sample/template PDFs from every bank's site for the chosen jurisdiction
- These are short (1-2 pages) but capture rendering exactly
- Use to validate the format adapter against real bank output
- **Output:** `data/samples/<bank_name>/`

#### Source 3: Real consented statements
- Your own personal statements, properly anonymised (name → "Test Customer", account → "XXXX1234")
- Friends/family willing to share, with explicit written consent and full anonymisation
- Possibly: a single friendly investee company willing to share statements for testing in exchange for free first analysis
- **Output:** `data/real_anonymised/` — small but maximally realistic

#### Source 4: Open Banking sandboxes (longer term)
- Plaid Sandbox (US) — free, synthetic but realistic transaction data
- Account Aggregator sandbox (India) — for when API ingestion is built in Phase 4
- HuggingFace/Kaggle search for any public transaction or statement data
- Not the primary source but worth pulling for adapter variety
- **Output:** `data/openbanking/` (when applicable)

#### Gold-standard test set
The regression backbone. Every build runs against this.
- Curate 20-30 statements spanning formats, investee types, and red-flag scenarios
- Hand-label every transaction's correct category, every legitimate red flag, every expected metric
- Locked: once a statement is in the gold set, its labels do not change without explicit version bump
- **Output:** `data/gold/` + `data/gold/labels/`

### 0B — Repo and environment
- Single monorepo (recommended for solo dev)
- Branch strategy: `main` plus `feat/*` branches; never push directly to main
- CI on every PR: lint, type-check, gold-standard regression suite
- **Output:** GitHub repo + README + CI pipeline green on empty test suite

### 0C — Tech stack decisions
| Concern | Recommendation | Reasoning |
|---|---|---|
| Backend language | Python | Best PDF/ML/agent ecosystem |
| PDF extraction | Benchmark pdfplumber, PyMuPDF, tabula-py | Pick winner after testing on samples |
| OCR | Tesseract first; AWS Textract / Google Document AI as paid fallback | Free first, upgrade when accuracy matters |
| LLM provider | Anthropic Claude or OpenAI; design provider-agnostic | Don't lock in |
| Data store | Postgres (results) + S3 or equivalent (source files) | Standard |
| Orchestration | Custom Python class first; revisit Temporal/Prefect at Phase 4 | Don't overbuild |
| Frontend | Defer; Streamlit for Phase 1 demo, Next.js for real product | Build only when needed |
- **Output:** `docs/stack.md`

### Phase 0 Deliverables
| Artifact | Location |
|---|---|
| Market choice | `docs/decisions/01-market.md` |
| Competitive landscape | `docs/competitive-landscape.md` |
| Security model v0 | `docs/security-model.md` |
| Phase 1 spec | `docs/phase1-spec.md` |
| Canonical schema | `schema/canonical.py` (locked v1) |
| Synthetic generator + statements | `tools/synthetic_gen.py` + `data/synthetic/` |
| Real samples + ground truth | `data/samples/`, `data/gold/` |
| Repo + CI | GitHub + pipeline green |

### Phase 0 Done When
- ≥100 synthetic statements generated across ≥3 bank templates
- ≥20 gold-standard statements hand-labelled
- CI runs the regression suite on every PR (it's empty for now, but it runs)
- Canonical schema is documented and version-locked

---

## Phase 1 — The Spine: PDF → Angel Lens
**Goal:** thinnest complete end-to-end path. Upload a PDF, get a one-page angel summary out. Proves the pipeline architecture works and produces a real demoable artifact.

### 1A — Format adapter (PDF, digital)
- Implement `adapters/pdf_digital.py`
- Table-structure detection per page
- Extract rows: date, description, debit/credit, balance
- Multi-page handling: continuation, header repetition, balance carry-over
- Adapter interface: receives a file, returns raw extracted rows + metadata
- **Validation:** ≥90% transaction extraction rate on gold-standard digital PDFs

### 1B — Normaliser + per-bank profiles
- Implement `pipeline/normaliser.py`
- Build the first 3 bank profiles (matching the synthetic generator templates)
- Each profile: column mapping, date format, debit/credit convention, currency
- Model-assisted fallback for unknown layouts: LLM infers column mapping, system saves the inference as a new profile for future statements from that bank
- Output: `CanonicalTransaction[]` matching schema v1
- **Validation:** 100% schema compliance on output; balance continuity preserved

### 1C — Validator
- Implement `pipeline/validator.py`
- Primary check: running balance continuity (every row's balance = previous balance + credits - debits)
- Secondary checks: missing dates, duplicate rows, period gaps
- On failure: halt the pipeline, surface the precise issue with row reference
- **Validation:** deliberately corrupted statements (insert/delete/edit a row) must be caught

### 1D — Categoriser
- Implement `pipeline/categoriser.py`
- LLM-driven with constrained vocabulary: revenue, vendor_payment, salary, loan_in, loan_out, tax, transfer, founder_withdrawal, fees, other
- Cache categorisation by description pattern (description hash → category) to reduce repeat calls
- Few-shot prompt with examples from gold standard
- **Validation:** ≥85% category accuracy on gold-standard set

### 1E — Financial-health analyst
- Implement `analysis/financial_health.py`
- Pure arithmetic, no LLM:
  - Monthly burn rate
  - Runway (months at current burn)
  - Revenue trend + month-over-month growth
  - Cash flow pattern (volatility, seasonality)
- **Validation:** exact match against gold-standard expected metrics

### 1F — Angel lens report generator
- Implement `reports/angel_lens.py`
- One-page output containing:
  - Header: company name, statement period, key numbers
  - Top 3 red flags (Phase 1 only has financial-health flags; risk analyst comes in Phase 2)
  - Runway gauge
  - Revenue trend mini-chart
  - Burn breakdown
- Format: PDF (primary) + matching .xlsx (for analysts who want to tinker)
- Templating: Jinja → HTML → PDF via weasyprint; openpyxl for spreadsheet
- **Validation:** opens cleanly in standard PDF readers + Excel/Sheets

### 1G — Orchestrator (v1)
- Implement `pipeline/orchestrator.py`
- Sequential execution: adapter → normaliser → validator → categoriser → financial-health → report
- Persist every intermediate result to the result store, keyed by document_id + stage_name
- Errors surface with the responsible stage attributed
- **Validation:** any intermediate state can be inspected after a run

### 1H — Demo UI (minimal)
- Single page: drop a PDF, get the angel lens back
- No accounts, no auth, no payment — just the demo flow
- Stack: Streamlit (fastest) or a single Next.js page
- **Validation:** a stranger can use it without instruction

### Phase 1 Validation Criteria
| Criterion | Target |
|---|---|
| Extraction accuracy (transactions extracted vs ground truth) | ≥90% |
| Categorisation accuracy on gold set | ≥85% |
| Financial-health metrics | Exact match |
| End-to-end success rate on gold set | ≥80% (a few failures are expected and acceptable) |
| Demo time (PDF in → lens out) | <60 seconds |

### Phase 1 Customer-facing Artifact
- 2-minute live demo: drop a real anonymised statement, walk through the lens
- 90-second screen-capture for cold outreach
- One sample angel lens PDF used as the front-page demo on the landing page (Phase 6)

### Phase 1 Deliverables
| Artifact | Location |
|---|---|
| Digital PDF adapter | `adapters/pdf_digital.py` |
| Normaliser + 3 profiles | `pipeline/normaliser.py` + `profiles/` |
| Validator | `pipeline/validator.py` |
| Categoriser | `pipeline/categoriser.py` |
| Financial-health analyst | `analysis/financial_health.py` |
| Angel lens report | `reports/angel_lens.py` |
| Orchestrator v1 | `pipeline/orchestrator.py` |
| Demo UI | `demo/` (Streamlit app or Next.js page) |
| Phase 1 validation report | `results/phase1/validation.json` + summary |

---

## Phase 2 — Risk + VC Lens
**Goal:** the product crosses from "nice to have" to "genuinely valuable." Red-flag detection is the value-add that justifies paid pricing.

### 2A — Related-party tagger
- Implement `pipeline/related_party.py`
- Input: enriched table + known affiliates list (user-provided form: directors, sister companies, etc)
- Deterministic name matching against known affiliates
- LLM fuzzy matching for disguised/misspelt counterparties
- Output: per-transaction `is_related_party` flag + matched-affiliate reference
- **Validation:** ≥95% recall on synthetic related-party transactions

### 2B — Risk / red-flag analyst
Each detector is implemented as a separate, independently testable function.
- Implement `analysis/risk.py` with detectors:
  - **Structuring** — clusters of amounts just under a threshold (threshold from jurisdiction module when Phase 3 lands; placeholder for now)
  - **Round-tripping** — matched out-and-back flows within N days, same counterparty
  - **Spikes** — statistical outliers in inflows vs the account's own baseline
  - **Drains** — same, on the outflow side
  - **Customer concentration** — top-N revenue share exceeds a threshold
  - **Round-amount clustering** — too many suspiciously round amounts
  - **Founder over-extraction** — withdrawal patterns inconsistent with declared salaries
- A model-driven interpretation layer composes individual signals into plain-English explanations
- Every flag carries: detector name, triggering transaction IDs, severity, plain-English reason
- **Validation:** ≥90% recall on injected red flags in synthetic test set; ≤5% false positives on healthy synthetic statements

### 2C — VC lens report generator
- Implement `reports/vc_lens.py`
- Multi-sheet .xlsx workbook:
  - **Sheet 1 — Summary:** key numbers + composite risk score
  - **Sheet 2 — Financial health:** monthly breakdown of metrics
  - **Sheet 3 — Red flags:** every detected flag with evidence
  - **Sheet 4 — Transactions:** enriched table with categories, related-party tags, flags
  - **Sheet 5 — Related parties:** filtered view of related-party activity
- Composite risk score: weighted combination of flag count × severity
- Also produces a PDF summary version for partners who don't want a workbook
- **Validation:** opens cleanly in Excel and Google Sheets; risk score is interpretable

### 2D — Categoriser refinement
- Update categoriser prompt and few-shot examples using Phase 1 error analysis
- Add edge-case categories that emerged from real statements
- **Validation:** category accuracy improves from ≥85% (Phase 1) to ≥92%

### Phase 2 Validation Criteria
| Criterion | Target |
|---|---|
| Risk recall (injected flags caught) | ≥90% |
| Risk false-positive rate (healthy statements) | ≤5% |
| Related-party recall | ≥95% |
| Category accuracy | ≥92% |
| VC workbook opens in Excel + Sheets | 100% |

### Phase 2 Customer-facing Artifact
- Two demo flows ready:
  - Clean statement → angel lens + VC workbook
  - Synthetic statement with injected fraud patterns → show the risk analyst catching them
- Updated 90-second outreach video

### Phase 2 Deliverables
| Artifact | Location |
|---|---|
| Related-party tagger | `pipeline/related_party.py` |
| Risk analyst + detectors | `analysis/risk.py` |
| VC lens report | `reports/vc_lens.py` |
| Refined categoriser | `pipeline/categoriser.py` (v2) |
| Phase 2 validation report | `results/phase2/` |

---

## Phase 3 — Compliance, Reconciliation, Finance-team Workbench
**Goal:** serve the heaviest investor users (in-house analysts at funds) and prove the jurisdiction-pluggable architecture in production.

### 3A — Jurisdiction module (first one)
The defining feature of the compliance layer.
- Implement `jurisdictions/<country>/rules.py` for the chosen first market
- Codify, with regulatory references:
  - Cash-transaction limits (e.g. ₹2 lakh in India under Income Tax Act §269ST; CTR thresholds in US under BSA)
  - Tax payment expectations
  - Suspicious-transaction thresholds and reporting requirements
  - Related-party transaction limits where applicable
- Module is data + simple logic; no LLM in the rule layer (defensible, reproducible, auditable)
- Engage legal review of the codified rules before any paid customer uses this output
- **Output:** a swappable module with a documented interface

### 3B — Compliance analyst
- Implement `analysis/compliance.py`
- Loads the active jurisdiction module
- Runs each rule across enriched transaction data
- Output: list of compliance exceptions, each with:
  - The rule violated
  - The transaction(s) that violated it
  - The regulatory reference
  - Severity (informational / warning / critical)
- In the investor-facing product, these are framed as **regulatory risk signals**: evidence the investee is exposed to regulatory issues that would, in turn, expose its investors
- **Validation:** catches all injected violations in the synthetic compliance-violation test set

### 3C — Reconciliation analyst
- Implement `analysis/reconciliation.py`
- Input: enriched statement data + company's claimed figures (from a form, or extracted from an uploaded pitch deck PDF)
- LLM extracts unstructured claims from pitch decks; deterministic matching against statement data
- Compares:
  - Declared revenue vs categorised revenue total
  - Declared loan inflows vs detected loan_in transactions
  - Declared headcount × average salary vs detected salary outflows
  - Any user-defined claim → statement check
- Output: list of match/mismatch findings with magnitude and direction
- **Validation:** catches mismatches when synthetic statements diverge from synthetic claims

### 3D — Finance-team analyst workbench lens
The deepest output. This is what makes the product valuable to professional analysts.
- Implement `reports/workbench_lens.py`
- Output: comprehensive multi-sheet workbook + raw enriched data export (CSV + JSON)
- Features beyond VC lens:
  - Every finding clickable to drill down to underlying transactions
  - Toggle which detectors run (analysts may want to ignore some)
  - Export raw enriched data for use in the fund's own models
  - Custom rule injection (analyst-defined detectors, e.g. "flag any transaction >X with counterparty Y")
- **Validation:** an external test analyst can use the workbench unaided for 20 minutes and surface findings

### 3E — Versioned analysis
- Tag every analysis run with: schema version, agent versions, jurisdiction module version
- Storage: include the version tuple in the result store metadata
- Replay capability: re-run a historical analysis with current code; diff the outputs
- Reason: analysts need reproducibility ("can you re-run that one from last quarter?"); partners need to trust version-to-version diffs
- **Validation:** an old analysis can be replayed and the diff is meaningful, not noise

### Phase 3 Validation Criteria
| Criterion | Target |
|---|---|
| Compliance violation recall | ≥95% |
| Reconciliation mismatch recall | ≥90% |
| Workbench drill-down works on every metric | 100% |
| Versioned replay produces deterministic output | 100% |

### Phase 3 Customer-facing Artifact
- New demo aimed at VC and PE finance teams: walk through the workbench on a complex statement with multiple findings
- Show: drill-down, custom rule, export to CSV that loads into a typical fund model

### Phase 3 Deliverables
| Artifact | Location |
|---|---|
| First jurisdiction module | `jurisdictions/<country>/` |
| Compliance analyst | `analysis/compliance.py` |
| Reconciliation analyst | `analysis/reconciliation.py` |
| Workbench lens | `reports/workbench_lens.py` |
| Versioning system | `pipeline/versioning.py` |
| Phase 3 validation report | `results/phase3/` |

---

## Phase 4 — Network Batch Lens + Format Breadth + Scale
**Goal:** serve angel networks at volume. Expand format support. Harden for production load.

### 4A — Network batch lens
The lens that earns the angel-network segment.
- Implement `reports/network_lens.py`
- Input: N statements representing N companies (one statement per company)
- Output: a standardised comparison workbook
  - One row per company
  - Columns: key financial-health metrics, flag counts, composite risk score, compliance status
  - Sortable / filterable by any column
  - Per-company individual reports linked from each row
- Also: an aggregate dashboard (PDF) summarising the cohort
- **Validation:** an angel-network operator can triage 50 deals in <10 minutes using only this output

### 4B — Additional format adapters
Each new adapter expands the addressable input universe.
- `adapters/csv.py` — column auto-detection, varied delimiter/encoding
- `adapters/excel.py` — XLSX parsing, sheet detection, header detection
- `adapters/scanned_pdf.py` — adds OCR layer (Tesseract first, paid API as fallback)
- `adapters/image.py` — JPG/PNG of single statement pages
- Track coverage: which bank formats each adapter handles successfully
- **Validation:** real-world test set coverage rises to ≥90%

### 4C — Batch processing
- Concurrent pipeline execution: multiple statements processed in parallel
- Job queue (Postgres-backed initially; upgrade to dedicated queue if throughput demands)
- Per-job progress tracking exposed via API
- Failure isolation: one bad statement does not kill the batch
- **Validation:** 50 mixed-format statements complete a full pipeline in <30 minutes

### 4D — Scale hardening
- Per-analysis LLM cost tracking (token usage logged, alerts on outliers)
- Cache at every model-driven stage: categoriser, related-party fuzzy match, claim extraction
- Per-bank normalisation profile library grows to ≥10 banks
- Observability: structured logs + metrics dashboard (success rate, latency per stage, flag counts)
- **Validation:** average per-statement cost in steady state ≤target (set during Phase 6 pricing work)

### Phase 4 Validation Criteria
| Criterion | Target |
|---|---|
| Real-world format coverage | ≥90% |
| Batch throughput (50 statements) | <30 min |
| Per-statement LLM cost | Under pricing-derived budget |
| Profile library | ≥10 banks |

### Phase 4 Customer-facing Artifact
- Network demo: drop a folder of 50 PDFs, get a ranked comparison + per-company drill-downs
- This is the highest-value live demo because it shows scale and the network lens together
- Update outreach materials to include the network angle

### Phase 4 Deliverables
| Artifact | Location |
|---|---|
| Network batch lens | `reports/network_lens.py` |
| CSV adapter | `adapters/csv.py` |
| Excel adapter | `adapters/excel.py` |
| Scanned-PDF adapter | `adapters/scanned_pdf.py` |
| Image adapter | `adapters/image.py` |
| Batch processing infra | `pipeline/batch.py`, job queue |
| Observability dashboard | `ops/dashboard/` |
| All four lenses live | angel, VC, workbench, network |

---

## Phase 5 — Expansion
**Goal:** extend reach. New jurisdictions, integrations, deeper features — driven by customer pull, not roadmap.

### 5A — Second jurisdiction module
- Choice driven by customer demand (the second market that pays)
- New module ≈ rules + regulatory references; engine unchanged
- Validate that mid-analysis jurisdiction switching works cleanly
- **Output:** `jurisdictions/<second-country>/`

### 5B — Integrations
Picked based on what customers are asking for.
- **Live bank data:** Account Aggregator API (India) or Plaid (US) — moves the product from "upload PDFs" to "connect bank and refresh"
- **CRM integrations:** push analysis summaries into investor deal-flow systems (Affinity, Attio, Salesforce)
- **Fund-admin integrations:** export workbench data into fund accounting tools (Carta, AngelList Stack)
- Each integration is its own small project; only do the ones with at least three customer requests

### 5C — Cross-statement analysis
- Trend across multiple statement periods for the same company
- Cohort comparison: how does this investee compare to peers in the same sector
- Requires: persistent storage of past analyses + a peer dataset (built incrementally from real customer analyses, with consent)
- **Output:** trend mode in the workbench lens

### 5D — Customer-driven feature roadmap
- Build from Phase 7 feedback loop, not from speculation
- Examples that may emerge: scenario modelling, what-if simulations, custom-detector library per fund, white-labelled outputs for fund branding

### Phase 5 Done When
- Decided based on customer pull; no fixed end criterion. This phase runs as long as the product runs.

---

## Phase 6 — Go-to-Market Preparation
**Goal:** replace the research roadmap's "Paper Writing" stage with everything needed to sell. Can start running in parallel with Phases 2-4 — does not require the full product.

### 6A — Brand and positioning
- Final product name (lock it)
- One-line positioning: "<verb> for <user> doing <job>" formula
- Target customer descriptions per lens (one short page each: angel, network, VC, finance team)
- **Output:** `marketing/positioning.md`

### 6B — Landing page
- Hero: what it does in 10 seconds
- 90-second demo video (from Phase 2 onward)
- One section per investor type with their specific value prop and lens screenshot
- Pricing page (contact-for-pricing initially)
- Security page (excerpt from `security-model.md`)
- Stack: Next.js + Vercel, or Webflow if non-technical preferred
- **Output:** live URL, mobile-responsive, page-speed >90

### 6C — Demo materials
- Curated demo statements showing every lens at its best
- Live 5-minute demo script
- 90-second async demo recording
- Self-serve sandbox: a public page that lets a visitor analyse one statement without signing up (rate-limited)
- **Output:** `demo-assets/`, sandbox URL

### 6D — Pricing test
- Three-tier hypothesis: angel (low), network/VC (mid), enterprise/finance-team (high)
- Don't publish numbers publicly until tested
- A/B in early outreach: ask 5 prospects what they'd pay before quoting a price
- Document the converged pricing in `marketing/pricing.md`
- **Output:** pricing hypothesis backed by ≥10 conversations

### 6E — Legal and operational basics
- Terms of Service
- Privacy Policy (especially explicit on bank-data handling and retention)
- Customer Data Processing Agreement (DPA) template
- Company incorporation if not already done
- Business bank account, accounting setup
- **Output:** all legal docs reviewed by a lawyer before first paid customer

### 6F — Security and trust collateral
- Customer-facing security one-pager
- SOC 2 readiness checklist (full SOC 2 is deferred until revenue justifies it; the checklist starts now)
- Penetration test scheduled before first enterprise customer
- Bug-bounty channel (even informal)
- **Output:** `marketing/security.md` (customer-facing) + internal SOC 2 checklist tracker

### Phase 6 Deliverables
| Artifact | Location |
|---|---|
| Positioning + brand | `marketing/positioning.md` |
| Landing page | live URL |
| Demo materials | `demo-assets/` + sandbox URL |
| Pricing | `marketing/pricing.md` (private) |
| Legal docs | reviewed and signed off |
| Security one-pager | `marketing/security.md` |
| SOC 2 checklist | internal tracker |

---

## Phase 7 — First Customers + Iteration
**Goal:** get to first ten paying customers. Use them to find what's worth building next. This is the equivalent of the paper's "Results" stage — the validation that the whole thing works.

### 7A — Outreach
- Warm-list first: every angel, network, VC, finance-team contact reachable through mentor's network
- Target funnel:
  - 30 first-contact conversations
  - 10 of those become demos
  - 5 of those become paid pilots
- Track in a simple CRM (Notion or Airtable initially; upgrade later)
- Cadence: 5 outreach conversations per week minimum

### 7B — Pilot structure
- Pilot terms: free or low-cost first run, in exchange for feedback + case-study rights + referrals
- Duration: 4-6 weeks
- Weekly check-in calls
- Capture in writing: what they used, what they ignored, what they wished it did, what they paid attention to
- **Output:** per-pilot `pilots/<customer>/log.md`

### 7C — Feedback loop
- Weekly: synthesise pilot feedback into a ranked feature backlog
- Monthly: ship updates driven by feedback
- Discipline: avoid single-customer feature creep; prioritise features ≥3 customers asked for
- **Output:** `roadmap/backlog.md` updated weekly

### 7D — Paid conversion
- At pilot end: convert to paid tier at the Phase 6D price
- Target: ≥60% pilot-to-paid conversion
- If conversion is below target: re-examine value prop, segment, or price — not the product surface
- **Output:** revenue, in writing, paid through the company's bank account

### 7E — Case studies and second wave
- Each paying customer becomes a case study (with permission)
- Use cases studies to drive a second outreach wave (this one cold)
- Begin content/SEO presence: blog posts on investor diligence, statement red flags, jurisdiction-specific compliance
- **Output:** ≥3 published case studies, ≥10 blog posts in the first six months of Phase 7

### Phase 7 Done When
- ≥10 paying customers
- Repeatable sales motion: a new customer can go from first contact to paid in ≤30 days, by following a documented process
- Clear product-market signal: which lens converts hardest, which segment pays most, which jurisdiction has demand

---

## Execution Timeline

| Phase | Depends On | Notes |
|---|---|---|
| Immediate Next Steps | — | Sequenced; each step is days, not weeks |
| Phase 0 (Foundations) | Steps 1-5 done | Dataset acquisition is the long pole |
| Phase 1 (Spine) | Phase 0 done | The single longest build phase |
| Phase 2 (Risk + VC lens) | Phase 1 stable | Core value-add |
| Phase 3 (Compliance + workbench) | Phase 2 stable | Jurisdiction homework parallelisable with build |
| Phase 4 (Network + breadth) | Phase 3 done | Adapters can be added incrementally |
| Phase 5 (Expansion) | Phase 4 done | Customer-driven; no hard end |
| Phase 6 (GTM prep) | Phase 1 done minimum | **Runs in parallel** with Phases 2-4 |
| Phase 7 (First customers) | Phase 2 + Phase 6 minimum | Begins as soon as a sellable demo exists |

**Solo realism:** Phases 0-2 are heavy build. Phase 3 onward is extension. Phase 6 runs alongside. Phase 7 is the goal — every earlier phase exists to make Phase 7 possible.

---

## Repo / Results Structure

At every phase, save artefacts to a defined structure for reproducibility and traceability.

```
bsaa/
  schema/
    canonical.py             — locked v1 contract
    CHANGELOG.md
  adapters/
    pdf_digital.py           — Phase 1
    csv.py, excel.py         — Phase 4
    scanned_pdf.py, image.py — Phase 4
  pipeline/
    normaliser.py            — Phase 1
    validator.py             — Phase 1
    categoriser.py           — Phase 1, refined Phase 2
    related_party.py         — Phase 2
    orchestrator.py
    batch.py                 — Phase 4
    versioning.py            — Phase 3
  analysis/
    financial_health.py      — Phase 1
    risk.py                  — Phase 2
    compliance.py            — Phase 3
    reconciliation.py        — Phase 3
  jurisdictions/
    <first-country>/         — Phase 3
    <second-country>/        — Phase 5
  reports/
    angel_lens.py            — Phase 1
    vc_lens.py               — Phase 2
    workbench_lens.py        — Phase 3
    network_lens.py          — Phase 4
  profiles/                  — per-bank normalisation profiles
  data/
    synthetic/               — Phase 0
    samples/                 — Phase 0
    real_anonymised/         — Phase 0
    gold/                    — Phase 0, append-only
    openbanking/             — Phase 4+
  tools/
    synthetic_gen.py
  demo/
  results/
    phase0_foundations/
    phase1_spine/
      validation.json
      gold_set_results/
      demo_recording.mp4
    phase2_risk_vc/
    phase3_compliance_workbench/
    phase4_network_scale/
    phase5_expansion/
  marketing/                 — Phase 6
  pilots/                    — Phase 7
  docs/
    decisions/               — every major decision logged
    competitive-landscape.md
    security-model.md
    phase1-spec.md
    stack.md
  ops/
    dashboard/               — Phase 4+
```

Every `results/phaseN_*/` folder should contain:
- `validation.json` — measured against phase validation criteria
- Test artefacts (sample outputs from gold-set runs)
- Config and version metadata used for that phase's runs
- Any visualisations or demo recordings produced

---

## Decision Log

Every non-trivial decision goes in `docs/decisions/NNN-title.md` with:
- **Context** — why this came up
- **Decision** — what was chosen
- **Alternatives** — what was considered and rejected
- **Reversal cost** — how hard it would be to change later

Initial entries (created during Immediate Next Steps and Phase 0):
- `01-market.md` — India vs US
- `02-stack.md` — language, frameworks, vendors
- `03-schema-v1.md` — canonical transaction record fields
- `04-security-model.md` — storage, retention, consent
- `05-llm-provider.md` — initial choice + abstraction layer

---

## Risk Register

| Risk | Mitigation | Owner |
|---|---|---|
| Extraction reliability — bad parsing kills every downstream conclusion | Validator (1C) is the primary defence; gold-standard regression on every PR; balance-continuity check halts on failure | Solo |
| Trust / explainability — investors won't act on a black box | Persistence layer captures every intermediate state; every flag carries evidence; UI surfaces traceability | Solo |
| Regulatory liability — system mistaken for advice or filing | Non-goals (plan Section 4.3) stated in product copy; ToS makes the boundary explicit | Solo + lawyer |
| Data security incident — fatal to the business | Security model from Step 3; legal review before paid customers; pen test before enterprise customers | Solo + lawyer + external pentest |
| Scope creep — building all four lenses at once | Phase gating; no Phase N+1 work until Phase N validation passes | Solo |
| Dataset bottleneck — no real statements to test against | Phase 0 synthetic generator is the long-pole investment; expand with consented real statements as available | Solo |
| Solo bandwidth — phases stack | Phase 6 runs in parallel; pilot customers (Phase 7) are themselves part of the validation, not extra work | Solo |
| Jurisdiction module incorrectness — wrong rule, wrong flag, lost customer | Legal review of every codified rule before any paid customer sees output; regulatory references in every exception | Solo + lawyer |

---

## What Done Looks Like

The roadmap is complete when:
- ≥10 paying customers across at least two of the four investor types
- A repeatable, documented sales motion
- Validated pricing
- A second jurisdiction module either built or contracted
- A clear product-market signal: which lens, which segment, which jurisdiction is the strongest pull

From that point onward, "Phase 5" is the rest of the company's life.
