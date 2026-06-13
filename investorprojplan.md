# Agentic Bank Statement Analysis Platform
### Full Product Plan

*This is a planning document, not a build specification. It lays out the complete product vision, architecture, phasing, and go-to-market so that the build sequence is clear and defensible.*

---

## 1. Executive summary

The product is an **agentic system that ingests company bank statements in any format and produces investor-grade financial analysis automatically.**

Today, anyone evaluating a company's finances from raw bank statements — an angel investor, an angel investor network, a VC, or the finance team inside an investment firm — does it by hand. They open a PDF, scroll through hundreds of transactions, manually tag what's revenue versus a loan versus a founder withdrawal, eyeball it for anything suspicious, and rebuild the numbers in a spreadsheet. It is slow, inconsistent, and error-prone, and the quality depends entirely on how diligent and experienced the reviewer is.

This platform replaces that manual work with a pipeline of specialised agents: it reads the statement regardless of format, normalises every bank's layout into one clean structure, categorises and enriches every transaction, runs financial-health, risk, compliance, and reconciliation analysis, and produces a tailored output for whichever kind of investor is asking — a one-page summary for an angel, a deep workbook for a VC, a batch screening view for an angel network triaging many deals, or an analyst's workbench for an in-house finance team.

The core insight that keeps this buildable: **one analysis engine, many output lenses.** The hard work of reading and understanding a statement is identical no matter which investor is looking. What changes is only how the findings are framed and presented. This lets the product serve every investor user type from a single engine, without building four separate products.

---

## 2. Goals of the project

### 2.1 Primary goal

Automate the conversion of raw bank statements into trustworthy, decision-ready financial analysis for investors — removing the slow, inconsistent, error-prone manual work that every reviewer does today, and replacing it with a fast, repeatable, explainable pipeline.

### 2.2 Specific objectives

- **Universal ingestion.** Accept bank statements in any format — digital PDFs, scanned documents, images, CSV, Excel, and eventually direct bank/API feeds — through a format-agnostic ingestion design.
- **Reliable normalisation.** Convert every bank's idiosyncratic layout into one clean, validated transaction structure that all analysis depends on, and detect extraction failures rather than analysing corrupt data.
- **Intelligent enrichment.** Categorise and contextualise every transaction — revenue, loans, salaries, taxes, transfers, related-party movements — at a level no fixed rule set could achieve.
- **Multi-dimensional analysis.** Produce financial-health metrics (burn, runway, revenue trend), risk and red-flag detection (round-tripping, structuring, anomalies, concentration), compliance checks against the relevant jurisdiction's rules, and reconciliation against the company's own claims.
- **Tailored output per investor type.** Serve individual angels, angel investor networks, VCs, and in-house investor finance teams from a single engine, each receiving output framed for their workflow.
- **Explainability and trust.** Ensure every conclusion can be traced back to the specific transactions that drove it, so investors can act on the output with confidence.
- **Scalability.** Support analysis at volume — many companies processed in batch — so the product serves not only individual angels but networks and funds reviewing dozens of deals at once.

### 2.3 Success criteria

The project succeeds when an investor can upload a bank statement in a common format and receive, within minutes, an accurate and explainable analysis that they would otherwise have spent hours producing by hand — and when that output is trusted enough to inform a real investment decision.

### 2.4 Guiding principles

- **One engine, many lenses** — build the analysis once, present it many ways.
- **Architecture for breadth, ship with focus** — design for every format and every investor type, but deliver in disciplined phases.
- **Trust over cleverness** — an explainable, traceable result beats an impressive black box.
- **Security as a first-class concern** — handling bank data responsibly is foundational, not an add-on.

---

## 3. The problem in detail

### 3.1 Who has this problem

Four distinct investor user types share the same underlying pain — turning raw bank data into a trustworthy financial picture:

