"""Parquet schemas for the corpus: feature tables vs label tables.

Feature tables hold only what a bank statement shows. Every ground-truth or
generator-internal field lives in a label table under ``labels/``. The only
columns the two sides share are the join keys in :data:`JOIN_KEYS`;
tests/test_corpus.py asserts that nothing else overlaps and that no feature
column carries a label name.
"""
from __future__ import annotations

import pyarrow as pa

CORPUS_VERSION = "v1"

MONEY = pa.decimal128(18, 2)

# ── features/ ───────────────────────────────────────────────────────────────
FEATURE_TRANSACTIONS = pa.schema([
    ("txn_id", pa.string()),
    ("company_id", pa.string()),
    ("account_id", pa.string()),
    ("bank", pa.string()),
    ("date", pa.date32()),
    ("txn_time", pa.time32("s")),
    ("value_date", pa.date32()),
    ("description", pa.string()),
    ("ref_no", pa.string()),
    ("debit", MONEY),
    ("credit", MONEY),
    ("balance", MONEY),
    ("row_seq", pa.int32()),
])

FEATURE_COMPANIES = pa.schema([
    ("company_id", pa.string()),
    ("bank", pa.string()),
    ("account_id", pa.string()),
    ("account_holder", pa.string()),
    ("period_start", pa.date32()),
    ("period_end", pa.date32()),
    ("opening_balance", MONEY),
])

# ── labels/ ─────────────────────────────────────────────────────────────────
LABEL_TRANSACTIONS = pa.schema([
    ("txn_id", pa.string()),
    ("company_id", pa.string()),
    ("true_category", pa.string()),
    ("template_id", pa.string()),
    ("anomaly_type", pa.string()),
    ("anomaly_kind", pa.string()),
    ("anomaly_flags", pa.list_(pa.string())),
    ("true_customer_id", pa.string()),
    ("counterparty_key", pa.string()),
    ("is_related_party", pa.bool_()),
])

LABEL_CUSTOMERS = pa.schema([
    ("company_id", pa.string()),
    ("customer_id", pa.string()),
    ("customer_name", pa.string()),
    ("health", pa.float64()),
    ("acquired_month", pa.int32()),
    ("churn_month", pa.int32()),
    ("churn_month_start", pa.date32()),
    ("signaller", pa.bool_()),
    ("lead_months", pa.int32()),
    ("n_payments", pa.int32()),
    ("last_payment_date", pa.date32()),
])

LABEL_COMPANIES = pa.schema([
    ("company_id", pa.string()),
    ("profile", pa.string()),
    ("bank", pa.string()),
    ("seed", pa.string()),
    ("n_months", pa.int32()),
    ("start", pa.date32()),
    ("revenue_scale", pa.float64()),
    ("n_customers", pa.int32()),
    ("rule_flags", pa.list_(pa.string())),
    ("ml_flags", pa.list_(pa.string())),
    ("pdf_rendered", pa.bool_()),
])

LABEL_FLAGS = pa.schema([
    ("company_id", pa.string()),
    ("flag_type", pa.string()),
    ("kind", pa.string()),
    ("flag_description", pa.string()),
    ("triggering_txn_ids", pa.list_(pa.string())),
])

FEATURE_SCHEMAS: dict[str, pa.Schema] = {
    "transactions": FEATURE_TRANSACTIONS,
    "companies": FEATURE_COMPANIES,
}
LABEL_SCHEMAS: dict[str, pa.Schema] = {
    "transactions": LABEL_TRANSACTIONS,
    "customers": LABEL_CUSTOMERS,
    "companies": LABEL_COMPANIES,
    "flags": LABEL_FLAGS,
}

# Columns allowed on both sides (identifiers / statement facts, not labels).
JOIN_KEYS = frozenset({"txn_id", "company_id", "bank"})

# Substrings that must never appear in a feature column name.
FORBIDDEN_FEATURE_TOKENS = (
    "category", "customer", "template", "anomaly", "profile", "health", "churn",
    "related", "counterparty", "kind", "signaller", "flag", "label", "truth", "seed",
    "scale",
)
