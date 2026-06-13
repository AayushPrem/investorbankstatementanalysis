"""Tests for the digital PDF adapter (Step 1.5).

Each test generates a fresh synthetic statement in a temp directory so the suite
is self-contained — it does not rely on data/gold/ being pre-built.
"""
from __future__ import annotations

import json
import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

from adapters import AdapterError, DigitalPDFAdapter, RawRow, RawStatement
from tools.synthetic_gen import generate_statement

# ─── module-scoped fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("adapter_test")


@pytest.fixture(scope="module")
def hdfc_stmt(tmp_root: Path) -> tuple[RawStatement, dict]:  # type: ignore[type-arg]
    pdf, truth_path = generate_statement(
        bank="hdfc", profile="healthy_saas", output_dir=tmp_root,
        statement_id="adp_hdfc_test", flags=[], seed=42,
    )
    truth = json.loads(truth_path.read_text())
    stmt = DigitalPDFAdapter().extract(pdf)
    return stmt, truth


@pytest.fixture(scope="module")
def icici_stmt(tmp_root: Path) -> tuple[RawStatement, dict]:  # type: ignore[type-arg]
    pdf, truth_path = generate_statement(
        bank="icici", profile="burning_startup", output_dir=tmp_root,
        statement_id="adp_icici_test", flags=[], seed=42,
    )
    truth = json.loads(truth_path.read_text())
    stmt = DigitalPDFAdapter().extract(pdf)
    return stmt, truth


# ─── bank detection ───────────────────────────────────────────────────────────

