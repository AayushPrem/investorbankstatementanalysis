"""Header-based column mapping shared by adapters that see column names
(CSV, Excel) — unlike the digital PDF adapter, these formats have no fixed
per-bank layout, so columns are matched by keyword instead of position.
"""
from __future__ import annotations

import re

# Ordered so the most specific keyword wins when a header matches several
# (e.g. "withdrawal amt" must not be captured by a generic "amount" rule).
_FIELD_KEYWORDS: dict[str, tuple[str, ...]] = {
    "date": ("txn date", "transaction date", "value date", "date"),
    "description": (
        "narration", "particulars", "description", "details", "remarks",
    ),
    "debit": ("withdrawal", "debit", "dr amount", "dr"),
    "credit": ("deposit", "credit", "cr amount", "cr"),
    "balance": ("closing balance", "balance"),
    "ref_no": ("cheque", "chq", "ref no", "reference", "ref."),
}

# Fields that must all be present for a row to be accepted as a header row.
_REQUIRED_FIELDS = ("date", "debit", "credit", "balance")

_DATE_RE = re.compile(r"\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}")
_AMOUNT_RE = re.compile(r"^-?\d+(\.\d+)?$")


def match_field(header_text: str) -> str | None:
    """Return the canonical field name a header cell most likely represents."""
    text = header_text.strip().lower()
    if not text:
        return None
    for field_name, keywords in _FIELD_KEYWORDS.items():
        for kw in keywords:
            if kw in text:
                return field_name
    return None


def build_column_map(header_cells: list[str]) -> dict[str, int]:
    """Map canonical field name -> column index, first match wins per field."""
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(header_cells):
        field_name = match_field(cell)
        if field_name and field_name not in mapping:
            mapping[field_name] = idx
    return mapping


def is_header_row(cells: list[str]) -> bool:
    """True if this row's cells map to (at least) all required fields."""
    mapping = build_column_map(cells)
    return all(f in mapping for f in _REQUIRED_FIELDS)


def looks_like_data_row(cells: list[str], column_map: dict[str, int]) -> bool:
    """True if this row has a parseable date and at least one parseable amount
    in the positions given by *column_map* — used to confirm a header guess
    and to skip footer/summary rows once inside the data region.
    """
    date_idx = column_map.get("date")
    if date_idx is None or date_idx >= len(cells):
        return False
    if not _DATE_RE.search(cells[date_idx]):
        return False
    for key in ("debit", "credit", "balance"):
        idx = column_map.get(key)
        if idx is not None and idx < len(cells) and _AMOUNT_RE.match(_clean_amount(cells[idx])):
            return True
    return False


def _clean_amount(s: str) -> str:
    return s.replace("₹", "").replace(",", "").strip()
