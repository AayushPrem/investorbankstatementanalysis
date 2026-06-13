"""Shared types for all PDF adapters."""
from __future__ import annotations

from dataclasses import dataclass, field


class AdapterError(Exception):
    """Raised when a PDF cannot be opened or parsed by the adapter."""


@dataclass
class RawRow:
    """One extracted transaction row in bank-native format (pre-normalisation)."""
    page: int
    row_idx: int          # zero-indexed within the page's table
    raw_text: str         # full row joined as pipe-delimited string (for SourceReference)
    date_raw: str         # e.g. "01/04/24" (HDFC) or "01-04-2024" (ICICI)
    description_raw: str
    debit_raw: str | None   # amount string with commas, e.g. "1,20,000.00"; None if not a debit
    credit_raw: str | None  # amount string with commas; None if not a credit
    balance_raw: str | None # closing balance string with commas
    ref_no_raw: str | None = None  # Chq./Ref.No. (HDFC) or Ref No./Cheque No. (ICICI)


@dataclass
class RawStatement:
    """All raw rows extracted from one bank statement PDF."""
    source_path: str
    bank_name: str              # "HDFC" | "ICICI" | "UNKNOWN"
    account_id: str | None
    period_start_raw: str | None  # raw string from PDF header, e.g. "01/04/2024"
    period_end_raw: str | None
    opening_balance_raw: str | None
    rows: list[RawRow] = field(default_factory=list)
