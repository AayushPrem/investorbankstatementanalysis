"""CSV adapter — extracts raw transaction rows from a bank-exported CSV file.

Unlike the digital PDF adapter (which knows the exact HDFC/ICICI column
layout ahead of time), a CSV export can come from any bank with any column
order, so columns are matched by header keyword via adapters._column_mapping.

Handles:
  - Delimiter auto-detection (comma, semicolon, tab, pipe)
  - Encoding auto-detection (utf-8-sig, utf-8, cp1252)
  - Header row that isn't row 1 (some exports prepend a title/metadata block)

Output is a RawStatement of RawRows — same shape as every other adapter.
"""
from __future__ import annotations

import csv as _csv
import io
import re
from pathlib import Path

from adapters._column_mapping import build_column_map, is_header_row, looks_like_data_row
from adapters.base import AdapterError, RawRow, RawStatement

_ENCODINGS = ("utf-8-sig", "utf-8", "cp1252")
_MAX_HEADER_SCAN_ROWS = 20
_SKIP_DESCRIPTIONS = frozenset({"opening balance", "closing balance"})


class CSVAdapter:
    """Extracts raw transaction rows from a bank statement CSV export."""

    def extract(self, csv_path: str | Path) -> RawStatement:
        csv_path = Path(csv_path)
        if not csv_path.exists():
            raise AdapterError(f"File not found: {csv_path}")

        text = _read_text(csv_path)
        delimiter = _sniff_delimiter(text)

        reader = _csv.reader(io.StringIO(text), delimiter=delimiter)
        all_rows = [row for row in reader if any(c.strip() for c in row)]
        if not all_rows:
            raise AdapterError(f"No data found in {csv_path.name}")

        header_idx, column_map = _find_header(all_rows)
        if column_map is None:
            raise AdapterError(
                f"Could not detect a Date/Debit/Credit/Balance header row in {csv_path.name}"
            )

        rows = _extract_rows(all_rows[header_idx + 1 :], column_map)
        return RawStatement(
            source_path=str(csv_path),
            bank_name="UNKNOWN",
            account_id=None,
            period_start_raw=None,
            period_end_raw=None,
            opening_balance_raw=None,
            rows=rows,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Encoding + delimiter detection
# ─────────────────────────────────────────────────────────────────────────────

def _read_text(csv_path: Path) -> str:
    raw = csv_path.read_bytes()
    last_error: UnicodeDecodeError | None = None
    for enc in _ENCODINGS:
        try:
            return raw.decode(enc)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise AdapterError(f"Could not decode {csv_path.name} with any of {_ENCODINGS}: {last_error}")


def _sniff_delimiter(text: str) -> str:
    sample = "\n".join(text.splitlines()[:10])
    try:
        return _csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except _csv.Error:
        return ","  # fall back to the overwhelmingly common case


# ─────────────────────────────────────────────────────────────────────────────
# Header detection
# ─────────────────────────────────────────────────────────────────────────────

def _find_header(all_rows: list[list[str]]) -> tuple[int, dict[str, int] | None]:
    for idx, row in enumerate(all_rows[:_MAX_HEADER_SCAN_ROWS]):
        if is_header_row(row):
            return idx, build_column_map(row)
    return -1, None


# ─────────────────────────────────────────────────────────────────────────────
# Row extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_rows(data_rows: list[list[str]], column_map: dict[str, int]) -> list[RawRow]:
    rows: list[RawRow] = []
    for row_idx, cells in enumerate(data_rows):
        parsed = _parse_row(cells, column_map, row_idx)
        if parsed is not None:
            rows.append(parsed)
    return rows


def _parse_row(cells: list[str], column_map: dict[str, int], row_idx: int) -> RawRow | None:
    if not looks_like_data_row(cells, column_map):
        return None  # footer/summary row past the transaction table

    description = _cell(cells, column_map.get("description"))
    if description.lower() in _SKIP_DESCRIPTIONS:
        return None

    date_raw = _cell(cells, column_map["date"])
    debit_raw = _clean_amount(_cell(cells, column_map.get("debit")))
    credit_raw = _clean_amount(_cell(cells, column_map.get("credit")))
    balance_raw = _clean_amount(_cell(cells, column_map.get("balance")))

    return RawRow(
        page=0,
        row_idx=row_idx,
        raw_text=" | ".join(cells),
        date_raw=date_raw,
        description_raw=description,
        debit_raw=debit_raw or None,
        credit_raw=credit_raw or None,
        balance_raw=balance_raw or None,
        ref_no_raw=_cell(cells, column_map.get("ref_no")) or None,
    )


def _cell(cells: list[str], idx: int | None) -> str:
    if idx is None or idx >= len(cells):
        return ""
    return cells[idx].strip()


_NON_AMOUNT_CHARS = re.compile(r"[₹,\s]")


def _clean_amount(s: str) -> str:
    return _NON_AMOUNT_CHARS.sub("", s)
