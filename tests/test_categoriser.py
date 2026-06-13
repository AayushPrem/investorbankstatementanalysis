"""Tests for the Categoriser (Step 1.8).

Deterministic-first design: the rule layer is tested exhaustively without
any API calls. LLM behaviour is tested with a mock.

Pipeline under test:
    generate_statement → DigitalPDFAdapter → Normaliser → Categoriser (rules only)
"""
from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from adapters import DigitalPDFAdapter
from analysis.categoriser import (
    Categoriser,
    CategorisationStats,
    _build_user_message,
    _parse_llm_response,
    _rule_classify,
)
from pipeline.normaliser import Normaliser
from schema.canonical import CanonicalTransaction, SourceReference, TransactionCategory
from tools.synthetic_gen import generate_statement

# ─── helpers ──────────────────────────────────────────────────────────────────

def _ref() -> SourceReference:
    return SourceReference(file_path="test.pdf", page=0, row=0, raw_text="")


def _txn(description: str, *, credit: str | None = None, debit: str | None = None) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id="test_001",
        date=date(2024, 4, 1),
        description=description,
        credit=Decimal(credit) if credit else None,
        debit=Decimal(debit) if debit else None,
        balance=Decimal("1000000"),
        source_reference=_ref(),
    )


def _pipeline(tmp_root: Path, bank: str, profile: str, seed: int = 42) -> tuple[Any, Any]:
    """Generate → adapt → normalise → categorise (rules only). Return (doc, truth)."""
    sid = f"cat_{bank}_{profile}"
    pdf, truth_path = generate_statement(
        bank=bank, profile=profile, output_dir=tmp_root,
        statement_id=sid, flags=[], seed=seed,
    )
    truth = json.loads(truth_path.read_text())
    raw = DigitalPDFAdapter().extract(pdf)
    doc = Normaliser().normalise(raw)
    Categoriser(llm_enabled=False).categorise(doc)
    return doc, truth


# ─── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("categoriser_test")


@pytest.fixture(scope="module")
def hdfc_result(tmp_root: Path) -> tuple[Any, Any]:
    return _pipeline(tmp_root, "hdfc", "healthy_saas")


@pytest.fixture(scope="module")
def icici_result(tmp_root: Path) -> tuple[Any, Any]:
    return _pipeline(tmp_root, "icici", "services_firm")


# ─── rule layer: SALARY ───────────────────────────────────────────────────────

class TestSalaryRule:
    def test_hdfc_ecs_salary(self) -> None:
        assert _rule_classify(_txn("ECS/SALARY/ARUN KUMAR/49F1271B", debit="80000")) \
               == TransactionCategory.SALARY

    def test_icici_ecs_dr_salary(self) -> None:
        assert _rule_classify(_txn("ECS DR-SALARY-RAVI SHANKAR-A62622F8", debit="120000")) \
               == TransactionCategory.SALARY

    def test_salary_credit_not_matched(self) -> None:
        # A credit with SALARY in description should not be SALARY
        result = _rule_classify(_txn("NEFT CR-SALARY REFUND", credit="10000"))
        assert result != TransactionCategory.SALARY

    def test_payroll_keyword(self) -> None:
        assert _rule_classify(_txn("ECS PAYROLL/STAFF", debit="50000")) \
               == TransactionCategory.SALARY


# ─── rule layer: TAX ──────────────────────────────────────────────────────────

class TestTaxRule:
    def test_gst_payment(self) -> None:
        assert _rule_classify(_txn("NEFT/CBSB60328/GST PAYMENT/GSTIN54996866706", debit="231120")) \
               == TransactionCategory.TAX

    def test_tds_payment(self) -> None:
        assert _rule_classify(_txn("NEFT/CBSB/TDS PAYMENT/194C", debit="15000")) \
               == TransactionCategory.TAX

    def test_gstin_keyword(self) -> None:
        assert _rule_classify(_txn("NEFT DR-GSTIN12345678-TAX", debit="10000")) \
               == TransactionCategory.TAX

    def test_income_tax(self) -> None:
        assert _rule_classify(_txn("ADVANCE INCOME TAX PAYMENT", debit="100000")) \
               == TransactionCategory.TAX


# ─── rule layer: FOUNDER_WITHDRAWAL ──────────────────────────────────────────

