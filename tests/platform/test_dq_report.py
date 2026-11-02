"""DQ report (UPGRADE_GUIDE §8.2): additive to the validator, never gating."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal

from pipeline.validator import (
    DQ_SEVERITY_ERROR,
    Validator,
    build_dq_report,
    parse_header_amount,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    ValidationStatus,
)

EXPECTED_CHECKS = [
    "required_fields", "date_in_period", "duplicate_rows", "debit_credit_exclusive",
    "running_balance", "opening_balance_vs_header", "closing_balance_vs_header",
    "outlier_amounts", "currency_inr",
]


def _txn(i: int, *, debit: str | None = None, credit: str | None = None, balance: str,
         day: int | None = None, desc: str | None = None, currency: str = "INR") -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=f"txn_{i:03d}",
        date=dt.date(2024, 4, day or 1 + i),
        description=desc if desc is not None else f"UPI-PAYMENT-{i}",
        debit=Decimal(debit) if debit else None,
        credit=Decimal(credit) if credit else None,
        balance=Decimal(balance),
        currency=currency,
        source_reference=SourceReference(file_path="x.csv", page=1, row=i, raw_text=""),
    )


def _doc(txns: list[CanonicalTransaction]) -> StatementDocument:
    return StatementDocument(
        account_id="ACC1", statement_period_start=dt.date(2024, 4, 1),
        statement_period_end=dt.date(2024, 4, 30), source_format="csv", transactions=txns,
    )


def _clean_doc() -> StatementDocument:
    # opening 1000.00 → +500 → -200 → +300 → -100
    return _doc([
        _txn(0, credit="500.00", balance="1500.00"),
        _txn(1, debit="200.00", balance="1300.00"),
        _txn(2, credit="300.00", balance="1600.00"),
        _txn(3, debit="100.00", balance="1500.00"),
    ])


def test_every_check_has_the_documented_shape() -> None:
    report = build_dq_report(_clean_doc(), opening_balance=Decimal("1000.00"))
    assert [c.name for c in report.checks] == EXPECTED_CHECKS
    for c in report.to_dict()["checks"]:  # type: ignore[union-attr]
        assert set(c) >= {"name", "passed", "severity", "count", "sample_ids"}
    assert report.passed


def test_running_balance_break_reports_the_exact_row() -> None:
    doc = _clean_doc()
    # Corrupt row 2's balance by ₹0.50: under the validator's ₹1 gate, over DQ's ₹0.01.
    doc.transactions[2] = _txn(2, credit="300.00", balance="1600.50")
    report = build_dq_report(doc)
    rb = report.check("running_balance")
    # Row 2 breaks (1300 + 300 ≠ 1600.50) and so does row 3 (1600.50 − 100 ≠ 1500).
    assert rb.passed is False
    assert rb.severity == DQ_SEVERITY_ERROR
    assert rb.sample_ids == ["txn_002", "txn_003"]
    assert report.passed is False
    # The gate is unchanged: ₹0.50 is within its ₹1 tolerance.
    assert Validator().validate(doc).status == ValidationStatus.PASSED


def test_opening_balance_vs_header() -> None:
    assert build_dq_report(_clean_doc(), opening_balance=Decimal("1000.00")) \
        .check("opening_balance_vs_header").passed
    bad = build_dq_report(_clean_doc(), opening_balance=Decimal("999.00")).check("opening_balance_vs_header")
    assert not bad.passed and bad.sample_ids == ["txn_000"]
    skipped = build_dq_report(_clean_doc()).check("opening_balance_vs_header")
    assert skipped.skipped and skipped.passed


def test_closing_balance_skipped_without_header_and_checked_with_one() -> None:
    assert build_dq_report(_clean_doc()).check("closing_balance_vs_header").skipped
    assert not build_dq_report(_clean_doc(), closing_balance=Decimal("1.00")) \
        .check("closing_balance_vs_header").passed


def test_duplicates_dates_currency_required() -> None:
    doc = _doc([
        _txn(0, credit="500.00", balance="1500.00", day=5),
        _txn(1, credit="500.00", balance="1500.00", day=5, desc="UPI-PAYMENT-0"),  # dup of row 0
        _txn(2, debit="10.00", balance="1490.00", day=1, currency="USD"),
        _txn(3, debit="10.00", balance="1480.00", desc="   "),
    ])
    doc.transactions[2] = doc.transactions[2].model_copy(update={"date": dt.date(2024, 5, 3)})
    r = build_dq_report(doc)
    assert r.check("duplicate_rows").sample_ids == ["txn_001"]
    assert r.check("date_in_period").sample_ids == ["txn_002"]
    assert r.check("currency_inr").sample_ids == ["txn_002"]
    assert r.check("required_fields").sample_ids == ["txn_003"]


def test_outliers_flag_the_extreme_amount_only() -> None:
    txns = []
    bal = Decimal("100000")
    for i in range(20):
        amt = Decimal("1000") + i
        bal -= amt
        txns.append(_txn(i, debit=str(amt), balance=str(bal), day=1 + i))
    bal -= Decimal("5000000")
    txns.append(_txn(20, debit="5000000", balance=str(bal), day=25))
    out = build_dq_report(_doc(txns)).check("outlier_amounts")
    assert out.sample_ids == ["txn_020"]
    assert out.severity == "info"


def test_outliers_skipped_for_tiny_statements() -> None:
    assert build_dq_report(_clean_doc()).check("outlier_amounts").skipped


def test_dq_report_does_not_mutate_doc() -> None:
    doc = _clean_doc()
    before = doc.model_dump()
    build_dq_report(doc, opening_balance=Decimal("1"))
    assert doc.model_dump() == before


def test_parse_header_amount() -> None:
    assert parse_header_amount("1,20,000.50") == Decimal("120000.50")
    assert parse_header_amount("") is None
    assert parse_header_amount(None) is None
    assert parse_header_amount("n/a") is None
