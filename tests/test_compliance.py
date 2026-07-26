"""Tests for analysis/compliance.py and jurisdictions/india/rules.py."""
from __future__ import annotations

import datetime
from decimal import Decimal

import pytest

from analysis.compliance import ComplianceAnalyst, ComplianceReport
from analysis.risk import Severity
from jurisdictions.india.rules import (
    INDIA_RULES,
    CashTransactionLimit269ST,
    CashLoanProhibition269SS_269T,
    GSTPaymentConsistency,
    TDSDisbursementPattern,
    PMLAHighValueCash,
    RelatedPartyConcentration,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)

_SRC = SourceReference(file_path="t.pdf", page=0, row=0, raw_text="")


def _txn(tid, *, date=None, debit=None, credit=None, desc="TXN", cat=None,
         is_rp=False, rp_match=None):
    d = date or datetime.date(2024, 1, 15)
    kwargs = dict(
        transaction_id=tid, date=d, description=desc,
        balance=Decimal("0"), source_reference=_SRC,
        category=cat, is_related_party=is_rp, related_party_match=rp_match,
    )
    if debit is not None:
        kwargs["debit"] = Decimal(str(debit))
    else:
        kwargs["credit"] = Decimal(str(credit or 0))
    return CanonicalTransaction(**kwargs)


