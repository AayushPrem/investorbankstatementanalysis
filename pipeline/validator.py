"""Validator — audits a normalised StatementDocument for data-quality issues.

Runs after the Normaliser, before enrichment agents. Sets
StatementDocument.validation_status to PASSED or FAILED and returns a
ValidationReport with a structured list of issues.

Checks (in order):
  1. BALANCE_BREAK      — running balance doesn't match prev + credit − debit  (ERROR)
  2. DUPLICATE_ID       — two transactions share the same transaction_id         (ERROR)
  3. SPARSE_TRANSACTIONS— fewer transactions than expected for the period length  (WARNING)
  4. OUT_OF_ORDER       — transaction dates are not non-decreasing               (WARNING)
  5. DATE_OUTSIDE_PERIOD— transaction date is outside the statement period        (WARNING)
  6. EMPTY_DESCRIPTION  — transaction has a blank description                     (WARNING)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from enum import StrEnum

from schema.canonical import StatementDocument, ValidationStatus


class ValidationSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


@dataclass
class ValidationIssue:
    severity: ValidationSeverity
    code: str
    message: str
    transaction_id: str | None = None


@dataclass
class ValidationReport:
    """Full audit result for one StatementDocument."""
    status: ValidationStatus
    issues: list[ValidationIssue] = field(default_factory=list)
    transaction_count: int = 0
    balance_break_count: int = 0

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == ValidationSeverity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == ValidationSeverity.WARNING)

    def issues_by_code(self, code: str) -> list[ValidationIssue]:
        return [i for i in self.issues if i.code == code]


class Validator:
    """Audits a StatementDocument and sets its validation_status."""

    BALANCE_TOLERANCE: Decimal = Decimal("1")   # ₹1 — covers Indian rounding in PDF rendering
    MIN_TXNS_PER_WEEK: float = 0.5              # statements with <0.5 txns/week are flagged sparse
    PERIOD_BUFFER_DAYS: int = 2                 # edge-of-period tolerance for transaction dates

    # ── public API ─────────────────────────────────────────────────────────

    def validate(self, doc: StatementDocument) -> ValidationReport:
        """Run all checks on *doc*, update its validation_status, and return the report."""
        issues: list[ValidationIssue] = []
        issues.extend(self._check_unique_ids(doc))
        issues.extend(self._check_balance_continuity(doc))
        issues.extend(self._check_transaction_density(doc))
        issues.extend(self._check_date_ordering(doc))
        issues.extend(self._check_dates_in_period(doc))
        issues.extend(self._check_descriptions(doc))

        has_errors = any(i.severity == ValidationSeverity.ERROR for i in issues)
        status = ValidationStatus.FAILED if has_errors else ValidationStatus.PASSED
        doc.validation_status = status

        return ValidationReport(
            status=status,
            issues=issues,
            transaction_count=len(doc.transactions),
            balance_break_count=sum(1 for i in issues if i.code == "BALANCE_BREAK"),
        )

    # ── check 1: balance continuity ────────────────────────────────────────

    def _check_balance_continuity(self, doc: StatementDocument) -> list[ValidationIssue]:
        """Verify prev.balance + credit − debit == curr.balance for every consecutive pair.

        Sorted by (page, row) — the physical PDF order — not by date, since same-day
        transactions are ordered exactly as they appear in the source document.
        """
        txns = sorted(
            doc.transactions,
            key=lambda t: (t.source_reference.page, t.source_reference.row),
        )
        issues = []
        for i in range(1, len(txns)):
            prev, curr = txns[i - 1], txns[i]
            expected = (
                prev.balance
                + (curr.credit or Decimal(0))
                - (curr.debit or Decimal(0))
            )
            diff = abs(expected - curr.balance)
            if diff > self.BALANCE_TOLERANCE:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="BALANCE_BREAK",
                    message=(
                        f"Balance break at {curr.date}: "
                        f"prev={prev.balance}, expected={expected}, "
                        f"actual={curr.balance}, diff={diff}"
                    ),
                    transaction_id=curr.transaction_id,
                ))
        return issues

    # ── check 2: duplicate IDs ─────────────────────────────────────────────

    def _check_unique_ids(self, doc: StatementDocument) -> list[ValidationIssue]:
        seen: set[str] = set()
        issues = []
        for txn in doc.transactions:
            if txn.transaction_id in seen:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.ERROR,
                    code="DUPLICATE_ID",
                    message=f"Duplicate transaction_id: {txn.transaction_id}",
                    transaction_id=txn.transaction_id,
                ))
            seen.add(txn.transaction_id)
        return issues

    # ── check 3: transaction density ───────────────────────────────────────

    def _check_transaction_density(self, doc: StatementDocument) -> list[ValidationIssue]:
        period_days = (
            doc.statement_period_end - doc.statement_period_start
        ).days + 1
        if period_days <= 0:
            return []
        expected_min = max(1, period_days * self.MIN_TXNS_PER_WEEK / 7)
        if len(doc.transactions) < expected_min:
            return [ValidationIssue(
                severity=ValidationSeverity.WARNING,
                code="SPARSE_TRANSACTIONS",
                message=(
                    f"{len(doc.transactions)} transactions over {period_days} days "
                    f"(expected at least {expected_min:.0f})"
                ),
            )]
        return []

    # ── check 4: date ordering ─────────────────────────────────────────────

    def _check_date_ordering(self, doc: StatementDocument) -> list[ValidationIssue]:
        """Warn if any transaction date is earlier than the one before it in PDF order."""
        txns = sorted(
            doc.transactions,
            key=lambda t: (t.source_reference.page, t.source_reference.row),
        )
        issues = []
        for i in range(1, len(txns)):
            if txns[i].date < txns[i - 1].date:
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    code="OUT_OF_ORDER",
                    message=(
                        f"Transaction {txns[i].transaction_id} dated {txns[i].date} "
                        f"appears after {txns[i-1].date} in the PDF"
                    ),
                    transaction_id=txns[i].transaction_id,
                ))
        return issues

    # ── check 5: dates within period ───────────────────────────────────────

    def _check_dates_in_period(self, doc: StatementDocument) -> list[ValidationIssue]:
        buffer = timedelta(days=self.PERIOD_BUFFER_DAYS)
        lo = doc.statement_period_start - buffer
        hi = doc.statement_period_end + buffer
        issues = []
        for txn in doc.transactions:
            if not (lo <= txn.date <= hi):
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    code="DATE_OUTSIDE_PERIOD",
                    message=(
                        f"Transaction date {txn.date} is outside the stated period "
                        f"{doc.statement_period_start} – {doc.statement_period_end}"
                    ),
                    transaction_id=txn.transaction_id,
                ))
        return issues

    # ── check 6: non-empty descriptions ────────────────────────────────────

    def _check_descriptions(self, doc: StatementDocument) -> list[ValidationIssue]:
        issues = []
        for txn in doc.transactions:
            if not txn.description.strip():
                issues.append(ValidationIssue(
                    severity=ValidationSeverity.WARNING,
                    code="EMPTY_DESCRIPTION",
                    message=f"Empty description on transaction {txn.transaction_id}",
                    transaction_id=txn.transaction_id,
                ))
        return issues
