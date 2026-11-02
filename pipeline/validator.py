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

from schema.canonical import CanonicalTransaction, StatementDocument, ValidationStatus


class ValidationSeverity(StrEnum):
    ERROR = "ERROR"
    WARNING = "WARNING"


class ValidationFailedError(Exception):
    """Raised by pipeline entry points when a statement fails validation.

    The pipeline must not silently produce a normal report from data that
    failed balance-continuity or integrity checks — callers halt on this
    rather than continuing to categorisation/analysis/reports.
    """

    def __init__(
        self,
        report: ValidationReport,
        dq_report: DQReport | None = None,
        doc: StatementDocument | None = None,
    ) -> None:
        self.report = report
        # Phase 2: the DQ report and the normalised document travel with the
        # failure, so a failed job can still persist them for GET /dq.
        self.dq_report = dq_report
        self.doc = doc
        issue_lines = [
            f"[{i.severity}] {i.code}"
            + (f" (transaction_id={i.transaction_id})" if i.transaction_id else "")
            + f": {i.message}"
            for i in report.issues
            if i.severity == ValidationSeverity.ERROR
        ]
        message = (
            f"Validation failed with {report.error_count} error(s) across "
            f"{report.transaction_count} transactions:\n" + "\n".join(issue_lines)
        )
        super().__init__(message)


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


# ─────────────────────────────────────────────────────────────────────────────
# Data-quality report (Phase 2, UPGRADE_GUIDE §8.2)
#
# Additive and informational. ``Validator.validate`` above still decides
# PASSED/FAILED (₹1 tolerance) and gates the pipeline exactly as before. The
# DQ report runs stricter, finer-grained checks, and every check reports
# ``{name, passed, severity, count, sample_ids}``. It is persisted to
# ``dq_reports`` and served by ``GET /v1/runs/{id}/dq``. sample_ids are
# transaction ids only, never descriptions or amounts (§1.9).
# ─────────────────────────────────────────────────────────────────────────────

DQ_SEVERITY_ERROR = "error"
DQ_SEVERITY_WARNING = "warning"
DQ_SEVERITY_INFO = "info"

_DQ_SAMPLE_LIMIT = 20


@dataclass
class DQCheck:
    name: str
    passed: bool
    severity: str
    count: int = 0
    sample_ids: list[str] = field(default_factory=list)
    skipped: bool = False
    detail: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "passed": self.passed,
            "severity": self.severity,
            "count": self.count,
            "sample_ids": list(self.sample_ids),
            "skipped": self.skipped,
            "detail": self.detail,
        }


@dataclass
class DQReport:
    checks: list[DQCheck]

    @property
    def passed(self) -> bool:
        """True when no error-severity check failed (warnings/info don't fail the report)."""
        return all(c.passed for c in self.checks if c.severity == DQ_SEVERITY_ERROR)

    def check(self, name: str) -> DQCheck:
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)

    def to_dict(self) -> dict[str, object]:
        return {"passed": self.passed, "checks": [c.to_dict() for c in self.checks]}


def _dq(name: str, severity: str, ids: list[str], detail: str = "") -> DQCheck:
    return DQCheck(
        name=name,
        passed=not ids,
        severity=severity,
        count=len(ids),
        sample_ids=ids[:_DQ_SAMPLE_LIMIT],
        detail=detail,
    )


