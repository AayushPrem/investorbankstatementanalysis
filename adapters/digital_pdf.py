"""Digital PDF adapter — extracts raw transaction rows from HDFC and ICICI PDFs.

Handles:
  - HDFC  7-column format  (Date | Narration | Chq./Ref.No. | Value Dt | Withdrawal(Dr) | Deposit(Cr) | Balance)
  - ICICI 8-column format  (S No. | Txn Date | Value Date | Description | Ref No. | Debit | Credit | Balance)

Output is a RawStatement of RawRows — bank-native strings, no parsing yet.
The Normaliser (Step 1.6) converts these to CanonicalTransaction objects.
"""
from __future__ import annotations

import re
from pathlib import Path

try:
    import pdfplumber  # type: ignore[import-untyped]
except ImportError as exc:
    raise ImportError("pdfplumber is required: pip install pdfplumber") from exc

from adapters.base import AdapterError, RawRow, RawStatement

# ─────────────────────────────────────────────────────────────────────────────
# Column index maps (zero-based)
# ─────────────────────────────────────────────────────────────────────────────

_HDFC = {"date": 0, "narration": 1, "ref_no": 2, "debit": 4, "credit": 5, "balance": 6}
_ICICI = {"txn_date": 1, "description": 3, "ref_no": 4, "debit": 5, "credit": 6, "balance": 7}

# Rows whose description signals a non-transaction entry to skip
_SKIP_DESCRIPTIONS = frozenset({"opening balance", "closing balance"})


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