class TestFounderWithdrawalRule:
    def test_personal_suffix(self) -> None:
        assert _rule_classify(_txn("NEFT DR-UTIB1511572-PRIYA GUPTA PERSONAL", debit="50000")) \
               == TransactionCategory.FOUNDER_WITHDRAWAL

    def test_founder_withdrawal_keyword(self) -> None:
        assert _rule_classify(_txn("IMPS/FOUNDER WITHDRAWAL/RAJESH SHARMA", debit="100000")) \
               == TransactionCategory.FOUNDER_WITHDRAWAL

    def test_director_withdrawal(self) -> None:
        assert _rule_classify(_txn("DIRECTOR WITHDRAWAL", debit="75000")) \
               == TransactionCategory.FOUNDER_WITHDRAWAL


# ─── rule layer: FEES ─────────────────────────────────────────────────────────

class TestFeesRule:
    def test_bank_charges(self) -> None:
        assert _rule_classify(_txn("BANK CHARGES/NEFT", debit="15")) \
               == TransactionCategory.FEES

    def test_fee_deduction(self) -> None:
        assert _rule_classify(_txn("NEFT DR-SBIN6990876-RAZORPAY FEE DEDUCTION", debit="500")) \
               == TransactionCategory.FEES

    def test_processing_fee(self) -> None:
        assert _rule_classify(_txn("PROCESSING FEE/LOAN", debit="1000")) \
               == TransactionCategory.FEES

    def test_neft_charges(self) -> None:
        assert _rule_classify(_txn("NEFT CHARGES", debit="5")) \
               == TransactionCategory.FEES


# ─── rule layer: REVENUE ──────────────────────────────────────────────────────

class TestRevenueRule:
    def test_invoice_credit_hdfc(self) -> None:
        assert _rule_classify(_txn("NEFT/PUNB1579247/GROWTHHUB DIGITAL/INV00100", credit="48000")) \
               == TransactionCategory.REVENUE

    def test_invoice_credit_icici(self) -> None:
        assert _rule_classify(_txn("NEFT CR-SBIN1685761-VISIONTECH INC-INV0020", credit="78000")) \
               == TransactionCategory.REVENUE

    def test_cash_deposit(self) -> None:
        assert _rule_classify(_txn("CASH DEP-MAIN BRANCH", credit="200000")) \
               == TransactionCategory.REVENUE

    def test_generic_credit_fallback(self) -> None:
        assert _rule_classify(_txn("NEFT CR-SOME CORP-UNKNOWN", credit="10000")) \
               == TransactionCategory.REVENUE


# ─── rule layer: VENDOR_PAYMENT ───────────────────────────────────────────────

class TestVendorPaymentRule:
    def test_neft_debit_vendor(self) -> None:
        assert _rule_classify(_txn("NEFT/ICIC8304073/AMAZON WEB SERVICES/BBE0C282", debit="180000")) \
               == TransactionCategory.VENDOR_PAYMENT

    def test_imps_debit_vendor(self) -> None:
        assert _rule_classify(_txn("IMPS/VENDOR NAME/INV-999", debit="5000")) \
               == TransactionCategory.VENDOR_PAYMENT

    def test_generic_debit_fallback(self) -> None:
        assert _rule_classify(_txn("UNKNOWN PAYMENT/XYZ CORP", debit="25000")) \
               == TransactionCategory.VENDOR_PAYMENT


# ─── rule layer: LOAN ────────────────────────────────────────────────────────

class TestLoanRule:
    def test_loan_in(self) -> None:
        assert _rule_classify(_txn("TERM LOAN DISBURSEMENT", credit="5000000")) \
               == TransactionCategory.LOAN_IN

    def test_loan_out_emi(self) -> None:
        assert _rule_classify(_txn("EMI/HDFC BANK/TERM LOAN", debit="150000")) \
               == TransactionCategory.LOAN_OUT

    def test_loan_repayment(self) -> None:
        assert _rule_classify(_txn("LOAN REPAY/ICICI BANK", debit="250000")) \
               == TransactionCategory.LOAN_OUT


# ─── all transactions categorised ────────────────────────────────────────────