def _doc(*txns):
    return StatementDocument(
        account_id="test",
        statement_period_start=datetime.date(2024, 1, 1),
        statement_period_end=datetime.date(2024, 6, 30),
        source_format="test",
        transactions=list(txns),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1 — §269ST
# ─────────────────────────────────────────────────────────────────────────────

class TestCashLimit269ST:
    def test_cash_below_threshold_no_exception(self):
        doc = _doc(_txn("t1", credit=150000, desc="CASH DEP-CUSTOMER"))
        rule = CashTransactionLimit269ST()
        assert rule.evaluate(doc) == []

    def test_cash_at_or_above_threshold_raises_exception(self):
        doc = _doc(_txn("t1", credit=200000, desc="CASH DEP-CUSTOMER"))
        rule = CashTransactionLimit269ST()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_269ST"
        assert excs[0].severity == Severity.HIGH

    def test_aggregated_same_party_same_day(self):
        # Two cash deposits on same day from same party = ₹1.5L + ₹80K = ₹2.3L
        doc = _doc(
            _txn("t1", credit=150000, desc="CASH DEP-VENDOR A", date=datetime.date(2024, 1, 10)),
            _txn("t2", credit=80000,  desc="CASH DEP-VENDOR A", date=datetime.date(2024, 1, 10)),
        )
        rule = CashTransactionLimit269ST()
        excs = rule.evaluate(doc)
        assert len(excs) == 1

    def test_no_cash_transactions_no_exception(self):
        doc = _doc(_txn("t1", credit=500000, desc="NEFT/SBIN123/CUSTOMER/INV001"))
        rule = CashTransactionLimit269ST()
        assert rule.evaluate(doc) == []

    def test_exactly_at_threshold_flags(self):
        # §269ST threshold is >= ₹2,00,000. Exactly ₹2,00,000 must flag.
        doc = _doc(_txn("t1", credit=200000, desc="CASH DEP-CUSTOMER"))
        rule = CashTransactionLimit269ST()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_269ST"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 2 — GST consistency
# ─────────────────────────────────────────────────────────────────────────────

class TestGSTConsistencyDisclaimer:
    """Wave 3.3 — GST rule must carry the same 'heuristic, not a statutory
    determination' disclaimer the PMLA and related-party rules already carry."""

    def test_description_carries_heuristic_disclaimer(self) -> None:
        rule = GSTPaymentConsistency()
        assert "heuristic" in rule.description.lower()
        assert "not a statutory" in rule.description.lower()


class TestGSTConsistency:
    def test_revenue_with_gst_no_exception(self):
        # Revenue below ₹40L annual gate — exercises the early-exit path only
        doc = _doc(
            _txn("r1", credit=500000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 1, 15)),
            _txn("g1", debit=90000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 1, 20)),
        )
        rule = GSTPaymentConsistency()
        assert rule.evaluate(doc) == []

    def test_high_revenue_with_gst_present_no_exception(self):
        # 6 × ₹750K = ₹45L — above the ₹40L gate — with GST in every month.
        # This is the true negative case: exercises the per-month check and finds GST present.
        doc = _doc(
            _txn("r1", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 1, 10)),
            _txn("g1", debit=135000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 1, 20)),
            _txn("r2", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 2, 10)),
            _txn("g2", debit=135000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 2, 20)),
            _txn("r3", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 3, 10)),
            _txn("g3", debit=135000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 3, 20)),
            _txn("r4", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 4, 10)),
            _txn("g4", debit=135000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 4, 20)),
            _txn("r5", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 5, 10)),
            _txn("g5", debit=135000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 5, 20)),
            _txn("r6", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 6, 10)),
            _txn("g6", debit=135000, desc="NEFT/GST PAYMENT/GSTIN1234", date=datetime.date(2024, 6, 20)),
        )
        rule = GSTPaymentConsistency()
        assert rule.evaluate(doc) == []

    def test_high_revenue_no_gst_raises_exception(self):
        # 6 × ₹750K = ₹45L > ₹40L GST threshold — no GST payments present
        doc = _doc(
            _txn("r1", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 1, 15)),
            _txn("r2", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 2, 15)),
            _txn("r3", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 3, 15)),
            _txn("r4", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 4, 15)),
            _txn("r5", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 5, 15)),
            _txn("r6", credit=750000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 6, 15)),
        )
        rule = GSTPaymentConsistency()
        excs = rule.evaluate(doc)
        assert len(excs) >= 1
        assert excs[0].rule_id == "IND_GST_CONSISTENCY"

    def test_low_revenue_no_exception(self):
        # Total revenue ₹5L < ₹40L threshold → no exception
        doc = _doc(_txn("r1", credit=500000, cat=TransactionCategory.REVENUE))
        rule = GSTPaymentConsistency()
        assert rule.evaluate(doc) == []

    def test_exception_carries_revenue_transaction_ids(self):
        # When GST is absent, the revenue txn IDs for the flagged months must be attached.
        doc = _doc(
            _txn("rev-jan", credit=750000, cat=TransactionCategory.REVENUE,
                 date=datetime.date(2024, 1, 15)),
            _txn("rev-feb", credit=750000, cat=TransactionCategory.REVENUE,
                 date=datetime.date(2024, 2, 15)),
            _txn("rev-mar", credit=750000, cat=TransactionCategory.REVENUE,
                 date=datetime.date(2024, 3, 15)),
            _txn("rev-apr", credit=750000, cat=TransactionCategory.REVENUE,
                 date=datetime.date(2024, 4, 15)),
            _txn("rev-may", credit=750000, cat=TransactionCategory.REVENUE,
                 date=datetime.date(2024, 5, 15)),
            _txn("rev-jun", credit=750000, cat=TransactionCategory.REVENUE,
                 date=datetime.date(2024, 6, 15)),
        )
        rule = GSTPaymentConsistency()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert len(excs[0].triggering_transaction_ids) > 0
        assert "rev-jan" in excs[0].triggering_transaction_ids


# ─────────────────────────────────────────────────────────────────────────────
# Rule 3 — TDS pattern
# ─────────────────────────────────────────────────────────────────────────────