class TestBankDetection:
    def test_hdfc_detected(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        assert stmt.bank_name == "HDFC"

    def test_icici_detected(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        assert stmt.bank_name == "ICICI"


# ─── metadata extraction ──────────────────────────────────────────────────────

class TestMetadataExtraction:
    def test_hdfc_account_id_extracted(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = hdfc_stmt
        assert stmt.account_id is not None
        assert stmt.account_id == truth["account_id"]

    def test_icici_account_id_extracted(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = icici_stmt
        assert stmt.account_id is not None
        assert stmt.account_id == truth["account_id"]

    def test_hdfc_period_extracted(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        assert stmt.period_start_raw is not None
        assert stmt.period_end_raw is not None

    def test_icici_period_extracted(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        assert stmt.period_start_raw is not None
        assert stmt.period_end_raw is not None

    def test_opening_balance_extracted(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = hdfc_stmt
        assert stmt.opening_balance_raw is not None
        # Truth stores opening balance as integer string without commas
        assert Decimal(stmt.opening_balance_raw) == Decimal(truth["opening_balance"])


# ─── row count ────────────────────────────────────────────────────────────────

class TestRowCount:
    def test_hdfc_row_count_matches_truth(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = hdfc_stmt
        expected = len(truth["transactions"])
        assert len(stmt.rows) == expected, (
            f"HDFC adapter extracted {len(stmt.rows)} rows, truth has {expected}"
        )

    def test_icici_row_count_matches_truth(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = icici_stmt
        expected = len(truth["transactions"])
        assert len(stmt.rows) == expected, (
            f"ICICI adapter extracted {len(stmt.rows)} rows, truth has {expected}"
        )

    def test_no_empty_row_list(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        assert len(stmt.rows) > 0


# ─── date fields ──────────────────────────────────────────────────────────────

class TestDateField:
    def test_hdfc_dates_match_dd_mm_yy(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        pattern = re.compile(r"^\d{2}/\d{2}/\d{2}$")
        for row in stmt.rows:
            assert pattern.match(row.date_raw), (
                f"HDFC date_raw '{row.date_raw}' does not match DD/MM/YY"
            )

    def test_icici_dates_match_dd_mm_yyyy(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        pattern = re.compile(r"^\d{2}-\d{2}-\d{4}$")
        for row in stmt.rows:
            assert pattern.match(row.date_raw), (
                f"ICICI date_raw '{row.date_raw}' does not match DD-MM-YYYY"
            )


# ─── description field ────────────────────────────────────────────────────────

class TestDescriptionField:
    def test_hdfc_descriptions_non_empty(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            assert row.description_raw.strip(), (
                f"Empty description on HDFC row page={row.page} idx={row.row_idx}"
            )

    def test_icici_descriptions_non_empty(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        for row in stmt.rows:
            assert row.description_raw.strip(), (
                f"Empty description on ICICI row page={row.page} idx={row.row_idx}"
            )

    def test_opening_balance_row_excluded(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        descriptions_lower = {r.description_raw.lower() for r in stmt.rows}
        assert "opening balance" not in descriptions_lower
        assert "closing balance" not in descriptions_lower


# ─── debit / credit exclusivity ──────────────────────────────────────────────

class TestDebitCreditExclusivity:
    def test_hdfc_not_both_set(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            assert not (row.debit_raw and row.credit_raw), (
                f"HDFC row has both debit and credit: {row.raw_text[:80]}"
            )

    def test_icici_not_both_set(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        for row in stmt.rows:
            assert not (row.debit_raw and row.credit_raw), (
                f"ICICI row has both debit and credit: {row.raw_text[:80]}"
            )

    def test_hdfc_at_least_one_set(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            assert row.debit_raw or row.credit_raw, (
                f"HDFC row has neither debit nor credit: {row.raw_text[:80]}"
            )

    def test_icici_at_least_one_set(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        for row in stmt.rows:
            assert row.debit_raw or row.credit_raw, (
                f"ICICI row has neither debit nor credit: {row.raw_text[:80]}"
            )


# ─── amount parseability ──────────────────────────────────────────────────────

def _to_decimal(s: str | None) -> Decimal | None:
    if not s:
        return None
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


class TestAmountParseability:
    def test_hdfc_balances_parse(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            val = _to_decimal(row.balance_raw)
            assert val is not None, (
                f"HDFC balance_raw '{row.balance_raw}' is not parseable"
            )
            assert val >= 0

    def test_icici_balances_parse(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        for row in stmt.rows:
            val = _to_decimal(row.balance_raw)
            assert val is not None, (
                f"ICICI balance_raw '{row.balance_raw}' is not parseable"
            )

    def test_hdfc_amounts_parse(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            for raw in (row.debit_raw, row.credit_raw):
                if raw:
                    val = _to_decimal(raw)
                    assert val is not None and val > 0, (
                        f"HDFC amount '{raw}' is not a positive number"
                    )

    def test_icici_amounts_parse(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = icici_stmt
        for row in stmt.rows:
            for raw in (row.debit_raw, row.credit_raw):
                if raw:
                    val = _to_decimal(raw)
                    assert val is not None and val > 0, (
                        f"ICICI amount '{raw}' is not a positive number"
                    )


# ─── amount accuracy against ground truth ────────────────────────────────────

class TestAmountAccuracy:
    """Verify extracted amounts match the ground truth within ₹1 tolerance."""

    def _amounts_close(self, raw: str | None, truth_str: str | None) -> bool:
        if truth_str is None and raw is None:
            return True
        if truth_str is None or raw is None:
            return False
        try:
            return abs(Decimal(raw.replace(",", "")) - Decimal(truth_str)) <= Decimal("1")
        except InvalidOperation:
            return False

    def _check_accuracy(
        self, rows: list[RawRow], truth_txns: list[dict]  # type: ignore[type-arg]
    ) -> None:
        # Zip in order — synthetic gen produces transactions in date order and the
        # adapter preserves page order, so positional alignment is reliable.
        assert len(rows) == len(truth_txns)
        for i, (row, t) in enumerate(zip(rows, truth_txns)):
            debit_ok = self._amounts_close(row.debit_raw, t.get("debit"))
            credit_ok = self._amounts_close(row.credit_raw, t.get("credit"))
            assert debit_ok, (
                f"Row {i}: debit mismatch — extracted '{row.debit_raw}', truth '{t.get('debit')}'"
            )
            assert credit_ok, (
                f"Row {i}: credit mismatch — extracted '{row.credit_raw}', truth '{t.get('credit')}'"
            )

    def test_hdfc_amounts_match_truth(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = hdfc_stmt
        self._check_accuracy(stmt.rows, truth["transactions"])

    def test_icici_amounts_match_truth(self, icici_stmt: tuple[RawStatement, dict]) -> None:
        stmt, truth = icici_stmt
        self._check_accuracy(stmt.rows, truth["transactions"])


# ─── source reference fields ──────────────────────────────────────────────────

class TestSourceFields:
    def test_raw_text_non_empty(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            assert row.raw_text.strip()

    def test_page_indices_non_negative(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        for row in stmt.rows:
            assert row.page >= 0
            assert row.row_idx >= 0

    def test_pages_cover_multiple(self, hdfc_stmt: tuple[RawStatement, dict]) -> None:
        stmt, _ = hdfc_stmt
        pages_seen = {r.page for r in stmt.rows}
        assert len(pages_seen) > 1, "All rows on one page — multi-page handling may be broken"


# ─── error handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_missing_file_raises_adapter_error(self, tmp_root: Path) -> None:
        with pytest.raises(AdapterError, match="File not found"):
            DigitalPDFAdapter().extract(tmp_root / "does_not_exist.pdf")

    def test_non_pdf_file_raises_adapter_error(self, tmp_root: Path) -> None:
        fake = tmp_root / "not_a_pdf.pdf"
        fake.write_bytes(b"this is not a PDF")
        with pytest.raises(AdapterError):
            DigitalPDFAdapter().extract(fake)
