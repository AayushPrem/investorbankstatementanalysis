"""Tests for the CSV adapter (Sprint 3, Step 3.6)."""
from __future__ import annotations

from pathlib import Path

import pytest

from adapters import AdapterError, CSVAdapter

_HEADER = "Date,Narration,Chq./Ref.No.,Withdrawal Amt.,Deposit Amt.,Closing Balance"
_ROWS = [
    "01/04/24,NEFT CR-ACME PVT LTD-INV-1,REF001,,50000.00,150000.00",
    "03/04/24,UPI-VENDOR PAYMENT-XYZ,REF002,12000.00,,138000.00",
    "05/04/24,IMPS-SALARY-STAFF,REF003,25000.00,,113000.00",
]


def _write_csv(path: Path, header: str, rows: list[str], preamble: list[str] | None = None,
               encoding: str = "utf-8") -> Path:
    lines = (preamble or []) + [header] + rows
    path.write_text("\n".join(lines) + "\n", encoding=encoding)
    return path


class TestBasicExtraction:
    def test_extracts_all_transaction_rows(self, tmp_path: Path) -> None:
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, _ROWS)
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3

    def test_fields_mapped_correctly(self, tmp_path: Path) -> None:
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, _ROWS)
        stmt = CSVAdapter().extract(csv_path)
        row = stmt.rows[0]
        assert row.date_raw == "01/04/24"
        assert "ACME" in row.description_raw
        assert row.credit_raw == "50000.00"
        assert row.debit_raw is None
        assert row.balance_raw == "150000.00"

    def test_debit_row_mapped_correctly(self, tmp_path: Path) -> None:
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, _ROWS)
        stmt = CSVAdapter().extract(csv_path)
        row = stmt.rows[1]
        assert row.debit_raw == "12000.00"
        assert row.credit_raw is None

    def test_bank_name_unknown(self, tmp_path: Path) -> None:
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, _ROWS)
        stmt = CSVAdapter().extract(csv_path)
        assert stmt.bank_name == "UNKNOWN"


class TestDelimiterDetection:
    def test_semicolon_delimiter(self, tmp_path: Path) -> None:
        header = _HEADER.replace(",", ";")
        rows = [r.replace(",", ";") for r in _ROWS]
        csv_path = _write_csv(tmp_path / "stmt.csv", header, rows)
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3

    def test_pipe_delimiter(self, tmp_path: Path) -> None:
        header = _HEADER.replace(",", "|")
        rows = [r.replace(",", "|") for r in _ROWS]
        csv_path = _write_csv(tmp_path / "stmt.csv", header, rows)
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3


class TestEncodingDetection:
    def test_utf8_sig_bom(self, tmp_path: Path) -> None:
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, _ROWS, encoding="utf-8-sig")
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3
        assert stmt.rows[0].date_raw == "01/04/24"

    def test_cp1252(self, tmp_path: Path) -> None:
        rows = list(_ROWS)
        rows[0] = rows[0].replace("ACME PVT LTD", "ACME – PVT LTD")  # en-dash, non-UTF8-safe
        csv_path = tmp_path / "stmt.csv"
        content = "\n".join([_HEADER] + rows) + "\n"
        csv_path.write_bytes(content.encode("cp1252"))
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3


class TestHeaderNotFirstRow:
    def test_header_after_preamble(self, tmp_path: Path) -> None:
        preamble = ["Account Statement", "Account No: 1234567890", "Period: Apr 2024", ""]
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, _ROWS, preamble=preamble)
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3
        assert stmt.rows[0].date_raw == "01/04/24"


class TestFooterHandling:
    def test_summary_footer_row_skipped(self, tmp_path: Path) -> None:
        rows = list(_ROWS) + ["Total,,,,37000.00,"]
        csv_path = _write_csv(tmp_path / "stmt.csv", _HEADER, rows)
        stmt = CSVAdapter().extract(csv_path)
        assert len(stmt.rows) == 3


class TestErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AdapterError):
            CSVAdapter().extract(tmp_path / "does_not_exist.csv")

    def test_no_header_raises(self, tmp_path: Path) -> None:
        csv_path = tmp_path / "junk.csv"
        csv_path.write_text("foo,bar,baz\n1,2,3\n", encoding="utf-8")
        with pytest.raises(AdapterError):
            CSVAdapter().extract(csv_path)
