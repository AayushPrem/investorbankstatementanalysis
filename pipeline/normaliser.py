"""Normaliser — converts RawStatement → StatementDocument.

Takes bank-native strings from the adapter layer (dates like "01/04/24",
amounts like "1,20,000.00") and produces CanonicalTransaction objects ready
for enrichment and analysis.

Supports:
  HDFC  — transaction dates DD/MM/YY,   header period dates DD/MM/YYYY
  ICICI — transaction dates DD-MM-YYYY, header period dates DD-MM-YYYY
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from adapters.base import RawRow, RawStatement
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    ValidationStatus,
)


class NormaliserError(Exception):
    """Raised when a required field cannot be parsed and the row must be skipped."""


class Normaliser:
    """Converts a RawStatement (from DigitalPDFAdapter) into a StatementDocument."""

    # Transaction-level date formats keyed by bank name
    _TXN_FORMATS: dict[str, list[str]] = {
        "HDFC":    ["%d/%m/%y"],
        "ICICI":   ["%d-%m-%Y"],
        "UNKNOWN": ["%d/%m/%y", "%d-%m-%Y", "%d/%m/%Y"],
    }

    # Period dates in the document header are always 4-digit year
    _PERIOD_FORMATS: list[str] = ["%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y", "%d-%m-%y"]

    # ── public API ─────────────────────────────────────────────────────────

    def normalise(self, raw: RawStatement) -> StatementDocument:
        """Normalise all rows in *raw* and return a StatementDocument.

        Rows that cannot be parsed are silently skipped — the Validator
        (Step 1.7) will flag extraction gaps by comparing transaction counts.
        """
        transactions: list[CanonicalTransaction] = []
        for row in raw.rows:
            try:
                transactions.append(self._normalise_row(row, raw))
            except NormaliserError:
                pass  # unparseable row — Validator will detect the gap

        period_start = self._parse_period_date(raw.period_start_raw)
        period_end = self._parse_period_date(raw.period_end_raw)

        # Derive period from transaction dates when the header regex missed
        if period_start is None and transactions:
            period_start = min(t.date for t in transactions)
        if period_end is None and transactions:
            period_end = max(t.date for t in transactions)

        if period_start is None or period_end is None:
            raise NormaliserError(
                f"Cannot determine statement period for {raw.source_path}"
            )

        return StatementDocument(
            account_id=raw.account_id or "unknown",
            statement_period_start=period_start,
            statement_period_end=period_end,
            source_format=f"digital_pdf_{raw.bank_name.lower()}",
            bank_name=raw.bank_name if raw.bank_name != "UNKNOWN" else None,
            validation_status=ValidationStatus.UNVALIDATED,
            transactions=transactions,
        )

    # ── per-row normalisation ──────────────────────────────────────────────

    def _normalise_row(self, row: RawRow, raw: RawStatement) -> CanonicalTransaction:
        txn_date = self._parse_txn_date(row.date_raw, raw.bank_name)
        debit = self._parse_amount(row.debit_raw)
        credit = self._parse_amount(row.credit_raw)
        balance = self._require_balance(row.balance_raw, row)

        if debit is None and credit is None:
            raise NormaliserError(
                f"Row has neither debit nor credit: page={row.page} row={row.row_idx}"
            )

        return CanonicalTransaction(
            transaction_id=self._make_txn_id(row, raw.source_path),
            date=txn_date,
            description=row.description_raw.strip(),
            debit=debit,
            credit=credit,
            balance=balance,
            currency="INR",
            source_reference=SourceReference(
                file_path=raw.source_path,
                page=row.page,
                row=row.row_idx,
                raw_text=row.raw_text,
            ),
        )

    # ── date parsing ───────────────────────────────────────────────────────

    def _parse_txn_date(self, raw: str, bank: str) -> date:
        for fmt in self._TXN_FORMATS.get(bank, self._TXN_FORMATS["UNKNOWN"]):
            try:
                return datetime.strptime(raw.strip(), fmt).date()
            except ValueError:
                continue
        raise NormaliserError(f"Cannot parse transaction date: {raw!r}")

    def _parse_period_date(self, raw: str | None) -> date | None:
        if not raw:
            return None
        for fmt in self._PERIOD_FORMATS:
            try:
                return datetime.strptime(raw.strip(), fmt).date()
            except ValueError:
                continue
        return None  # non-fatal — caller falls back to transaction dates

    # ── amount parsing ──────────────────────────────────────────────────────

    def _parse_amount(self, raw: str | None) -> Decimal | None:
        """Parse an Indian-format amount string; return None for blank/unparseable."""
        if not raw:
            return None
        cleaned = raw.replace(",", "").strip()
        if not cleaned:
            return None
        try:
            return Decimal(cleaned)
        except InvalidOperation:
            return None

    def _require_balance(self, raw: str | None, row: RawRow) -> Decimal:
        val = self._parse_amount(raw)
        if val is None:
            raise NormaliserError(
                f"Cannot parse balance {raw!r} at page={row.page} row={row.row_idx}"
            )
        return val

    # ── deterministic transaction ID ────────────────────────────────────────

    def _make_txn_id(self, row: RawRow, source_path: str) -> str:
        """SHA-1 of (file stem, page, row_idx, date, amount) — stable across reruns."""
        amount = row.debit_raw or row.credit_raw or ""
        key = f"{Path(source_path).stem}|{row.page}|{row.row_idx}|{row.date_raw}|{amount}"
        return "txn_" + hashlib.sha1(key.encode()).hexdigest()[:10]