- **Angel investors** evaluating an early-stage company, often working from incomplete information and limited time. They need to know quickly: is this company burning cash sustainably, is real revenue coming in, and are the founders behaving responsibly with money?
- **Angel investor networks** that screen and circulate deals to their member base. They process many companies per cycle and need a standardised, comparable summary across all of them — the goal is fast, consistent triage so members focus on the deals worth a deeper look.
- **VCs and institutional investors** doing formal due diligence on a smaller number of companies they are seriously considering. They need everything an angel needs, plus depth: revenue quality, customer concentration, related-party transactions, regulatory red flags, and whether the statements reconcile with what the company claims in its pitch deck.
- **In-house investor finance teams** — the analysts inside VC, PE, family-office, or corporate-development groups who actually do the diligence work and produce reports for the partners. They are the heaviest users: they want the deepest output, transaction-level drill-down, customisable analysis, and exportable data they can fold into their own models.

### 3.2 Why it is painful today

- **Manual and slow.** A thorough review of one company's statements can take a skilled analyst hours to days.
- **Inconsistent.** Two reviewers looking at the same statements reach different conclusions because there is no standard method.
- **Format chaos.** Every bank formats statements differently. There is no universal structure, so even extracting clean data is a project in itself.
- **Easy to miss red flags.** Subtle patterns — money cycling in and out, transactions deliberately kept just under reporting thresholds, revenue that is actually a disguised loan — are exactly what a tired human scrolling a PDF misses.
- **Doesn't scale.** An angel network screening fifty deals a month, or a VC's finance team running diligence on a dozen, cannot do this by hand at volume.

### 3.3 Why now

Two things make this newly possible. First, language models can now read messy, unstructured financial documents and reason about transaction meaning in a way that rule-based software never could. Second, "agentic" architectures — pipelines where specialised components each handle one step and pass structured results forward — are mature enough to build a reliable multi-stage analysis system rather than one monolithic black box.

---

## 4. Product vision and scope

### 4.1 What it is

A platform that takes bank statements as input and produces tailored financial analysis as output, with the depth and framing matching the investor type. The same engine powers all outputs; the user chooses a "lens."

### 4.2 What "agentic" means here, concretely

Agentic does not mean one giant prompt. It means a pipeline of specialised agents, each owning one job, each with a defined input and output, each independently testable and improvable. This matters because it makes the system reliable, debuggable, and explainable — which is non-negotiable when the output influences investment decisions. A black box that says "this company is risky" with no traceable reasoning is unsellable. A pipeline that can show *which transactions* drove that conclusion is trustworthy.

### 4.3 Explicit non-goals (at least initially)

- It is **not** an accounting system or bookkeeping replacement.
- It does **not** file taxes or submit regulatory reports on anyone's behalf.
- It does **not** give investment advice or a buy/don't-buy verdict — it surfaces the facts and flags; investors decide.
- It does **not** connect to live bank accounts in early phases; it works from statements the investor or company provides.
- It is **not** sold to companies for self-review or to regulators for compliance auditing in the initial product. The investor wedge is the focus; other audiences may come later but are out of scope here.

Stating these clearly protects the product from scope creep and from regulatory liability it shouldn't take on.

---

## 5. System architecture (high-level)

The platform is five layers. Data flows down through them; each layer only ever sees the clean output of the one above. This is the single most important design decision in the whole plan, because it is what makes "support every format and serve every investor type" achievable without chaos.

### Layer 1 — Ingestion

**Job:** accept any input format and extract raw rows.

Bank statements arrive as digital PDFs (most common), scanned PDFs or images (need OCR), CSV exports (clean but rare), Excel files, and — eventually — direct bank or account-aggregator API feeds. Each format gets its own **adapter**. An adapter's only responsibility is to turn its format into raw extracted transaction rows. Adding a new format later means writing one new adapter, not touching anything downstream.

This is the key reframe on "support all formats": the *architecture* supports unlimited formats from day one because of the adapter pattern. The *product* ships adapters one at a time, starting with whatever format the first customers actually hand over (almost always PDF).

### Layer 2 — Normalisation

**Job:** turn messy, bank-specific raw rows into one clean, standard transaction table.

