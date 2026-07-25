"""Excel adapter — extracts raw transaction rows from a bank statement .xlsx/.xls.

Handles:
  - Multi-sheet workbooks: scores every sheet by how well it matches a
    Date/Debit/Credit/Balance header and picks the best one.
  - Header row that isn't row 1 (title/logo/account-summary rows commonly
    precede the transaction table in bank exports).
  - Merged cells: openpyxl only stores a value in the top-left cell of a
    merged range, so merged cells are resolved before header/column matching.

Output is a RawStatement of RawRows — same shape as every other adapter.
"""
from __future__ import annotations

from pathlib import Path

from openpyxl import load_workbook
from openpyxl.worksheet.worksheet import Worksheet

from adapters._column_mapping import build_column_map, is_header_row, looks_like_data_row
from adapters.base import AdapterError, RawRow, RawStatement

_MAX_HEADER_SCAN_ROWS = 20
_SKIP_DESCRIPTIONS = frozenset({"opening balance", "closing balance"})


class ExcelAdapter:
    """Extracts raw transaction rows from a bank statement Excel workbook."""

    def extract(self, xlsx_path: str | Path) -> RawStatement:
        xlsx_path = Path(xlsx_path)
        if not xlsx_path.exists():
            raise AdapterError(f"File not found: {xlsx_path}")

        try:
            wb = load_workbook(str(xlsx_path), data_only=True, read_only=False)
        except Exception as exc:
            raise AdapterError(f"openpyxl could not read {xlsx_path.name}: {exc}") from exc

        best: tuple[Worksheet, int, dict[str, int]] | None = None
        for ws in wb.worksheets:
            grid = _resolved_grid(ws)
            header_idx, column_map = _find_header(grid)
            if column_map is not None and (best is None or len(column_map) > len(best[2])):
                best = (ws, header_idx, column_map)

        if best is None:
            raise AdapterError(
                f"No sheet in {xlsx_path.name} has a detectable "
                "Date/Debit/Credit/Balance header row"
            )

        ws, header_idx, column_map = best
        grid = _resolved_grid(ws)
        rows = _extract_rows(grid[header_idx + 1 :], column_map)

        return RawStatement(
            source_path=str(xlsx_path),
            bank_name="UNKNOWN",
            account_id=None,
            period_start_raw=None,
            period_end_raw=None,
            opening_balance_raw=None,
            rows=rows,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Merged-cell resolution
# ─────────────────────────────────────────────────────────────────────────────

def _resolved_grid(ws: Worksheet) -> list[list[str]]:
    """Return sheet contents as strings, with merged-range values broadcast to
    every cell in the range so column-index-based lookups work uniformly.
    """
    values: list[list[str]] = [
        ["" if c is None else str(c).strip() for c in row]
        for row in ws.iter_rows(values_only=True)
    ]
    for merged_range in ws.merged_cells.ranges:
        fill_value = values[merged_range.min_row - 1][merged_range.min_col - 1]
        for r in range(merged_range.min_row - 1, merged_range.max_row):
            for c in range(merged_range.min_col - 1, merged_range.max_col):
                if r < len(values) and c < len(values[r]):
                    values[r][c] = fill_value
    return values


# ─────────────────────────────────────────────────────────────────────────────
# Header detection
# ─────────────────────────────────────────────────────────────────────────────

def _find_header(grid: list[list[str]]) -> tuple[int, dict[str, int] | None]:
    for idx, row in enumerate(grid[:_MAX_HEADER_SCAN_ROWS]):
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
        return None

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


def _clean_amount(s: str) -> str:
    return s.replace("₹", "").replace(",", "").strip()