class TestTDSPattern:
    def test_salary_with_tds_no_exception(self):
        # Salary ₹1.2L annualised — below the ₹5L gate. Exercises the early-exit path only.
        doc = _doc(
            _txn("s1", debit=100000, desc="ECS/SALARY/EMPLOYEE/001", cat=TransactionCategory.SALARY),
            _txn("t1", debit=10000,  desc="NEFT/TDS PAYMENT/TAN12345"),
        )
        rule = TDSDisbursementPattern()
        assert rule.evaluate(doc) == []

    def test_high_salary_with_tds_present_no_exception(self):
        # 6 × ₹500K salary = ₹3M → annualised ₹6M, above the ₹5L gate.
        # TDS payment is present — this is the true negative case that exercises the TDS check.
        doc = _doc(*[
            _txn(f"s{i}", debit=500000, desc="PAYROLL", cat=TransactionCategory.SALARY,
                 date=datetime.date(2024, i, 5))
            for i in range(1, 7)
        ], _txn("tds1", debit=50000, desc="NEFT/TDS PAYMENT/TAN12345",
                date=datetime.date(2024, 1, 7)))
        rule = TDSDisbursementPattern()
        assert rule.evaluate(doc) == []

    def test_high_salary_no_tds_raises(self):
        # 6 months × ₹500K = ₹3M → annualised ₹6M > ₹5L threshold
        doc = _doc(*[
            _txn(f"s{i}", debit=500000, desc="PAYROLL", cat=TransactionCategory.SALARY,
                 date=datetime.date(2024, i, 5))
            for i in range(1, 7)
        ])
        rule = TDSDisbursementPattern()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_TDS_PATTERN"

    def test_small_salary_no_exception(self):
        # Very low salary → no TDS required
        doc = _doc(_txn("s1", debit=30000, desc="SALARY/EMP001", cat=TransactionCategory.SALARY))
        rule = TDSDisbursementPattern()
        assert rule.evaluate(doc) == []

    def test_exception_carries_salary_transaction_ids(self):
        # When TDS is absent, the salary txn IDs must be attached to the exception.
        doc = _doc(*[
            _txn(f"sal-{i}", debit=500000, desc="PAYROLL", cat=TransactionCategory.SALARY,
                 date=datetime.date(2024, i, 5))
            for i in range(1, 7)
        ])
        rule = TDSDisbursementPattern()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert len(excs[0].triggering_transaction_ids) == 6
        assert "sal-1" in excs[0].triggering_transaction_ids


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4 — PMLA high-value cash
# ─────────────────────────────────────────────────────────────────────────────

class TestPMLAHighValueCash:
    def test_cash_below_10l_no_exception(self):
        doc = _doc(_txn("t1", credit=900000, desc="CASH DEP-SHOP"))
        rule = PMLAHighValueCash()
        assert rule.evaluate(doc) == []

    def test_cash_above_10l_in_30_days_raises(self):
        doc = _doc(
            _txn("t1", credit=500000, desc="CASH DEP-SHOP", date=datetime.date(2024, 1, 1)),
            _txn("t2", credit=300000, desc="CASH DEP-SHOP", date=datetime.date(2024, 1, 10)),
            _txn("t3", credit=300000, desc="CASH DEP-SHOP", date=datetime.date(2024, 1, 20)),
        )
        rule = PMLAHighValueCash()
        excs = rule.evaluate(doc)
        assert len(excs) >= 1
        assert excs[0].rule_id == "IND_PMLA_HVC"
        assert excs[0].severity == Severity.HIGH

    def test_no_cash_transactions_no_exception(self):
        doc = _doc(_txn("t1", credit=1500000, desc="NEFT/SBIN/CUSTOMER/INV001"))
        rule = PMLAHighValueCash()
        assert rule.evaluate(doc) == []

    def test_exactly_at_10l_threshold_flags(self):
        # Threshold is >= ₹10L in a 30-day window. Exactly ₹10,00,000 must flag.
        doc = _doc(
            _txn("t1", credit=600000, desc="CASH DEP-SHOP", date=datetime.date(2024, 1, 1)),
            _txn("t2", credit=400000, desc="CASH DEP-SHOP", date=datetime.date(2024, 1, 15)),
        )
        rule = PMLAHighValueCash()
        excs = rule.evaluate(doc)
        assert len(excs) >= 1
        assert excs[0].rule_id == "IND_PMLA_HVC"


# ─────────────────────────────────────────────────────────────────────────────
# Rule 5 — Related-party concentration
# ─────────────────────────────────────────────────────────────────────────────

class TestRelatedPartyConcentration:
    def test_low_rp_share_no_exception(self):
        doc = _doc(
            _txn("t1", debit=10000, is_rp=True, rp_match="AffiliateA"),
            _txn("t2", debit=200000),
        )
        rule = RelatedPartyConcentration()
        assert rule.evaluate(doc) == []

    def test_high_rp_share_raises(self):
        doc = _doc(
            _txn("t1", debit=250000, is_rp=True, rp_match="AffiliateA"),
            _txn("t2", debit=50000),
        )
        rule = RelatedPartyConcentration()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_RPT_CONCENTRATION"

    def test_no_related_party_txns_no_exception(self):
        doc = _doc(_txn("t1", debit=500000))
        rule = RelatedPartyConcentration()
        assert rule.evaluate(doc) == []