Every bank orders columns differently, formats dates differently, and shows debits and credits differently (separate columns, signed amounts, or a running-balance direction). This layer reconciles all of that into a single internal standard: date, description, debit, credit, balance, and a reference to the source row.

Critically, this layer also **validates**: it checks that the running balance actually adds up transaction to transaction. If it doesn't, extraction failed somewhere and the system flags it rather than silently analysing corrupt data. This validation step is what separates a trustworthy product from a demo.

Everything below this layer only ever sees the clean standard table. They never know or care what format the original was. That is the entire payoff of the architecture.

### Layer 3 — Enrichment

**Job:** add meaning to each transaction.

This is where the intelligence begins. For every transaction, the system: categorises it (revenue, vendor payment, salary, loan in/out, tax, inter-account transfer, founder withdrawal, fees), identifies the counterparty where possible, marks whether it is recurring or one-time, and tags related-party transactions (money moving between the investee company and its own directors or sister entities). This is the layer where an LLM agent genuinely earns its place, because categorisation requires reading and understanding free-text descriptions that no fixed rule set can fully cover.

### Layer 4 — Analysis

**Job:** run the investor-relevant analysis on the enriched data.

This layer has four analysis modules, each producing structured findings. All four feed investor decision-making — including compliance, which surfaces regulatory red flags that are themselves a major investor risk signal:

- **Financial health:** monthly burn rate, runway (months of cash remaining at current burn), revenue trend and growth, cash-flow patterns, and seasonality.
- **Risk and red flags:** round-tripping (money cycling out and back), structuring (many transactions deliberately just under a reporting threshold), sudden unexplained spikes or drains, circular fund movement between related parties, and customer concentration (too much revenue from one source).
- **Compliance:** cash transactions over legal limits, consistency between declared tax obligations and actual tax payments, and patterns that would draw regulatory attention. These are surfaced as risk signals to the investor — a company exposing itself to regulators is exposing its investors too. *This module is jurisdiction-specific — see Section 9.*
- **Reconciliation:** does what the statements show match what the company claims elsewhere (in a pitch deck, a declared revenue figure, a loan schedule)?

### Layer 5 — Output

**Job:** present the analysis through the right lens for the investor.

The analysis layer produces one rich set of findings. This layer selects, frames, and formats from it per investor type:

- **Angel lens:** a punchy one-page summary — key numbers, runway, top red flags. No 40-tab workbook. Built for speed and a clear go/no-go gut read.
- **Angel network lens:** a batch view across many companies at once — standardised, comparable, ranked or filterable. Built for triage at portfolio scale.
- **VC lens:** a detailed multi-sheet workbook plus a risk score and full transaction-level backup. Built for formal diligence on a smaller set of serious candidates.
- **Finance-team analyst workbench:** the deepest output — every finding, every flagged transaction, raw enriched data exportable to a fund's own models, and the ability to customise which checks run. Built for the analysts who do the actual work and answer to a partner.

Outputs export as spreadsheets and reports.

---

## 6. The agent breakdown

Mapping the layers to concrete agents, each independently testable:

| Agent | Layer | Input | Output |
|---|---|---|---|
| Format adapters (one per format) | Ingestion | Raw file | Raw extracted rows |
| Normaliser | Normalisation | Raw rows | Clean standard transaction table |
| Validator | Normalisation | Clean table | Pass/fail + flagged extraction errors |
| Categoriser | Enrichment | Clean table | Table with category, counterparty, recurrence tags |
| Related-party tagger | Enrichment | Tagged table | Table with related-party flags |
| Financial-health analyst | Analysis | Enriched table | Burn, runway, trend, seasonality metrics |
| Risk/red-flag analyst | Analysis | Enriched table | List of flagged patterns with reasons |
| Compliance analyst | Analysis | Enriched table | List of compliance exceptions, surfaced as investor risk signals (jurisdiction-aware) |
| Reconciliation analyst | Analysis | Enriched table + company claims | Match/mismatch findings |
| Report generator (per lens) | Output | All findings + chosen lens | Formatted spreadsheet/report |

Because each agent has a defined input and output, you can build, test, and improve any one of them without breaking the others — and you can show an investor exactly *why* a conclusion was reached by tracing back through the chain.