def parse_header_amount(raw: str | None) -> Decimal | None:
    """Parse an Indian-format header amount ("1,20,000.00"); None if blank/unparseable."""
    if not raw:
        return None
    cleaned = raw.replace(",", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except ArithmeticError:
        return None


def build_dq_report(
    doc: StatementDocument,
    *,
    opening_balance: Decimal | None = None,
    closing_balance: Decimal | None = None,
    balance_tolerance: Decimal = Decimal("0.01"),
    outlier_z: float = 3.5,
) -> DQReport:
    """Run the §8.2 data-quality checks on *doc*. Does not mutate it."""
    txns = sorted(doc.transactions, key=lambda t: (t.source_reference.page, t.source_reference.row))
    checks: list[DQCheck] = []

    # 1. required fields
    missing = [
        t.transaction_id or f"row:{t.source_reference.page}:{t.source_reference.row}"
        for t in txns
        if not t.transaction_id or not t.description.strip()
    ]
    checks.append(_dq("required_fields", DQ_SEVERITY_ERROR, missing,
                      "transaction_id and description present"))

    # 2. date within statement period (no buffer; the validator's gate allows ±2 days)
    out_of_range = [
        t.transaction_id for t in txns
        if not (doc.statement_period_start <= t.date <= doc.statement_period_end)
    ]
    checks.append(_dq("date_in_period", DQ_SEVERITY_WARNING, out_of_range,
                      f"{doc.statement_period_start}..{doc.statement_period_end}"))

    # 3. duplicate rows (same date + amount + description + balance)
    seen: dict[tuple[object, ...], str] = {}
    dupes: list[str] = []
    for t in txns:
        key = (t.date, t.debit, t.credit, t.description.strip().upper(), t.balance)
        if key in seen:
            dupes.append(t.transaction_id)
        else:
            seen[key] = t.transaction_id
    checks.append(_dq("duplicate_rows", DQ_SEVERITY_WARNING, dupes))

    # 4. debit/credit exclusivity (the schema enforces it; re-checked for defence in depth)
    non_exclusive = [t.transaction_id for t in txns if (t.debit is None) == (t.credit is None)]
    checks.append(_dq("debit_credit_exclusive", DQ_SEVERITY_ERROR, non_exclusive))

    # 5. running-balance reconciliation: prev_balance - debit + credit == balance
    breaks: list[str] = []
    for prev, curr in zip(txns, txns[1:]):
        expected = prev.balance - (curr.debit or Decimal(0)) + (curr.credit or Decimal(0))
        if abs(expected - curr.balance) > balance_tolerance:
            breaks.append(curr.transaction_id)
    checks.append(_dq("running_balance", DQ_SEVERITY_ERROR, breaks,
                      f"tolerance {balance_tolerance}; sample_ids are the rows where the break appears"))

    # 6. opening / closing balance vs header
    if opening_balance is None or not txns:
        checks.append(DQCheck("opening_balance_vs_header", True, DQ_SEVERITY_ERROR, skipped=True,
                              detail="no opening balance in the statement header"))
    else:
        first = txns[0]
        implied = first.balance + (first.debit or Decimal(0)) - (first.credit or Decimal(0))
        bad = [first.transaction_id] if abs(implied - opening_balance) > balance_tolerance else []
        checks.append(_dq("opening_balance_vs_header", DQ_SEVERITY_ERROR, bad))
    if closing_balance is None or not txns:
        checks.append(DQCheck("closing_balance_vs_header", True, DQ_SEVERITY_ERROR, skipped=True,
                              detail="no closing balance in the statement header"))
    else:
        last = txns[-1]
        bad = [last.transaction_id] if abs(last.balance - closing_balance) > balance_tolerance else []
        checks.append(_dq("closing_balance_vs_header", DQ_SEVERITY_ERROR, bad))

    # 7. outlier amounts: modified z-score on log(amount) (median/MAD, Iglewicz-Hoaglin)
    checks.append(_outlier_check(txns, outlier_z))

    # 8. non-INR currency
    non_inr = [t.transaction_id for t in txns if (t.currency or "").upper() != "INR"]
    checks.append(_dq("currency_inr", DQ_SEVERITY_WARNING, non_inr))

    return DQReport(checks=checks)


def _outlier_check(txns: list[CanonicalTransaction], z_cut: float) -> DQCheck:
    import math
    import statistics

    amounts = [
        (t.transaction_id, float(t.debit if t.debit is not None else t.credit or 0))
        for t in txns
    ]
    logs = [(tid, math.log(a)) for tid, a in amounts if a > 0]
    if len(logs) < 8:
        return DQCheck("outlier_amounts", True, DQ_SEVERITY_INFO, skipped=True,
                       detail="fewer than 8 non-zero amounts")
    values = [v for _, v in logs]
    med = statistics.median(values)
    mad = statistics.median(abs(v - med) for v in values)
    if mad == 0:
        return DQCheck("outlier_amounts", True, DQ_SEVERITY_INFO, skipped=True,
                       detail="MAD is zero")
    outliers = [tid for tid, v in logs if abs(0.6745 * (v - med) / mad) > z_cut]
    return _dq("outlier_amounts", DQ_SEVERITY_INFO, outliers, f"|modified z| > {z_cut} on log amount")
