import datetime
import json
from decimal import Decimal

import pytest

from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
    ValidationStatus,
)

# --- helpers ---

def _ref(**overrides: object) -> SourceReference:
    return SourceReference(
        **{
            "file_path": "data/synthetic/hdfc/test_001.pdf",
            "page": 0,
            "row": 0,
            "raw_text": "01/04/24|NEFT/ACME PVT LTD|12345|01/04/24||1,50,000.00|4,50,000.00",
            **overrides,
        }
    )


def _txn(**overrides: object) -> CanonicalTransaction:
    return CanonicalTransaction(
        **{
            "transaction_id": "txn_001",
            "date": datetime.date(2024, 4, 1),
            "description": "NEFT/SBIN0001234/ACME PVT LTD/INV-447",
            "credit": Decimal("150000.00"),
            "balance": Decimal("450000.00"),
            "source_reference": _ref(),
            **overrides,
        }
    )


# --- SourceReference ---

class TestSourceReference:
    def test_all_fields_populate(self) -> None:
        ref = _ref(page=2, row=7)
        assert ref.file_path == "data/synthetic/hdfc/test_001.pdf"
        assert ref.page == 2
        assert ref.row == 7
        assert "NEFT" in ref.raw_text

    def test_row_zero_indexed(self) -> None:
        assert _ref(row=0).row == 0
        assert _ref(row=99).row == 99


# --- CanonicalTransaction core fields ---

class TestCanonicalTransactionFields:
    def test_credit_row_populates(self) -> None:
        txn = _txn()
        assert txn.transaction_id == "txn_001"
        assert txn.date == datetime.date(2024, 4, 1)
        assert txn.credit == Decimal("150000.00")
        assert txn.debit is None
        assert txn.balance == Decimal("450000.00")
        assert txn.currency == "INR"

    def test_debit_row_populates(self) -> None:
        txn = _txn(credit=None, debit=Decimal("12500.00"))
        assert txn.debit == Decimal("12500.00")
        assert txn.credit is None

    def test_default_currency_inr(self) -> None:
        assert _txn().currency == "INR"

    def test_custom_currency(self) -> None:
        txn = _txn(currency="USD")
        assert txn.currency == "USD"

    def test_description_preserved_exactly(self) -> None:
        raw = "NEFT/SBIN0001234/ACME PVT LTD/INV-447  extra  spaces"
        txn = _txn(description=raw)
        assert txn.description == raw


# --- Validation: debit/credit invariant ---

class TestDebitCreditValidation:
    def test_both_set_raises(self) -> None:
        with pytest.raises(ValueError, match="cannot have both"):
            _txn(debit=Decimal("1000.00"), credit=Decimal("2000.00"))

    def test_neither_set_raises(self) -> None:
        with pytest.raises(ValueError, match="must have either"):
            _txn(credit=None, debit=None)

    def test_only_debit_valid(self) -> None:
        txn = _txn(credit=None, debit=Decimal("500.00"))
        assert txn.debit == Decimal("500.00")

    def test_only_credit_valid(self) -> None:
        txn = _txn(credit=Decimal("500.00"))
        assert txn.credit == Decimal("500.00")


# --- Enrichment slots ---

class TestEnrichmentSlots:
    def test_all_default_to_none(self) -> None:
        txn = _txn()
        assert txn.category is None
        assert txn.counterparty is None
        assert txn.is_recurring is None
        assert txn.is_related_party is None
        assert txn.related_party_match is None
        assert txn.customer_id is None

    def test_anomaly_flags_default_empty(self) -> None:
        assert _txn().anomaly_flags == []

    def test_anomaly_flags_independent_across_instances(self) -> None:
        t1 = _txn(transaction_id="txn_001")
        t2 = _txn(transaction_id="txn_002")
        t1.anomaly_flags.append("structuring")
        assert t2.anomaly_flags == []

    def test_category_enum_accepted(self) -> None:
        for cat in TransactionCategory:
            txn = _txn(category=cat)
            assert txn.category == cat

    def test_customer_id_defaults_none(self) -> None:
        assert _txn().customer_id is None

    def test_enrichment_populated(self) -> None:
        txn = _txn(
            category=TransactionCategory.REVENUE,
            counterparty="Acme Pvt Ltd",
            is_recurring=True,
            is_related_party=False,
            customer_id="cust_abc123",
            anomaly_flags=["round_amount"],
        )
        assert txn.category == TransactionCategory.REVENUE
        assert txn.counterparty == "Acme Pvt Ltd"
        assert txn.is_recurring is True
        assert txn.is_related_party is False
        assert txn.customer_id == "cust_abc123"
        assert txn.anomaly_flags == ["round_amount"]


# --- Serialisation ---