---

## 7. Technical architecture

This section describes the system at a technical level: how data moves, how each agent is built and what contract it honours, the shared data model, and the supporting infrastructure. The principle throughout is a **typed pipeline of stateless agents communicating through well-defined data contracts**, orchestrated by a controller, with every intermediate result persisted so the system is auditable end to end.

### 7.1 Architectural style

The system is a **staged, orchestrated agent pipeline**, not a single model call. An orchestrator drives a directed sequence of agents. Each agent is stateless: it receives a typed input, performs one job, and returns a typed output. No agent holds hidden state between runs, which makes each one independently testable, independently replaceable, and safe to run in parallel where the data allows.

Three properties fall out of this style and they are the reasons to choose it:

- **Traceability.** Because every stage persists its input and output, any final conclusion can be traced backward through the exact intermediate states that produced it. This is what makes the output trustworthy enough to act on.
- **Composability.** Adding a new format, a new analysis, or a new output lens means adding or swapping one agent against a fixed contract — never rewriting the pipeline.
- **Resilience.** A failure in one stage is contained. The orchestrator can retry, fall back, or halt with a clear error pointing at the responsible stage, rather than producing a silently wrong result.

### 7.2 The core data contract — the canonical transaction model

Everything in the system depends on one shared structure: the **canonical transaction record**. This is the contract that decouples ingestion from analysis. Once raw data becomes canonical, no downstream agent needs to know anything about the original file format or bank.

A canonical transaction record contains, at minimum:

- a stable unique identifier,
- the transaction date (normalised to a single date format),
- the raw description text exactly as it appeared,
- a signed or split amount (debit and credit resolved into a consistent convention),
- the running balance after the transaction,
- a reference back to the exact source row/page in the original file (for traceability),
- and an extensible set of enrichment fields (category, counterparty, recurrence flag, related-party flag, anomaly flags) that later agents populate.

A **statement document** wraps a set of canonical transactions together with metadata: the account identity, the statement period, the source format, and the validation status. This document is the object that flows through the pipeline, accreting enrichment as it goes.

### 7.3 The agents in technical detail

Each agent below is described by its contract — what it consumes, what it produces, and how it is built. Agents fall into two implementation classes: **deterministic** (rule- and code-driven, predictable, fast) and **model-driven** (LLM-backed, for tasks requiring language understanding). The system deliberately uses deterministic agents wherever possible and reserves model-driven agents for genuine judgement tasks, because deterministic components are cheaper, faster, and easier to verify.

**Format adapters (deterministic, one per format).**
Consume a raw file of a specific type; produce raw extracted rows. A digital-PDF adapter uses a PDF text/table extraction library; a scanned-document adapter adds an OCR stage first; a CSV/Excel adapter parses tabular data directly; a future API adapter calls an account-aggregator endpoint. Each adapter is isolated behind a common adapter interface, so the orchestrator selects the right one by detected file type and treats them all identically. Adapters do *not* interpret meaning — they only extract.

**Normaliser (deterministic, with a model-assisted fallback).**
Consumes raw rows; produces canonical transaction records. It maps each bank's column layout, date format, and debit/credit convention onto the canonical model. Because bank layouts vary, the normaliser is driven by a library of per-bank mapping profiles; when it encounters an unrecognised layout, it falls back to a model-driven step that infers the column mapping, which is then saved as a new profile. This keeps the common case fast and deterministic while still handling novel formats gracefully.

**Validator (deterministic).**
Consumes canonical records; produces a pass/fail status plus a list of any integrity errors. Its primary check is balance continuity — that each running balance equals the previous balance plus credits minus debits. It also checks for missing dates, duplicate rows, and period gaps. If validation fails, the pipeline halts for that document and surfaces a precise error rather than analysing corrupt data. This agent is the system's primary defence against silent extraction errors.

