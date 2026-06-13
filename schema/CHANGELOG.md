# Schema Changelog

## v1 — 2026-06-13 — FROZEN

Initial canonical transaction schema. This is the central data contract for the entire pipeline.

**Rule: any field addition, removal, rename, or type change requires a version bump.**

Downstream agents must not be updated to handle a new field until the schema version is bumped and this changelog is updated. This discipline keeps the contract meaningful.

### Models

**SourceReference**
- `file_path: str`
- `page: int`
- `row: int` — zero-indexed within the page
- `raw_text: str` — original row text for debugging and traceability

**TransactionCategory** (enum)
- `REVENUE`, `VENDOR_PAYMENT`, `SALARY`, `LOAN_IN`, `LOAN_OUT`, `TAX`, `TRANSFER`, `FOUNDER_WITHDRAWAL`, `FEES`, `OTHER`

**ValidationStatus** (enum)
- `unvalidated` (default), `passed`, `failed`

**CanonicalTransaction**
- `transaction_id: str` — stable unique identifier within an analysis
- `date: datetime.date` — transaction date, normalised
- `description: str` — raw description, preserved exactly
- `debit: Decimal | None` — positive number, or None if credit row
- `credit: Decimal | None` — positive number, or None if debit row
- `balance: Decimal` — running balance after this transaction
- `currency: str` — default "INR"
- `source_reference: SourceReference`
- Enrichment slots (all optional, filled by later agents):
  - `category: TransactionCategory | None`
  - `counterparty: str | None`
  - `is_recurring: bool | None`
  - `is_related_party: bool | None`
  - `related_party_match: str | None`
  - `customer_id: str | None`
  - `anomaly_flags: list[str]` — default empty list

**Invariant:** exactly one of `debit` / `credit` must be set. Both None and both set are validation errors enforced at construction time.

**StatementDocument**
- `account_id: str`
- `statement_period_start: datetime.date`
- `statement_period_end: datetime.date`
- `source_format: str`
- `bank_name: str | None`
- `validation_status: ValidationStatus` — default `unvalidated`
- `transactions: list[CanonicalTransaction]` — default empty list