class DigitalPDFAdapter:
    """Extracts raw transaction rows from digital bank statement PDFs."""

    def extract(self, pdf_path: str | Path) -> RawStatement:
        """Open *pdf_path* and return all transaction rows as a RawStatement.

        Raises AdapterError on missing file or unreadable PDF.
        """
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise AdapterError(f"File not found: {pdf_path}")

        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                pages_text = [p.extract_text() or "" for p in pdf.pages]
                page_tables: list[tuple[int, list[list[str | None]]]] = []
                for page_num, page in enumerate(pdf.pages):
                    for table in (page.extract_tables() or []):
                        page_tables.append((page_num, table))
        except Exception as exc:
            raise AdapterError(f"pdfplumber could not read {pdf_path.name}: {exc}") from exc

        bank = _detect_bank(pages_text)
        return RawStatement(
            source_path=str(pdf_path),
            bank_name=bank,
            account_id=_extract_account_id(pages_text, bank),
            period_start_raw=_extract_period(pages_text)[0],
            period_end_raw=_extract_period(pages_text)[1],
            opening_balance_raw=_extract_opening_balance(pages_text),
            rows=_extract_rows(page_tables, bank),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bank detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_bank(pages_text: list[str]) -> str:
    # Restrict to the page-1 document header (first 300 chars) so that
    # "HDFC" appearing inside ICICI narrations (e.g. "NEFT CR-HDFC...") does
    # not trigger false detection.
    header = (pages_text[0][:300] if pages_text else "").lower()
    if "icici bank" in header:
        return "ICICI"
    if "hdfc bank" in header:
        return "HDFC"
    # Fallback: search all pages for the bank name as a whole phrase
    for text in pages_text:
        t = text.lower()
        if "icici bank" in t:
            return "ICICI"
        if "hdfc bank" in t:
            return "HDFC"
    return "UNKNOWN"


# ─────────────────────────────────────────────────────────────────────────────
# Header field extraction (from page text, not table)
# ─────────────────────────────────────────────────────────────────────────────

_RE_HDFC_ACCT = re.compile(r"account\s+no[.:]?\s*([0-9]+)", re.IGNORECASE)
_RE_ICICI_ACCT = re.compile(r"account\s+number[.:]?\s*([0-9]+)", re.IGNORECASE)
_RE_HDFC_PERIOD = re.compile(
    r"statement\s+period[.:]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+to\s+(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
    re.IGNORECASE,
)
_RE_ICICI_PERIOD = re.compile(
    r"statement\s+from[.:]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\s+to[.:]?\s*(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})",
    re.IGNORECASE,
)
_RE_OPENING = re.compile(
    # Skip any non-amount tokens (e.g. a date "01/04/24") with .*? before
    # matching the first proper decimal amount "15,00,000.00".
    r"opening\s+balance\s+.*?([0-9,]+\.[0-9]{2})",
    re.IGNORECASE,
)


def _extract_account_id(pages_text: list[str], bank: str) -> str | None:
    pattern = _RE_HDFC_ACCT if bank == "HDFC" else _RE_ICICI_ACCT
    for text in pages_text:
        m = pattern.search(text)
        if m:
            return m.group(1).strip()
    return None


def _extract_period(pages_text: list[str]) -> tuple[str | None, str | None]:
    for text in pages_text:
        for pattern in (_RE_HDFC_PERIOD, _RE_ICICI_PERIOD):
            m = pattern.search(text)
            if m:
                return m.group(1), m.group(2)
    return None, None


def _extract_opening_balance(pages_text: list[str]) -> str | None:
    for text in pages_text:
        m = _RE_OPENING.search(text)
        if m:
            return m.group(1).replace(",", "")
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Table row extraction
# ─────────────────────────────────────────────────────────────────────────────

def _cells(raw_row: list[str | None]) -> list[str]:
    return [str(c or "").strip() for c in raw_row]


def _is_hdfc_header(row_cells: list[str]) -> bool:
    """True if this is the HDFC column-header row."""
    if len(row_cells) < 7:
        return False
    first = row_cells[0].lower().strip()
    second = row_cells[1].lower().strip()
    return first == "date" and "narration" in second


def _is_icici_header(row_cells: list[str]) -> bool:
    """True if this is the ICICI column-header row (header text may be garbled by pdfplumber)."""
    if len(row_cells) < 7:
        return False
    joined = " ".join(c.lower() for c in row_cells)
    return "debit" in joined and "credit" in joined and "description" in joined


def _extract_rows(
    page_tables: list[tuple[int, list[list[str | None]]]],
    bank: str,
) -> list[RawRow]:
    rows: list[RawRow] = []
    seen_header = False

    for page_num, table in page_tables:
        for row_idx, raw_row in enumerate(table):
            cs = _cells(raw_row)

            # Skip column-header rows (repeated on every page via repeatRows=1)
            if bank == "HDFC" and _is_hdfc_header(cs):
                seen_header = True
                continue
            if bank == "ICICI" and _is_icici_header(cs):
                seen_header = True
                continue

            if not seen_header:
                continue  # pre-header content (should not happen in well-formed PDFs)

            parsed = _parse_row(cs, bank, page_num, row_idx)
            if parsed is not None:
                rows.append(parsed)

    return rows


def _parse_row(
    cs: list[str],
    bank: str,
    page: int,
    row_idx: int,
) -> RawRow | None:
    if bank == "HDFC":
        return _parse_hdfc(cs, page, row_idx)
    if bank == "ICICI":
        return _parse_icici(cs, page, row_idx)
    return None


def _parse_hdfc(cs: list[str], page: int, row_idx: int) -> RawRow | None:
    if len(cs) < 7:
        return None
    date_raw = cs[_HDFC["date"]]
    if not date_raw or not re.match(r"\d{2}/\d{2}/\d{2}", date_raw):
        return None  # not a transaction row (e.g. trailing empty row)

    narration = cs[_HDFC["narration"]]
    if narration.lower() in _SKIP_DESCRIPTIONS:
        return None  # opening / closing balance rows are metadata, not transactions

    withdrawal = _clean_amount(cs[_HDFC["debit"]])
    deposit = _clean_amount(cs[_HDFC["credit"]])
    balance = _clean_amount(cs[_HDFC["balance"]])

    return RawRow(
        page=page,
        row_idx=row_idx,
        raw_text=" | ".join(cs),
        date_raw=date_raw,
        description_raw=narration,
        debit_raw=withdrawal or None,
        credit_raw=deposit or None,
        balance_raw=balance or None,
        ref_no_raw=cs[_HDFC["ref_no"]] or None,
    )


def _parse_icici(cs: list[str], page: int, row_idx: int) -> RawRow | None:
    if len(cs) < 8:
        return None
    date_raw = cs[_ICICI["txn_date"]]
    if not date_raw or not re.match(r"\d{2}-\d{2}-\d{4}", date_raw):
        return None  # not a transaction row

    description = cs[_ICICI["description"]]
    if description.lower() in _SKIP_DESCRIPTIONS:
        return None

    debit = _clean_amount(cs[_ICICI["debit"]])
    credit = _clean_amount(cs[_ICICI["credit"]])
    balance = _clean_amount(cs[_ICICI["balance"]])

    return RawRow(
        page=page,
        row_idx=row_idx,
        raw_text=" | ".join(cs),
        date_raw=date_raw,
        description_raw=description,
        debit_raw=debit or None,
        credit_raw=credit or None,
        balance_raw=balance or None,
        ref_no_raw=cs[_ICICI["ref_no"]] or None,
    )


def _clean_amount(s: str) -> str:
    """Strip commas and whitespace; return empty string if nothing left."""
    return s.replace(",", "").strip()
