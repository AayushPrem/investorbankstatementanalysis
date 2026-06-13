"""Tests for the Normaliser (Step 1.6).

Pipeline under test: generate_statement → DigitalPDFAdapter → Normaliser.
Each assertion compares the normalised StatementDocument against the ground-
truth JSON produced by the synthetic generator, giving us a closed-loop check.
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from adapters import DigitalPDFAdapter
from pipeline.normaliser import Normaliser
from schema.canonical import StatementDocument, ValidationStatus
from tools.synthetic_gen import generate_statement

# ─── module-scoped fixtures ───────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("normaliser_test")


def _pipeline(tmp_root: Path, bank: str, profile: str, seed: int = 42) -> tuple[StatementDocument, dict]:  # type: ignore[type-arg]
    """Run full adapter → normaliser pipeline, return (doc, truth)."""
    sid = f"norm_{bank}_{profile}"
    pdf, truth_path = generate_statement(
        bank=bank, profile=profile, output_dir=tmp_root,
        statement_id=sid, flags=[], seed=seed,
    )
    truth = json.loads(truth_path.read_text())
    raw = DigitalPDFAdapter().extract(pdf)
    doc = Normaliser().normalise(raw)
    return doc, truth


@pytest.fixture(scope="module")
def hdfc_result(tmp_root: Path) -> tuple[StatementDocument, dict]:  # type: ignore[type-arg]
    return _pipeline(tmp_root, "hdfc", "healthy_saas")


@pytest.fixture(scope="module")
def icici_result(tmp_root: Path) -> tuple[StatementDocument, dict]:  # type: ignore[type-arg]
    return _pipeline(tmp_root, "icici", "burning_startup")


# ─── StatementDocument metadata ───────────────────────────────────────────────

class TestDocumentMetadata:
    def test_hdfc_bank_name(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        assert doc.bank_name == "HDFC"

    def test_icici_bank_name(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = icici_result
        assert doc.bank_name == "ICICI"

    def test_hdfc_account_id(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = hdfc_result
        assert doc.account_id == truth["account_id"]

    def test_icici_account_id(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = icici_result
        assert doc.account_id == truth["account_id"]

    def test_hdfc_source_format(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        assert doc.source_format == "digital_pdf_hdfc"

    def test_icici_source_format(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = icici_result
        assert doc.source_format == "digital_pdf_icici"

    def test_validation_status_unvalidated(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        assert doc.validation_status == ValidationStatus.UNVALIDATED

    def test_hdfc_period_start(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = hdfc_result
        expected = date.fromisoformat(truth["period_start"])
        assert doc.statement_period_start == expected

    def test_hdfc_period_end(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = hdfc_result
        expected = date.fromisoformat(truth["period_end"])
        assert doc.statement_period_end == expected

    def test_icici_period_start(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = icici_result
        expected = date.fromisoformat(truth["period_start"])
        assert doc.statement_period_start == expected

    def test_icici_period_end(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = icici_result
        expected = date.fromisoformat(truth["period_end"])
        assert doc.statement_period_end == expected


# ─── transaction count ────────────────────────────────────────────────────────

class TestTransactionCount:
    def test_hdfc_count_matches_truth(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = hdfc_result
        assert len(doc.transactions) == len(truth["transactions"])

    def test_icici_count_matches_truth(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = icici_result
        assert len(doc.transactions) == len(truth["transactions"])

    def test_transactions_non_empty(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        assert len(doc.transactions) > 0


# ─── CanonicalTransaction field values ────────────────────────────────────────

class TestTransactionFields:
    def test_dates_are_date_objects(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert isinstance(txn.date, date)

    def test_hdfc_dates_match_truth(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = hdfc_result
        for txn, t in zip(doc.transactions, truth["transactions"]):
            assert txn.date == date.fromisoformat(t["date"]), (
                f"Date mismatch: got {txn.date}, expected {t['date']}"
            )

    def test_icici_dates_match_truth(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, truth = icici_result
        for txn, t in zip(doc.transactions, truth["transactions"]):
            assert txn.date == date.fromisoformat(t["date"])

    def test_descriptions_non_empty(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.description.strip()

    def test_currency_inr(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.currency == "INR"

    def test_debit_credit_exclusive(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        """CanonicalTransaction model_validator enforces this — just confirm it holds."""
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert not (txn.debit is not None and txn.credit is not None)

    def test_icici_debit_credit_exclusive(self, icici_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = icici_result
        for txn in doc.transactions:
            assert not (txn.debit is not None and txn.credit is not None)


# ─── amount accuracy ──────────────────────────────────────────────────────────

_TOL = Decimal("1")  # ₹1 tolerance for formatting differences


class TestAmountAccuracy:
    def _check(self, doc: StatementDocument, truth: dict) -> None:  # type: ignore[type-arg]
        for txn, t in zip(doc.transactions, truth["transactions"]):
            true_debit = Decimal(t["debit"]) if t.get("debit") else None
            true_credit = Decimal(t["credit"]) if t.get("credit") else None
            true_balance = Decimal(t["balance"])

            if true_debit is not None:
                assert txn.debit is not None
                assert abs(txn.debit - true_debit) <= _TOL, (
                    f"Debit mismatch: got {txn.debit}, expected {true_debit}"
                )
            if true_credit is not None:
                assert txn.credit is not None
                assert abs(txn.credit - true_credit) <= _TOL, (
                    f"Credit mismatch: got {txn.credit}, expected {true_credit}"
                )
            # Indian bank PDFs show overdraft balances as positive numbers (no
            # minus sign) — compare magnitudes only; sign reconstruction is the
            # Validator/Analyst's job from running-balance context.
            assert abs(abs(txn.balance) - abs(true_balance)) <= _TOL, (
                f"Balance magnitude mismatch: got {txn.balance}, expected {true_balance}"
            )

    def test_hdfc_amounts_correct(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        self._check(*hdfc_result)

    def test_icici_amounts_correct(self, icici_result: tuple[StatementDocument, dict]) -> None:
        self._check(*icici_result)

    def test_balances_are_positive(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.balance >= 0


# ─── transaction IDs ──────────────────────────────────────────────────────────

class TestTransactionIds:
    def test_ids_non_empty(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.transaction_id.startswith("txn_")

    def test_ids_unique_within_document(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        ids = [t.transaction_id for t in doc.transactions]
        assert len(ids) == len(set(ids)), "Duplicate transaction IDs found"

    def test_ids_deterministic(self, tmp_root: Path) -> None:
        """Same PDF → same IDs on every run."""
        pdf, _ = generate_statement(
            bank="hdfc", profile="services_firm", output_dir=tmp_root,
            statement_id="norm_det_test", flags=[], seed=77,
        )
        raw = DigitalPDFAdapter().extract(pdf)
        doc1 = Normaliser().normalise(raw)
        doc2 = Normaliser().normalise(raw)
        assert [t.transaction_id for t in doc1.transactions] == \
               [t.transaction_id for t in doc2.transactions]


# ─── source references ────────────────────────────────────────────────────────

class TestSourceReferences:
    def test_file_path_populated(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.source_reference.file_path.endswith(".pdf")

    def test_page_non_negative(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.source_reference.page >= 0

    def test_row_non_negative(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.source_reference.row >= 0

    def test_raw_text_non_empty(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.source_reference.raw_text.strip()

    def test_source_pages_span_multiple(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        pages = {t.source_reference.page for t in doc.transactions}
        assert len(pages) > 1, "All transactions on one page — multi-page handling broken"


# ─── enrichment slot defaults ─────────────────────────────────────────────────

class TestEnrichmentDefaults:
    def test_category_none(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.category is None

    def test_counterparty_none(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.counterparty is None

    def test_customer_id_none(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.customer_id is None

    def test_anomaly_flags_empty(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        for txn in doc.transactions:
            assert txn.anomaly_flags == []


# ─── round-trip serialisation ──────────────────────────────────────────────────

class TestSerialization:
    def test_document_round_trip(self, hdfc_result: tuple[StatementDocument, dict]) -> None:
        doc, _ = hdfc_result
        json_str = doc.model_dump_json()
        reloaded = StatementDocument.model_validate_json(json_str)
        assert reloaded.account_id == doc.account_id
        assert reloaded.bank_name == doc.bank_name
        assert len(reloaded.transactions) == len(doc.transactions)
        assert reloaded.transactions[0].date == doc.transactions[0].date
        assert reloaded.transactions[0].debit == doc.transactions[0].debit

    def test_all_profiles_normalise(self, tmp_root: Path) -> None:
        """All 5 profiles × 2 banks produce non-empty documents without errors."""
        profiles = ["healthy_saas", "burning_startup", "services_firm",
                    "ecommerce", "restaurant"]
        banks = ["hdfc", "icici"]
        adapter = DigitalPDFAdapter()
        normaliser = Normaliser()
        for bank in banks:
            for profile in profiles:
                sid = f"norm_all_{bank}_{profile}"
                pdf, _ = generate_statement(
                    bank=bank, profile=profile, output_dir=tmp_root,
                    statement_id=sid, flags=[], seed=55,
                )
                raw = adapter.extract(pdf)
                doc = normaliser.normalise(raw)
                assert len(doc.transactions) > 0, (
                    f"{bank}/{profile}: Normaliser returned no transactions"
                )


# ─── error handling ───────────────────────────────────────────────────────────

class TestErrorHandling:
    def test_empty_rows_produces_empty_doc(self, tmp_root: Path) -> None:
        """A RawStatement with no rows produces a StatementDocument with no transactions."""
        from adapters.base import RawStatement
        raw = RawStatement(
            source_path=str(tmp_root / "fake.pdf"),
            bank_name="HDFC",
            account_id="12345",
            period_start_raw="01/04/2024",
            period_end_raw="30/06/2024",
            opening_balance_raw="1000000",
            rows=[],
        )
        doc = Normaliser().normalise(raw)
        assert doc.transactions == []

    def test_missing_period_falls_back_to_txn_dates(self, tmp_root: Path) -> None:
        """If header period is missing, derive period from transaction dates."""
        pdf, _ = generate_statement(
            bank="hdfc", profile="restaurant", output_dir=tmp_root,
            statement_id="norm_fallback_test", flags=[], seed=10,
        )
        raw = DigitalPDFAdapter().extract(pdf)
        # Wipe period fields to test fallback
        raw.period_start_raw = None
        raw.period_end_raw = None
        doc = Normaliser().normalise(raw)
        assert doc.statement_period_start is not None
        assert doc.statement_period_end is not None
        assert doc.statement_period_start <= doc.statement_period_end