class TestAllCategorised:
    def test_no_none_categories_hdfc(self, hdfc_result: tuple[Any, Any]) -> None:
        doc, _ = hdfc_result
        uncategorised = [t for t in doc.transactions if t.category is None]
        assert not uncategorised, (
            f"{len(uncategorised)} transactions left uncategorised: "
            + ", ".join(t.description[:40] for t in uncategorised[:3])
        )

    def test_no_none_categories_icici(self, icici_result: tuple[Any, Any]) -> None:
        doc, _ = icici_result
        assert all(t.category is not None for t in doc.transactions)

    def test_returns_doc_and_stats(self, hdfc_result: tuple[Any, Any]) -> None:
        """categorise() returns (StatementDocument, CategorisationStats)."""
        from schema.canonical import StatementDocument
        doc, _ = hdfc_result
        assert isinstance(doc, StatementDocument)

    def test_stats_totals_match_transaction_count(self, tmp_root: Path) -> None:
        pdf, _ = generate_statement(
            bank="hdfc", profile="services_firm", output_dir=tmp_root,
            statement_id="cat_stats_test", flags=[], seed=5,
        )
        doc = Normaliser().normalise(DigitalPDFAdapter().extract(pdf))
        _, stats = Categoriser(llm_enabled=False).categorise(doc)
        assert stats.total == len(doc.transactions)
        assert stats.rule_matched + stats.llm_matched + stats.fallback_other == stats.total

    def test_rule_match_rate_is_high(self, tmp_root: Path) -> None:
        """Rule layer should match ≥90% of synthetic statements without LLM."""
        pdf, _ = generate_statement(
            bank="hdfc", profile="healthy_saas", output_dir=tmp_root,
            statement_id="cat_rate_test", flags=[], seed=9,
        )
        doc = Normaliser().normalise(DigitalPDFAdapter().extract(pdf))
        _, stats = Categoriser(llm_enabled=False).categorise(doc)
        assert stats.rule_match_rate >= 0.90, (
            f"Rule match rate {stats.rule_match_rate:.1%} < 90%"
        )


# ─── accuracy against ground truth ───────────────────────────────────────────

_TOL = Decimal("1")
SPRINT1_CAT_TARGET = 0.82


