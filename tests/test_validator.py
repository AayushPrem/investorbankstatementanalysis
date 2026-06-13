"""Tests for the Validator (Step 1.7).

Pipeline under test: generate_statement → DigitalPDFAdapter → Normaliser → Validator.

Key facts baked into these tests:
  - healthy_saas and services_firm never go into overdraft → ValidationStatus.PASSED
  - Overdraft rows (balance shows positive in PDF, truth is negative) cause
    BALANCE_BREAK in burning_startup / ecommerce — that is correct Validator
    behaviour; the issue will be surfaced and dealt with in the Analyser.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from pathlib import Path

import pytest

from adapters import DigitalPDFAdapter
from pipeline.normaliser import Normaliser
from pipeline.validator import (
    ValidationReport,
    ValidationSeverity,
    Validator,
)
from schema.canonical import StatementDocument, ValidationStatus
from tools.synthetic_gen import generate_statement

# ─── helpers / fixtures ───────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("validator_test")


def _run(tmp_root: Path, bank: str, profile: str, seed: int = 42) -> tuple[StatementDocument, ValidationReport]:
    pdf, _ = generate_statement(
        bank=bank, profile=profile, output_dir=tmp_root,
        statement_id=f"val_{bank}_{profile}", flags=[], seed=seed,
    )
    raw = DigitalPDFAdapter().extract(pdf)
    doc = Normaliser().normalise(raw)
    report = Validator().validate(doc)
    return doc, report


@pytest.fixture(scope="module")
def clean_hdfc(tmp_root: Path) -> tuple[StatementDocument, ValidationReport]:
    """healthy_saas HDFC — never goes into overdraft, should pass cleanly."""
    return _run(tmp_root, "hdfc", "healthy_saas")


@pytest.fixture(scope="module")
def clean_icici(tmp_root: Path) -> tuple[StatementDocument, ValidationReport]:
    """services_firm ICICI — no overdraft, should pass cleanly."""
    return _run(tmp_root, "icici", "services_firm")


# ─── ValidationStatus on clean statements ────────────────────────────────────

class TestCleanStatementPasses:
    def test_hdfc_healthy_saas_passes(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        assert doc.validation_status == ValidationStatus.PASSED

    def test_icici_services_firm_passes(self, clean_icici: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_icici
        assert doc.validation_status == ValidationStatus.PASSED

    def test_hdfc_no_balance_breaks(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        assert report.balance_break_count == 0

    def test_icici_no_balance_breaks(self, clean_icici: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_icici
        assert report.balance_break_count == 0

    def test_hdfc_no_errors(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        assert report.error_count == 0

    def test_hdfc_no_duplicate_ids(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        assert not report.issues_by_code("DUPLICATE_ID")

    def test_hdfc_no_out_of_order(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        assert not report.issues_by_code("OUT_OF_ORDER")

    def test_hdfc_no_dates_outside_period(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        assert not report.issues_by_code("DATE_OUTSIDE_PERIOD")

    def test_status_written_to_doc(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        """Validator mutates doc.validation_status in place."""
        doc, report = clean_hdfc
        assert doc.validation_status == report.status


# ─── ValidationReport structure ───────────────────────────────────────────────

class TestReportStructure:
    def test_transaction_count_matches_doc(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, report = clean_hdfc
        assert report.transaction_count == len(doc.transactions)

    def test_error_count_property(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        errors = [i for i in report.issues if i.severity == ValidationSeverity.ERROR]
        assert report.error_count == len(errors)

    def test_warning_count_property(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        warnings = [i for i in report.issues if i.severity == ValidationSeverity.WARNING]
        assert report.warning_count == len(warnings)

    def test_issues_by_code_filters_correctly(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        _, report = clean_hdfc
        for issue in report.issues:
            assert issue in report.issues_by_code(issue.code)


# ─── BALANCE_BREAK detection ──────────────────────────────────────────────────

class TestBalanceBreak:
    def _corrupt_balance(self, doc: StatementDocument) -> StatementDocument:
        """Return a deep-copy of *doc* with the 5th transaction's balance corrupted."""
        corrupted = deepcopy(doc)
        target = corrupted.transactions[4]
        # Add ₹10,000 to the balance — enough to exceed the ₹1 tolerance
        corrupted.transactions[4] = target.model_copy(
            update={"balance": target.balance + Decimal("10000")}
        )
        return corrupted

    def test_corrupted_balance_fails(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._corrupt_balance(doc)
        report = Validator().validate(bad_doc)
        assert report.status == ValidationStatus.FAILED

    def test_corrupted_balance_has_break_issue(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._corrupt_balance(doc)
        report = Validator().validate(bad_doc)
        breaks = report.issues_by_code("BALANCE_BREAK")
        assert len(breaks) >= 1

    def test_balance_break_severity_is_error(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._corrupt_balance(doc)
        report = Validator().validate(bad_doc)
        breaks = report.issues_by_code("BALANCE_BREAK")
        assert all(i.severity == ValidationSeverity.ERROR for i in breaks)

    def test_balance_break_has_transaction_id(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._corrupt_balance(doc)
        report = Validator().validate(bad_doc)
        breaks = report.issues_by_code("BALANCE_BREAK")
        assert all(i.transaction_id is not None for i in breaks)

    def test_balance_break_count_field(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._corrupt_balance(doc)
        report = Validator().validate(bad_doc)
        assert report.balance_break_count == len(report.issues_by_code("BALANCE_BREAK"))


# ─── DUPLICATE_ID detection ───────────────────────────────────────────────────

class TestDuplicateId:
    def _inject_duplicate(self, doc: StatementDocument) -> StatementDocument:
        duped = deepcopy(doc)
        first_id = duped.transactions[0].transaction_id
        duped.transactions[1] = duped.transactions[1].model_copy(
            update={"transaction_id": first_id}
        )
        return duped

    def test_duplicate_id_fails(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._inject_duplicate(doc)
        report = Validator().validate(bad_doc)
        assert report.status == ValidationStatus.FAILED

    def test_duplicate_id_issue_present(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._inject_duplicate(doc)
        report = Validator().validate(bad_doc)
        assert report.issues_by_code("DUPLICATE_ID")

    def test_duplicate_id_severity_is_error(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad_doc = self._inject_duplicate(doc)
        report = Validator().validate(bad_doc)
        dupes = report.issues_by_code("DUPLICATE_ID")
        assert all(i.severity == ValidationSeverity.ERROR for i in dupes)


# ─── SPARSE_TRANSACTIONS detection ───────────────────────────────────────────

class TestSparseTransactions:
    def _sparse_doc(self, doc: StatementDocument) -> StatementDocument:
        """Return a copy with all but 2 transactions removed."""
        sparse = deepcopy(doc)
        sparse.transactions = sparse.transactions[:2]
        return sparse

    def test_sparse_statement_warns(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        sparse = self._sparse_doc(doc)
        report = Validator().validate(sparse)
        assert report.issues_by_code("SPARSE_TRANSACTIONS")

    def test_sparse_severity_is_warning(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        sparse = self._sparse_doc(doc)
        report = Validator().validate(sparse)
        issues = report.issues_by_code("SPARSE_TRANSACTIONS")
        assert all(i.severity == ValidationSeverity.WARNING for i in issues)

    def test_sparse_does_not_fail_on_its_own(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        """SPARSE is a WARNING only — statement still PASSES (no balance breaks, no dup IDs)."""
        doc, _ = clean_hdfc
        # Recompute from scratch so balance continuity holds with only 2 txns
        doc_2txn = deepcopy(doc)
        doc_2txn.transactions = doc_2txn.transactions[:2]
        report = Validator().validate(doc_2txn)
        # SPARSE warning may be present, but status depends on balance consistency
        assert report.issues_by_code("SPARSE_TRANSACTIONS")
        assert report.error_count == 0


# ─── OUT_OF_ORDER detection ───────────────────────────────────────────────────

class TestOutOfOrder:
    def test_out_of_order_detected(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        swapped = deepcopy(doc)
        # Swap the dates of transactions 2 and 10 to create a date regression
        d2 = swapped.transactions[2].date
        d10 = swapped.transactions[10].date
        swapped.transactions[2] = swapped.transactions[2].model_copy(update={"date": d10})
        swapped.transactions[10] = swapped.transactions[10].model_copy(update={"date": d2})
        report = Validator().validate(swapped)
        assert report.issues_by_code("OUT_OF_ORDER")

    def test_out_of_order_is_warning(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        swapped = deepcopy(doc)
        d2, d10 = swapped.transactions[2].date, swapped.transactions[10].date
        swapped.transactions[2] = swapped.transactions[2].model_copy(update={"date": d10})
        swapped.transactions[10] = swapped.transactions[10].model_copy(update={"date": d2})
        report = Validator().validate(swapped)
        oos = report.issues_by_code("OUT_OF_ORDER")
        assert all(i.severity == ValidationSeverity.WARNING for i in oos)


# ─── DATE_OUTSIDE_PERIOD detection ────────────────────────────────────────────

class TestDateOutsidePeriod:
    def test_far_future_date_flagged(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        from datetime import timedelta
        doc, _ = clean_hdfc
        bad = deepcopy(doc)
        future = doc.statement_period_end + timedelta(days=30)
        bad.transactions[0] = bad.transactions[0].model_copy(update={"date": future})
        report = Validator().validate(bad)
        assert report.issues_by_code("DATE_OUTSIDE_PERIOD")

    def test_within_buffer_not_flagged(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        from datetime import timedelta
        doc, _ = clean_hdfc
        fine = deepcopy(doc)
        # 1 day outside — within the 2-day buffer, so should not be flagged
        edge = doc.statement_period_end + timedelta(days=1)
        fine.transactions[0] = fine.transactions[0].model_copy(update={"date": edge})
        report = Validator().validate(fine)
        assert not report.issues_by_code("DATE_OUTSIDE_PERIOD")


# ─── EMPTY_DESCRIPTION detection ─────────────────────────────────────────────

class TestEmptyDescription:
    def test_empty_description_flagged(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad = deepcopy(doc)
        bad.transactions[0] = bad.transactions[0].model_copy(update={"description": "   "})
        report = Validator().validate(bad)
        assert report.issues_by_code("EMPTY_DESCRIPTION")

    def test_empty_description_is_warning(self, clean_hdfc: tuple[StatementDocument, ValidationReport]) -> None:
        doc, _ = clean_hdfc
        bad = deepcopy(doc)
        bad.transactions[0] = bad.transactions[0].model_copy(update={"description": ""})
        report = Validator().validate(bad)
        issues = report.issues_by_code("EMPTY_DESCRIPTION")
        assert all(i.severity == ValidationSeverity.WARNING for i in issues)


# ─── edge cases ───────────────────────────────────────────────────────────────

class TestEdgeCases:
    def test_empty_document_passes(self) -> None:
        """A document with no transactions has nothing to fail — status PASSED."""
        from adapters.base import RawStatement
        raw = RawStatement(
            source_path="fake.pdf", bank_name="HDFC", account_id="123",
            period_start_raw="01/04/2024", period_end_raw="30/06/2024",
            opening_balance_raw="1000000", rows=[],
        )
        doc = Normaliser().normalise(raw)
        report = Validator().validate(doc)
        assert report.status == ValidationStatus.PASSED
        assert report.transaction_count == 0
        assert report.balance_break_count == 0

    def test_single_transaction_passes(self, tmp_root: Path) -> None:
        """One transaction has no consecutive pair — balance continuity trivially holds."""
        from adapters.base import RawRow, RawStatement
        row = RawRow(
            page=0, row_idx=1, raw_text="01/04/24 | TEST | | 01/04/24 | 1000.00 | | 999000.00",
            date_raw="01/04/24", description_raw="TEST TXN",
            debit_raw="1000.00", credit_raw=None, balance_raw="999000.00",
        )
        raw = RawStatement(
            source_path=str(tmp_root / "single.pdf"), bank_name="HDFC",
            account_id="123", period_start_raw="01/04/2024",
            period_end_raw="30/06/2024", opening_balance_raw="1000000", rows=[row],
        )
        doc = Normaliser().normalise(raw)
        report = Validator().validate(doc)
        assert report.balance_break_count == 0
        assert report.error_count == 0

    def test_all_non_overdraft_profiles_pass(self, tmp_root: Path) -> None:
        """Profiles that never dip negative should all produce ValidationStatus.PASSED."""
        safe_combos = [
            ("hdfc", "healthy_saas"),
            ("hdfc", "services_firm"),
            ("hdfc", "restaurant"),
            ("icici", "services_firm"),
            ("icici", "healthy_saas"),
        ]
        adapter, normaliser, validator = DigitalPDFAdapter(), Normaliser(), Validator()
        for bank, profile in safe_combos:
            pdf, _ = generate_statement(
                bank=bank, profile=profile, output_dir=tmp_root,
                statement_id=f"val_safe_{bank}_{profile}", flags=[], seed=7,
            )
            doc = normaliser.normalise(adapter.extract(pdf))
            report = validator.validate(doc)
            assert report.status == ValidationStatus.PASSED, (
                f"{bank}/{profile} failed with "
                f"{report.balance_break_count} balance breaks: "
                f"{[i.message for i in report.issues_by_code('BALANCE_BREAK')][:2]}"
            )
