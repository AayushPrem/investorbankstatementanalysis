from __future__ import annotations

import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class TransactionCategory(StrEnum):
    REVENUE = "REVENUE"
    VENDOR_PAYMENT = "VENDOR_PAYMENT"
    SALARY = "SALARY"
    LOAN_IN = "LOAN_IN"
    LOAN_OUT = "LOAN_OUT"
    TAX = "TAX"
    TRANSFER = "TRANSFER"
    FOUNDER_WITHDRAWAL = "FOUNDER_WITHDRAWAL"
    FEES = "FEES"
    OTHER = "OTHER"


class ValidationStatus(StrEnum):
    UNVALIDATED = "unvalidated"
    PASSED = "passed"
    FAILED = "failed"


class SourceReference(BaseModel):
    """Points back to the exact location in the original file — enables traceability."""
    file_path: str
    page: int
    row: int  # zero-indexed within the page
    raw_text: str


class CanonicalTransaction(BaseModel):
    # Core fields
    transaction_id: str
    date: datetime.date
    description: str
    debit: Decimal | None = None
    credit: Decimal | None = None
    balance: Decimal
    currency: str = "INR"
    source_reference: SourceReference

    # Enrichment slots — filled by later pipeline agents
    category: TransactionCategory | None = None
    counterparty: str | None = None
    is_recurring: bool | None = None
    is_related_party: bool | None = None
    related_party_match: str | None = None
    customer_id: str | None = None
    anomaly_flags: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_debit_credit_exclusive(self) -> CanonicalTransaction:
        if self.debit is not None and self.credit is not None:
            raise ValueError(
                "A transaction cannot have both debit and credit set — "
                f"transaction_id={self.transaction_id!r}"
            )
        if self.debit is None and self.credit is None:
            raise ValueError(
                "A transaction must have either debit or credit set — "
                f"transaction_id={self.transaction_id!r}"
            )
        return self


class StatementDocument(BaseModel):
    account_id: str
    statement_period_start: datetime.date
    statement_period_end: datetime.date
    source_format: str
    bank_name: str | None = None
    validation_status: ValidationStatus = ValidationStatus.UNVALIDATED
    transactions: list[CanonicalTransaction] = Field(default_factory=list)