**Categoriser (model-driven).**
Consumes validated canonical records; produces the same records enriched with a category, an inferred counterparty, and a recurring/one-time flag. It reads each transaction's free-text description and classifies it (revenue, vendor payment, salary, loan in/out, tax, transfer, withdrawal, fees). Because descriptions are unstructured natural language, this is a genuine language task and is model-driven, but its output is constrained to a fixed category set so downstream agents receive predictable values. Categorisation results are cached by description pattern to reduce repeat model calls.

**Related-party tagger (model-driven + deterministic).**
Consumes categorised records plus any known list of the company's directors and affiliated entities; produces records flagged where a counterparty appears to be a related party. It combines deterministic name-matching against the known-affiliates list with model-driven judgement for fuzzy or disguised matches. Related-party movement is a key risk signal, so this agent feeds directly into the risk and compliance analysts.

**Financial-health analyst (deterministic).**
Consumes the fully enriched statement document; produces a structured metrics object — monthly burn rate, runway, revenue trend and growth rate, cash-flow patterns, and seasonality. These are arithmetic computations over the categorised data and are fully deterministic, which means they are exact, fast, and verifiable. No model is involved in producing a number that an investor will make a decision on.

**Risk / red-flag analyst (deterministic + model-driven).**
Consumes the enriched document; produces a list of flagged patterns, each with a machine-readable reason and the specific transactions that triggered it. Most detectors are deterministic rules over the data: structuring (clusters of transactions just under a threshold), round-tripping (matched out-and-back flows), sudden spikes or drains (statistical outliers against the account's own baseline), and customer concentration (revenue share by counterparty). A model-driven layer sits on top to interpret combinations of signals and explain them in plain language. Every flag carries its evidence, so nothing is unexplained.

**Compliance analyst (deterministic, jurisdiction-pluggable).**
Consumes the enriched document plus the active jurisdiction's compliance ruleset; produces a list of compliance exceptions. This agent is unique in that its rules are externalised into a swappable **jurisdiction module** — a self-contained ruleset encoding that country's cash-transaction limits, tax-payment expectations, and reporting thresholds. Switching or adding a jurisdiction means loading a different module, not changing the agent. The rules themselves are deterministic so that findings are reproducible and defensible. In the investor-facing product, these exceptions are presented as **regulatory risk signals** — evidence that the investee is exposed to compliance issues that would, in turn, expose its investors.

**Reconciliation analyst (deterministic + model-driven).**
Consumes the enriched document plus the company's own claims (declared revenue, loan schedules, pitch-deck figures); produces a list of match/mismatch findings. It compares what the statements show against what the company asserts and surfaces discrepancies. Structured claims are matched deterministically; unstructured claims (e.g. extracted from a deck) use a model-driven extraction step first.

**Report generators (deterministic templating, per lens).**
Consume the complete set of findings plus a chosen lens; produce the final formatted artifact. Each lens — angel summary, angel-network batch, VC workbook, finance-team workbench — is a template that selects, orders, and frames the relevant findings and renders them to a spreadsheet or report. Because all four lenses read from the same findings set, adding a new lens (or adjusting an existing one) is purely a presentation task with no change to any analysis agent.

### 7.4 Orchestration and control flow

An **orchestrator** owns the pipeline. Its responsibilities are: detecting the input type and selecting the correct adapter; running the stages in dependency order; passing each stage's typed output to the next; persisting every intermediate state; handling failures (retry, fall back, or halt with a precise error); and parallelising independent work — for example, the four analysis agents in Layer 4 can run concurrently because they all read the same enriched document and do not depend on each other.

The control flow for a single statement is: detect format → select adapter → extract → normalise → validate (halt on failure) → categorise → tag related parties → run the four analysts in parallel → assemble findings → generate the requested lens output. For batch use (e.g. an angel network screening many companies), the orchestrator runs many such pipelines concurrently, one per statement, with a shared result store.

### 7.5 Persistence and auditability

Every stage writes its input and output to a result store keyed by document and stage. This is not optional bookkeeping — it is the mechanism that delivers the explainability the product depends on. When a report says a company is risky, the system can reconstruct the full chain: which transactions, flagged by which detector, on which enriched data, derived from which canonical records, extracted from which source rows. This audit trail is also what allows individual agents to be debugged and improved against real historical inputs.

### 7.6 Cross-cutting technical concerns

- **Security and isolation.** Sensitive financial data is encrypted in transit and at rest, access is controlled per user and per document, and the architecture supports a processing mode that retains no source data after analysis where a customer requires it. (Expanded in the data-security section.)
- **Cost and latency control.** Model-driven agents are the expensive stages; the system minimises their use through deterministic-first design, caching of categorisation results, and per-bank normalisation profiles that avoid repeated inference.
- **Versioning.** Agents, jurisdiction modules, and the canonical schema are versioned, so a re-run of an old analysis can reproduce the original result, and so improvements can be rolled out per agent without destabilising the pipeline.
- **Observability.** Each stage emits structured logs and metrics (success rate, latency, flag counts), making pipeline health and extraction quality measurable rather than anecdotal.

---

## 8. Build phasing

The architecture is comprehensive from the start. The *shipped product* is built in deliberate phases so there is always something working and demonstrable. Each phase is a defensible stopping point that delivers real value to a specific investor type.

### Phase 0 — Foundations (planning → first code)
Lock the canonical transaction schema, choose the first target market (which determines the first jurisdiction module's rules), and resolve the data-security model (Section 10). These three decisions gate everything else.

### Phase 1 — The spine: PDF → angel lens
Build PDF adapter → normaliser → validator → categoriser → financial-health analyst → angel-lens output. This is the thinnest complete path from "upload a PDF" to "useful one-page summary." It proves the whole pipeline works end to end, serves the simplest investor user (the individual angel), and is immediately demonstrable.

### Phase 2 — Risk, depth, and the VC lens
Add the risk/red-flag analyst and the related-party tagger. Add the VC lens (deeper workbook + risk score + transaction-level backup). This is where the product becomes genuinely valuable rather than just convenient, and where the unusual-transaction detection lives.

### Phase 3 — Compliance, reconciliation, and the finance-team workbench
Add the compliance analyst (with the first jurisdiction module) and the reconciliation analyst, with their outputs surfaced as investor risk signals. Add the finance-team analyst workbench lens — the deepest output with transaction-level drill-down, export, and customisation. After this phase, the heaviest investor users (in-house analysts at funds) are properly served.

### Phase 4 — Network batch lens, format breadth, and scale
Add the angel-network batch lens (standardised cross-company comparison). Add remaining format adapters (Excel, CSV, scanned/OCR, then API feeds). Harden for volume — concurrent processing, many companies at once. This is the phase that makes the product usable at network or fund scale.

### Phase 5 — Expansion
Additional jurisdiction modules (each as a new plug-in ruleset), deeper integrations (CRM, deal-flow tools, fund-admin systems), and any vertical-specific tuning the market pulls you toward.

The sequencing principle: **prove the spine before adding breadth.** Each phase ships something real to a specific investor type; none requires the next to be useful.

---

## 9. The market and jurisdiction decision (open)

This is the single biggest open decision and it shapes the entire compliance layer of the product.

**Compliance logic is jurisdiction-specific.** Indian rules (GST, income-tax norms, cash-transaction limits under the Income Tax Act, suspicious-transaction reporting) are completely different from US rules (IRS, BSA/AML, SAR thresholds). The financial-health and risk layers are largely universal — burn rate is burn rate anywhere — but the compliance module is not. And the regulatory-risk signals surfaced to investors are only as useful as the jurisdiction module they come from.

The architecture handles this cleanly: **each jurisdiction is a swappable compliance module** plugged into Layer 4. So the platform can support many jurisdictions over time. But the *first* one built determines the first market, the first investor customers, and the regulatory homework required up front.

There is a genuine strategic choice between an India-first launch (clearer early access to domestic angel-network deal flow and well-understood local rules) and a US-first launch (a larger investor market with a different competitive set). The plan is built to support either; only the order in which jurisdiction modules are built changes. This decision should be made early, as it unblocks the compliance work and informs the competitive survey.

---

## 10. Data security and trust (non-negotiable)

Bank statements are among the most sensitive data a company holds. For a product that goes to market handling them, security is not a later feature — it is a day-one requirement that investor customers will ask about in the first meeting. "We hadn't thought about it" loses deals, particularly with institutional investors whose own LPs and compliance officers will demand answers.

The plan must address, at minimum: how data is stored and for how long, who can access it, whether data is encrypted at rest and in transit, how consent is captured from the investee company whose statements are being analysed, and whether sensitive data can be processed without being retained. There is also a meaningful design choice between processing data in the cloud versus keeping it within a customer's own environment, which more conservative institutional investors will demand.

This deserves its own dedicated planning session and likely professional input. It is flagged here as a first-class workstream, not an afterthought.

---

## 11. Business and go-to-market

### 11.1 The wedge

Even though the full product serves four investor user types, going to market means leading with one. The strongest wedge is **individual angel investors and angel networks**, for three reasons: it is the narrowest, clearest output (a one-page summary is easy to demo and easy to value); the red-flag and compliance-risk detection that all investors want is the same engine that the deeper lenses depend on, so building the wedge builds the foundation for the rest; and angels and networks are the easiest segment to reach through warm introductions. VC and finance-team adoption then expand from that beachhead, because the finance team inside a VC fund is the natural buyer once individual partners have already used the angel or VC lens.

### 11.2 Who pays, and how

Several plausible models, to be tested:
- **Per-analysis / per-company pricing** — natural for individual angels doing occasional diligence.
- **Subscription** — for angel networks, VCs, and finance teams running steady deal flow.
- **Tiered by depth** — angel summary at one price, VC workbook and finance-team workbench at higher tiers, with batch/network pricing as its own tier.
- **Seat-based for finance teams** — multiple analysts in one fund, each with workbench access.

The right model depends on which investor type converts first and how heavily they use it, which is itself a reason to run the angel wedge and watch what people will pay for.

### 11.3 Distribution

Warm network access is the strongest early advantage. Established angel networks and incubator relationships are direct channels to the first wave of investor users. The first cohort of customers should come from warm introductions rather than cold marketing; once angels are using and talking about it, the networks they belong to become natural distribution, and the VCs they co-invest with become the next tier.

### 11.4 Competitive landscape (to research)

Before building, a real survey of existing bank-statement-analysis tools is worth doing — lenders already use statement analysers for loan underwriting, and there are diligence and back-office tools in adjacent space. The differentiator here is the **agentic, multi-lens, investor-specific, explainable** approach: every analysis is built for an investor's decision, every finding is traceable to the transactions that drove it, and the same engine serves an angel, a network, a VC, and a finance team. This needs to be validated against what actually exists in the chosen market.

---

## 12. Key risks

- **Extraction reliability.** If the system mis-reads a statement, every downstream conclusion is wrong. The validator (Section 5, Layer 2; Section 7.3) is the primary defence and must be rigorous.
- **Trust and explainability.** Investors will not act on a black-box verdict about money. The agent pipeline must be able to show its work — which transactions drove which conclusion.
- **Regulatory liability.** The product surfaces facts and flags; it must not be positioned as giving regulatory or investment advice it isn't licensed to give. The non-goals in Section 4.3 are a deliberate liability boundary.
- **Data security incident.** Mishandling bank data would be fatal to the business. See Section 10.
- **Scope creep.** Trying to ship all four lenses and all formats at once produces a mediocre everything. The phasing in Section 8 is the discipline against this.

---

## 13. Immediate next steps

1. **Decide the first market** (India vs US) — this unblocks the first jurisdiction module and most of the plan.
2. **Run a competitive survey** of existing statement-analysis and diligence tools in that market.
3. **Hold a dedicated data-security planning session** and define the storage, consent, and processing model.
4. **Lock the canonical transaction schema** — the contract every layer depends on.
5. **Specify Phase 1 in detail** — the thin spine from PDF upload to angel summary.

---

*This document is a living plan. The architecture is fixed; the phasing and market decisions are meant to be debated and refined before any code is written.*