class TestSerialization:
    def test_round_trip_basic(self) -> None:
        txn = _txn()
        loaded = CanonicalTransaction.model_validate_json(txn.model_dump_json())
        assert loaded.transaction_id == txn.transaction_id
        assert loaded.date == txn.date
        assert loaded.credit == txn.credit
        assert loaded.balance == txn.balance
        assert loaded.currency == txn.currency

    def test_round_trip_with_enrichment(self) -> None:
        txn = _txn(
            category=TransactionCategory.SALARY,
            counterparty="Staff Payroll",
            customer_id="cust_xyz",
            anomaly_flags=["structuring", "round_amount"],
            is_related_party=True,
            related_party_match="Founder Family Trust",
        )
        loaded = CanonicalTransaction.model_validate_json(txn.model_dump_json())
        assert loaded.category == TransactionCategory.SALARY
        assert loaded.customer_id == "cust_xyz"
        assert loaded.anomaly_flags == ["structuring", "round_amount"]
        assert loaded.is_related_party is True
        assert loaded.related_party_match == "Founder Family Trust"

    def test_decimal_precision_preserved(self) -> None:
        txn = _txn(credit=Decimal("150000.50"), balance=Decimal("450000.50"))
        loaded = CanonicalTransaction.model_validate_json(txn.model_dump_json())
        assert loaded.credit == Decimal("150000.50")
        assert loaded.balance == Decimal("450000.50")

    def test_date_preserved_as_date(self) -> None:
        txn = _txn(date=datetime.date(2024, 12, 31))
        loaded = CanonicalTransaction.model_validate_json(txn.model_dump_json())
        assert loaded.date == datetime.date(2024, 12, 31)

    def test_source_reference_preserved(self) -> None:
        ref = _ref(file_path="data/gold/stmt_001.pdf", page=3, row=14)
        txn = _txn(source_reference=ref)
        loaded = CanonicalTransaction.model_validate_json(txn.model_dump_json())
        assert loaded.source_reference.file_path == "data/gold/stmt_001.pdf"
        assert loaded.source_reference.page == 3
        assert loaded.source_reference.row == 14

    def test_json_is_valid_json(self) -> None:
        txn = _txn()
        parsed = json.loads(txn.model_dump_json())
        assert "transaction_id" in parsed
        assert "source_reference" in parsed


# --- StatementDocument ---

class TestStatementDocument:
    def test_default_fields(self) -> None:
        doc = StatementDocument(
            account_id="50100123456789",
            statement_period_start=datetime.date(2024, 4, 1),
            statement_period_end=datetime.date(2024, 6, 30),
            source_format="digital_pdf",
            bank_name="HDFC",
        )
        assert doc.account_id == "50100123456789"
        assert doc.bank_name == "HDFC"
        assert doc.validation_status == ValidationStatus.UNVALIDATED
        assert doc.transactions == []

    def test_bank_name_optional(self) -> None:
        doc = StatementDocument(
            account_id="50100123456789",
            statement_period_start=datetime.date(2024, 4, 1),
            statement_period_end=datetime.date(2024, 6, 30),
            source_format="digital_pdf",
        )
        assert doc.bank_name is None

    def test_with_transactions(self) -> None:
        txns = [
            _txn(transaction_id=f"txn_{i:03d}", balance=Decimal(str(i * 1000)))
            for i in range(1, 4)
        ]
        doc = StatementDocument(
            account_id="50100123456789",
            statement_period_start=datetime.date(2024, 4, 1),
            statement_period_end=datetime.date(2024, 6, 30),
            source_format="digital_pdf",
            transactions=txns,
        )
        assert len(doc.transactions) == 3
        assert doc.transactions[0].transaction_id == "txn_001"
        assert doc.transactions[2].transaction_id == "txn_003"

    def test_validation_status_enum(self) -> None:
        doc = StatementDocument(
            account_id="50100123456789",
            statement_period_start=datetime.date(2024, 4, 1),
            statement_period_end=datetime.date(2024, 6, 30),
            source_format="digital_pdf",
            validation_status=ValidationStatus.PASSED,
        )
        assert doc.validation_status == ValidationStatus.PASSED

    def test_round_trip_serialization(self) -> None:
        txn = _txn(category=TransactionCategory.REVENUE, customer_id="cust_abc")
        doc = StatementDocument(
            account_id="50100123456789",
            statement_period_start=datetime.date(2024, 4, 1),
            statement_period_end=datetime.date(2024, 6, 30),
            source_format="digital_pdf",
            bank_name="HDFC",
            transactions=[txn],
        )
        loaded = StatementDocument.model_validate_json(doc.model_dump_json())
        assert loaded.account_id == doc.account_id
        assert loaded.bank_name == "HDFC"
        assert len(loaded.transactions) == 1
        assert loaded.transactions[0].category == TransactionCategory.REVENUE
        assert loaded.transactions[0].customer_id == "cust_abc"