# ─────────────────────────────────────────────────────────────────────────────
# Rule 6 — §269SS / §269T cash loan prohibition
# ─────────────────────────────────────────────────────────────────────────────

class TestCashLoanProhibition269SS269T:
    def test_cash_loan_in_above_threshold_flags(self):
        # LOAN_IN via cash ₹50K >= ₹20K → §269SS violation
        doc = _doc(_txn("l1", credit=50000, desc="CASH DEP-LOAN RECEIVED",
                         cat=TransactionCategory.LOAN_IN))
        rule = CashLoanProhibition269SS_269T()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_269SS_269T"
        assert excs[0].severity == Severity.HIGH
        assert "l1" in excs[0].triggering_transaction_ids

    def test_cash_loan_out_above_threshold_flags(self):
        # LOAN_OUT via cash ₹50K >= ₹20K → §269T violation
        doc = _doc(_txn("l2", debit=50000, desc="CASH WITH-LOAN REPAID",
                         cat=TransactionCategory.LOAN_OUT))
        rule = CashLoanProhibition269SS_269T()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_269SS_269T"
        assert "l2" in excs[0].triggering_transaction_ids

    def test_neft_loan_in_does_not_flag(self):
        # LOAN_IN via NEFT — not a cash transaction, must not flag
        doc = _doc(_txn("l3", credit=500000, desc="NEFT/SBIN123/LOAN FROM DIRECTOR",
                         cat=TransactionCategory.LOAN_IN))
        rule = CashLoanProhibition269SS_269T()
        assert rule.evaluate(doc) == []

    def test_exactly_at_threshold_flags(self):
        # Boundary is >= ₹20,000; exactly ₹20,000 must flag
        doc = _doc(_txn("l4", credit=20000, desc="CASH DEP-LOAN",
                         cat=TransactionCategory.LOAN_IN))
        rule = CashLoanProhibition269SS_269T()
        excs = rule.evaluate(doc)
        assert len(excs) == 1
        assert excs[0].rule_id == "IND_269SS_269T"


# ─────────────────────────────────────────────────────────────────────────────
# ComplianceAnalyst integration
# ─────────────────────────────────────────────────────────────────────────────

class TestComplianceAnalyst:
    def test_returns_compliance_report(self):
        doc = _doc(_txn("t1", credit=100000))
        report = ComplianceAnalyst(llm_enabled=False).analyse(doc, INDIA_RULES)
        assert isinstance(report, ComplianceReport)

    def test_clean_statement_minimal_exceptions(self):
        # Healthy statement with GST, TDS, no cash — genuinely below every
        # rule's trigger threshold, so it must produce ZERO exceptions, not
        # just "some ComplianceReport object" (Wave 4.1).
        doc = _doc(
            _txn("r1", credit=500000, cat=TransactionCategory.REVENUE, date=datetime.date(2024, 1, 15)),
            _txn("g1", debit=90000, desc="GST PAYMENT", date=datetime.date(2024, 1, 20)),
            _txn("t1", debit=10000, desc="TDS PAYMENT", date=datetime.date(2024, 2, 7)),
        )
        report = ComplianceAnalyst(llm_enabled=False).analyse(doc, INDIA_RULES)
        assert isinstance(report, ComplianceReport)
        assert report.exceptions == []
        assert report.has_exceptions is False
        assert report.by_severity == {"HIGH": 0, "MEDIUM": 0, "LOW": 0}

    def test_sorted_high_first(self):
        doc = _doc(_txn("t1", credit=250000, desc="CASH DEP-VENDOR A"))
        report = ComplianceAnalyst(llm_enabled=False).analyse(doc, INDIA_RULES)
        if len(report.exceptions) > 1:
            sev_order = ["HIGH", "MEDIUM", "LOW"]
            for i in range(len(report.exceptions) - 1):
                a = sev_order.index(str(report.exceptions[i].severity))
                b = sev_order.index(str(report.exceptions[i + 1].severity))
                assert a <= b

    def test_by_severity_counts(self):
        doc = _doc(_txn("t1", credit=250000, desc="CASH DEP-VENDOR"))
        report = ComplianceAnalyst(llm_enabled=False).analyse(doc, INDIA_RULES)
        total = sum(report.by_severity.values())
        assert total == len(report.exceptions)