class TestAccuracy:
    def _match(
        self, doc_txns: list[CanonicalTransaction], truth_txns: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Match extracted txns to truth by (date, amount). Return (correct, matched)."""
        from decimal import Decimal as D
        correct = matched = 0
        used: set[int] = set()

        for t in truth_txns:
            true_amt = D(t["credit"]) if t.get("credit") else D(t["debit"] or "0")
            true_date = t["date"]
            for i, txn in enumerate(doc_txns):
                if i in used:
                    continue
                txn_amt = (txn.credit or txn.debit) or D("0")
                txn_date = str(txn.date)
                if txn_date == true_date and abs(txn_amt - true_amt) <= _TOL:
                    used.add(i)
                    matched += 1
                    if txn.category is not None and txn.category.value == t.get("category"):
                        correct += 1
                    break

        return correct, matched

    def test_hdfc_categorisation_accuracy(self, hdfc_result: tuple[Any, Any]) -> None:
        doc, truth = hdfc_result
        correct, matched = self._match(doc.transactions, truth["transactions"])
        accuracy = correct / matched if matched else 0.0
        assert accuracy >= SPRINT1_CAT_TARGET, (
            f"HDFC categorisation accuracy {accuracy:.1%} < {SPRINT1_CAT_TARGET:.0%} target "
            f"({correct}/{matched} correct)"
        )

    def test_icici_categorisation_accuracy(self, icici_result: tuple[Any, Any]) -> None:
        doc, truth = icici_result
        correct, matched = self._match(doc.transactions, truth["transactions"])
        accuracy = correct / matched if matched else 0.0
        assert accuracy >= SPRINT1_CAT_TARGET, (
            f"ICICI categorisation accuracy {accuracy:.1%} < {SPRINT1_CAT_TARGET:.0%} target "
            f"({correct}/{matched} correct)"
        )


# ─── LLM plumbing (mocked) ───────────────────────────────────────────────────

class TestLLMPlumbing:
    def test_parse_llm_response_valid(self) -> None:
        cats = _parse_llm_response('["REVENUE", "SALARY", "TAX"]', 3)
        assert cats == [
            TransactionCategory.REVENUE,
            TransactionCategory.SALARY,
            TransactionCategory.TAX,
        ]

    def test_parse_llm_response_invalid_json(self) -> None:
        cats = _parse_llm_response("not json at all", 2)
        assert cats == [TransactionCategory.OTHER, TransactionCategory.OTHER]

    def test_parse_llm_response_wrong_length(self) -> None:
        cats = _parse_llm_response('["REVENUE"]', 3)
        assert cats == [TransactionCategory.OTHER] * 3

    def test_parse_llm_response_unknown_category(self) -> None:
        cats = _parse_llm_response('["REVENUE", "UNICORN"]', 2)
        assert cats[0] == TransactionCategory.REVENUE
        assert cats[1] == TransactionCategory.OTHER

    def test_build_user_message_format(self) -> None:
        txns = [
            _txn("SALARY PAYMENT", debit="80000"),
            _txn("NEFT CR-CUSTOMER", credit="50000"),
        ]
        msg = _build_user_message(txns)
        assert "[DEBIT] SALARY PAYMENT" in msg
        assert "[CREDIT] NEFT CR-CUSTOMER" in msg
        assert "1." in msg
        assert "2." in msg

    def test_llm_batch_called_when_rule_returns_none(self) -> None:
        """_llm_categorise_batch is invoked for transactions the rule layer doesn't resolve.

        The catch-all rules in _RULES normally match everything, so we patch
        _rule_classify at the module level to return None, simulating a future
        extension where the rule layer is intentionally non-exhaustive.
        """
        txn = CanonicalTransaction(
            transaction_id="unmatched_001",
            date=date(2024, 4, 1),
            description="SOME COMPLETELY AMBIGUOUS TXFR XYZ",
            debit=Decimal("99999"),
            balance=Decimal("900000"),
            source_reference=_ref(),
        )
        from schema.canonical import StatementDocument, ValidationStatus
        doc = StatementDocument(
            account_id="123", statement_period_start=date(2024, 4, 1),
            statement_period_end=date(2024, 6, 30), source_format="digital_pdf_hdfc",
            validation_status=ValidationStatus.UNVALIDATED, transactions=[txn],
        )

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            cat = Categoriser(llm_enabled=True)
            # Force the rule layer to return None so the LLM layer is exercised
            with patch("analysis.categoriser._rule_classify", return_value=None):
                with patch.object(
                    cat, "_llm_categorise_batch",
                    return_value=[TransactionCategory.VENDOR_PAYMENT]
                ) as mock_batch:
                    cat.categorise(doc)
            mock_batch.assert_called_once()

        assert txn.category == TransactionCategory.VENDOR_PAYMENT

    def test_llm_api_failure_falls_back_to_other(self) -> None:
        """If _llm_categorise_batch raises, transactions get OTHER — not an unhandled exception."""
        txn = CanonicalTransaction(
            transaction_id="unmatched_002",
            date=date(2024, 4, 1),
            description="AMBIGUOUS XYZ 999",
            debit=Decimal("1000"),
            balance=Decimal("999000"),
            source_reference=_ref(),
        )
        from schema.canonical import StatementDocument, ValidationStatus
        doc = StatementDocument(
            account_id="123", statement_period_start=date(2024, 4, 1),
            statement_period_end=date(2024, 6, 30), source_format="digital_pdf_hdfc",
            validation_status=ValidationStatus.UNVALIDATED, transactions=[txn],
        )

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            cat = Categoriser(llm_enabled=True)
            # Force rule layer to return None, then make the LLM batch blow up
            with patch("analysis.categoriser._rule_classify", return_value=None):
                with patch.object(cat, "_llm_categorise_batch", side_effect=RuntimeError("API timeout")):
                    cat.categorise(doc)

        assert txn.category == TransactionCategory.OTHER


# ─── CategorisationStats ──────────────────────────────────────────────────────

class TestStats:
    def test_rule_match_rate_zero_on_empty(self) -> None:
        stats = CategorisationStats(total=0)
        assert stats.rule_match_rate == 0.0

    def test_rule_match_rate_calculation(self) -> None:
        stats = CategorisationStats(total=10, rule_matched=8)
        assert stats.rule_match_rate == pytest.approx(0.8)
