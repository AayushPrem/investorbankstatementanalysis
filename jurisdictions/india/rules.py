"""Indian jurisdiction compliance rules.

Implements six rule evaluators covering:
  1. §269ST cash transaction limit (Income Tax Act)
  2. GST payment consistency (CGST Act)
  3. TDS disbursement pattern (Income Tax Act §194)
  4. PMLA high-value cash aggregation (Prevention of Money-Laundering Act)
  5. Related-party transaction concentration (Companies Act §188)
  6. §269SS/§269T cash loan prohibition (Income Tax Act)

Each rule exposes:
  rule_id, name, regulatory_citation, severity, description
  evaluate(document) -> list[ComplianceException]
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from analysis.risk import Severity
from jurisdictions import ComplianceException

if TYPE_CHECKING:
    from schema.canonical import StatementDocument, TransactionCategory

_ZERO = Decimal("0")
_LAKH = Decimal("100000")

# ─────────────────────────────────────────────────────────────────────────────
# Operative thresholds — named constants for auditability.
# All monetary values in INR. Change a value here to affect all rules uniformly.
# ─────────────────────────────────────────────────────────────────────────────

# §269ST: cash receipts >= ₹2L per counterparty per day are prohibited.
_269ST_CASH_LIMIT = Decimal("200000")

# §269SS/§269T: loans or deposits accepted/repaid in cash >= ₹20,000 are prohibited.
_269SS_T_CASH_LOAN_LIMIT = Decimal("20000")

# GST annual turnover gate: businesses with aggregate turnover > ₹40L must register
# and file monthly returns (CGST Act §22 / §39). ₹40L = ₹4,000,000.
_GST_ANNUAL_TURNOVER_GATE = Decimal("4000000")

# GST per-month revenue floor: months with revenue below this are skipped when checking
# for missing GST payments. Chosen as ~1/13th of the ₹40L annual gate so months with
# minor or nil activity don't generate false positives.
_GST_MONTHLY_REVENUE_FLOOR = Decimal("300000")

# TDS salary gate: annualised salary above ₹5L/year triggers mandatory TDS under §192.
_TDS_ANNUAL_SALARY_GATE = Decimal("500000")

# CTR cash-window threshold: aggregate cash >= ₹10L in a rolling 30-day window
# matches the pattern for a Cash Transaction Report under PMLA Rules 2005 Rule 3(1)(a).
_CTR_CASH_WINDOW_THRESHOLD = _LAKH * 10  # ₹10,00,000

# Related-party concentration: related-party debits as a share of total debits above
# this fraction are flagged as a governance concentration signal.
_RPT_CONCENTRATION_THRESHOLD = 0.20  # 20%


# ─────────────────────────────────────────────────────────────────────────────
# Helper patterns
# ─────────────────────────────────────────────────────────────────────────────

_CASH_DESC_RE = re.compile(r"CASH\s*DEP|CASH\s*WITH|CASH\s*DEPOS", re.I)
_GST_DESC_RE  = re.compile(r"GST\s*(PAYMENT|PMT)|GSTIN", re.I)
_TDS_DESC_RE  = re.compile(r"TDS\s*PAYMENT", re.I)
_SALARY_DESC_RE = re.compile(r"\bSALARY\b|\bPAYROLL\b|\bWAGES\b", re.I)
_TAX_DESC_RE = re.compile(
    r"GST|TDS|INCOME\s*TAX|PROFESSIONAL\s*TAX|ESI|PROVIDENT\s*FUND|PF\s*PAYMENT", re.I
)


def _window_start(d: date, window_days: int) -> date:
    return d - timedelta(days=window_days - 1)


# ─────────────────────────────────────────────────────────────────────────────
# Rule 1 — §269ST cash transaction limit
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CashTransactionLimit269ST:
    rule_id: str = "IND_269ST"
    name: str = "Cash Transaction Limit (§269ST)"
    regulatory_citation: str = "Income Tax Act, 1961 §269ST"
    severity: Severity = Severity.HIGH
    description: str = (
        "§269ST prohibits receipt of any sum ≥ ₹2 lakh in cash from a single person "
        "in a day or for a single transaction or event."
    )
    _threshold: Decimal = field(default=_269ST_CASH_LIMIT, init=False, repr=False)

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        from schema.canonical import TransactionCategory

        cash_credits = [
            t for t in document.transactions
            if t.credit is not None and _CASH_DESC_RE.search(t.description)
        ]
        if not cash_credits:
            return []

        # Group by (counterparty or description, date)
        by_party_date: dict[tuple[str, date], list] = defaultdict(list)
        for t in cash_credits:
            key = (t.counterparty or t.description[:40], t.date)
            by_party_date[key].append(t)

        exceptions: list[ComplianceException] = []
        for (party, txn_date), txns in by_party_date.items():
            total = sum(t.credit for t in txns if t.credit)  # type: ignore[misc]
            if total >= self._threshold:
                exceptions.append(ComplianceException(
                    rule_id=self.rule_id,
                    rule_name=self.name,
                    regulatory_citation=self.regulatory_citation,
                    severity=self.severity,
                    description=(
                        f"Cash receipts from '{party}' on {txn_date} total "
                        f"₹{total:,.0f} — exceeds §269ST limit of ₹2,00,000."
                    ),
                    investor_risk_framing=(
                        f"A ₹{total:,.0f} cash receipt on {txn_date} violates §269ST of the Income Tax Act. "
                        "Violations attract a penalty equal to the transaction amount and signal unreported "
                        "cash flows that could materially misrepresent the company's financial position."
                    ),
                    triggering_transaction_ids=[t.transaction_id for t in txns],
                    evidence={"total_cash": float(total), "date": str(txn_date), "party": party},
                ))
        return exceptions


# ─────────────────────────────────────────────────────────────────────────────
# Rule 2 — GST payment consistency
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class GSTPaymentConsistency:
    rule_id: str = "IND_GST_CONSISTENCY"
    name: str = "GST Payment Consistency"
    regulatory_citation: str = "Central Goods and Services Tax Act, 2017 §39"
    severity: Severity = Severity.MEDIUM
    description: str = (
        "Businesses with aggregate turnover > ₹40 lakh must file and pay GST monthly. "
        "Absence of GST outflows when significant revenue is present is a red flag."
    )
    _threshold_revenue: Decimal = field(default=_GST_ANNUAL_TURNOVER_GATE, init=False, repr=False)

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        from schema.canonical import TransactionCategory

        total_revenue = sum(
            t.credit for t in document.transactions
            if t.category == TransactionCategory.REVENUE and t.credit
        )
        if total_revenue < self._threshold_revenue:
            return []

        # Check months that have revenue for corresponding GST payments
        by_month: dict[str, dict] = defaultdict(lambda: {"rev": _ZERO, "gst": False, "ids": []})
        for t in document.transactions:
            ym = t.date.strftime("%Y-%m")
            if t.category == TransactionCategory.REVENUE and t.credit:
                by_month[ym]["rev"] += t.credit
                by_month[ym]["ids"].append(t.transaction_id)
            if t.debit and _GST_DESC_RE.search(t.description):
                by_month[ym]["gst"] = True

        flagged_months = [
            (ym, data)
            for ym, data in sorted(by_month.items())
            if data["rev"] > _GST_MONTHLY_REVENUE_FLOOR and not data["gst"]
        ]
        if not flagged_months:
            return []

        missing_months = [ym for ym, _ in flagged_months]
        triggering_ids = [tid for _, data in flagged_months for tid in data["ids"]]

        return [ComplianceException(
            rule_id=self.rule_id,
            rule_name=self.name,
            regulatory_citation=self.regulatory_citation,
            severity=self.severity,
            description=(
                f"No GST payment found in {len(missing_months)} month(s) despite significant revenue: "
                + ", ".join(missing_months) + "."
            ),
            investor_risk_framing=(
                f"GST was absent in {len(missing_months)} months where material revenue was recorded. "
                "Under CGST §39, monthly filing is mandatory above ₹40L turnover. Non-compliance attracts "
                "18% interest plus penalties and creates contingent tax liabilities the investor must price."
            ),
            triggering_transaction_ids=triggering_ids,
            evidence={"missing_months": missing_months},
        )]


# ─────────────────────────────────────────────────────────────────────────────
# Rule 3 — TDS disbursement pattern
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class TDSDisbursementPattern:
    rule_id: str = "IND_TDS_PATTERN"
    name: str = "TDS Disbursement Pattern"
    regulatory_citation: str = "Income Tax Act, 1961 §194A/§194C/§192"
    severity: Severity = Severity.MEDIUM
    description: str = (
        "Employers paying salaries above ₹5L/year must deduct and deposit TDS by the 7th of the "
        "following month (§192). Absence of TDS when salaries are present indicates non-compliance."
    )
    _min_annual_salary: Decimal = field(default=_TDS_ANNUAL_SALARY_GATE, init=False, repr=False)

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        from schema.canonical import TransactionCategory

        total_salary = sum(
            t.debit for t in document.transactions
            if t.category == TransactionCategory.SALARY and t.debit
        )
        months_covered = max(
            (document.statement_period_end - document.statement_period_start).days // 30, 1
        )
        annualised_salary = total_salary * Decimal("12") / Decimal(str(months_covered))
        if annualised_salary < self._min_annual_salary:
            return []

        salary_txns = [
            t for t in document.transactions
            if t.category == TransactionCategory.SALARY and t.debit
        ]
        has_tds = any(_TDS_DESC_RE.search(t.description) for t in document.transactions if t.debit)
        if has_tds:
            return []

        return [ComplianceException(
            rule_id=self.rule_id,
            rule_name=self.name,
            regulatory_citation=self.regulatory_citation,
            severity=self.severity,
            description=(
                f"Total salary outflow is ₹{total_salary:,.0f} over the period with no TDS payment found. "
                "TDS on salaries must be deposited by the 7th of each following month."
            ),
            investor_risk_framing=(
                f"The company paid ₹{total_salary:,.0f} in salaries but shows no TDS deduction. "
                "Under §192, employers must withhold and remit tax on salaries above ₹5L/year. "
                "Outstanding TDS is a liability that TRACES to the employer and can block regulatory clearances."
            ),
            triggering_transaction_ids=[t.transaction_id for t in salary_txns],
            evidence={"total_salary": float(total_salary), "annualised_salary": float(annualised_salary)},
        )]


# ─────────────────────────────────────────────────────────────────────────────
# Rule 4 — PMLA high-value cash aggregation
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PMLAHighValueCash:
    rule_id: str = "IND_PMLA_HVC"
    name: str = "High-Value Cash Aggregation (CTR-style detection)"
    regulatory_citation: str = "PMLA Rules, 2005 Rule 3(1)(a) — CTR-style aggregate cash detection"
    severity: Severity = Severity.HIGH
    description: str = (
        "Aggregate cash transactions (credits + debits) ≥ ₹10 lakh in any rolling 30-day window "
        "are flagged as a CTR-style signal under PMLA Rules 2005 Rule 3(1)(a). "
        "This is a cash-volume heuristic, not a statutory STR determination."
    )
    _stir_threshold: Decimal = field(default=_CTR_CASH_WINDOW_THRESHOLD, init=False, repr=False)

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        cash_txns = sorted(
            [t for t in document.transactions if _CASH_DESC_RE.search(t.description)],
            key=lambda t: t.date,
        )
        if not cash_txns:
            return []

        exceptions: list[ComplianceException] = []
        seen_windows: set[tuple] = set()

        for i, anchor in enumerate(cash_txns):
            window_end = anchor.date
            window_start = _window_start(window_end, 30)
            window_txns = [t for t in cash_txns if window_start <= t.date <= window_end]
            total = sum(
                (t.credit or _ZERO) + (t.debit or _ZERO) for t in window_txns
            )
            if total >= self._stir_threshold:
                ids = tuple(sorted(t.transaction_id for t in window_txns))
                if ids in seen_windows:
                    continue
                seen_windows.add(ids)
                exceptions.append(ComplianceException(
                    rule_id=self.rule_id,
                    rule_name=self.name,
                    regulatory_citation=self.regulatory_citation,
                    severity=self.severity,
                    description=(
                        f"Cash transactions totalling ₹{total:,.0f} in 30-day window "
                        f"{window_start} → {window_end} meet the PMLA STR reporting threshold."
                    ),
                    investor_risk_framing=(
                        f"₹{total:,.0f} in cash activity within 30 days matches the volume pattern "
                        "that triggers a Cash Transaction Report (CTR) under PMLA Rules 2005 Rule 3(1)(a). "
                        "This level of cash concentration warrants explanation; sustained cash-heavy operations "
                        "can attract regulatory scrutiny and create reputational risk for the company."
                    ),
                    triggering_transaction_ids=[t.transaction_id for t in window_txns],
                    evidence={"window_total": float(total), "start": str(window_start),
                              "end": str(window_end), "transaction_count": len(window_txns)},
                ))
        return exceptions


# ─────────────────────────────────────────────────────────────────────────────
# Rule 5 — Related-party transaction concentration
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class RelatedPartyConcentration:
    rule_id: str = "IND_RPT_CONCENTRATION"
    name: str = "Related-Party Transaction Concentration"
    regulatory_citation: str = (
        "Companies Act §188 governance principle — heuristic based on outflow concentration"
    )
    severity: Severity = Severity.MEDIUM
    description: str = (
        "Related-party outflows exceed 20% of total outflows over the statement period. "
        "This is a concentration heuristic, not a statutory §188 determination (which requires "
        "net-worth data not available from a bank statement alone)."
    )
    _concentration_threshold: float = _RPT_CONCENTRATION_THRESHOLD

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        related_debits = sum(
            t.debit for t in document.transactions
            if t.is_related_party and t.debit
        )
        total_debits = sum(t.debit for t in document.transactions if t.debit) or _ZERO
        if total_debits == _ZERO or related_debits == _ZERO:
            return []

        share = float(related_debits / total_debits)
        if share < self._concentration_threshold:
            return []

        by_party: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        for t in document.transactions:
            if t.is_related_party and t.debit and t.related_party_match:
                by_party[t.related_party_match] += t.debit
        top_party = max(by_party, key=lambda k: by_party[k]) if by_party else "unknown"

        return [ComplianceException(
            rule_id=self.rule_id,
            rule_name=self.name,
            regulatory_citation=self.regulatory_citation,
            severity=self.severity,
            description=(
                f"{share*100:.0f}% of total outflows (₹{related_debits:,.0f}) are to related parties. "
                f"Largest counterparty: '{top_party}'."
            ),
            investor_risk_framing=(
                f"{share*100:.0f}% of total outflows over the statement period flow to related parties "
                f"(₹{related_debits:,.0f}). This is a governance concentration heuristic — bank statements "
                "do not carry net-worth data, so this is not a statutory §188 evaluation. "
                "High related-party concentration warrants investigation into whether board approval "
                "processes under §188 were followed, and whether the payments represent arm's-length terms."
            ),
            triggering_transaction_ids=[
                t.transaction_id for t in document.transactions if t.is_related_party and t.debit
            ],
            evidence={"related_party_share": round(share, 4), "related_debits": float(related_debits),
                      "top_counterparty": top_party},
        )]


# ─────────────────────────────────────────────────────────────────────────────
# Rule 6 — §269SS / §269T cash loan prohibition
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CashLoanProhibition269SS_269T:
    rule_id: str = "IND_269SS_269T"
    name: str = "Cash Loan / Deposit Prohibition (§269SS, §269T)"
    regulatory_citation: str = "Income Tax Act, 1961 §269SS, §269T"
    severity: Severity = Severity.HIGH
    description: str = (
        "§269SS prohibits accepting any loan or deposit of ₹20,000 or more in cash. "
        "§269T prohibits repaying any loan or deposit of ₹20,000 or more in cash. "
        "Violations attract a penalty equal to the loan/deposit amount."
    )
    _threshold: Decimal = field(default=_269SS_T_CASH_LOAN_LIMIT, init=False, repr=False)

    def evaluate(self, document: "StatementDocument") -> list[ComplianceException]:
        from schema.canonical import TransactionCategory

        exceptions: list[ComplianceException] = []
        for t in document.transactions:
            if t.category not in (TransactionCategory.LOAN_IN, TransactionCategory.LOAN_OUT):
                continue
            if not _CASH_DESC_RE.search(t.description):
                continue
            amount = (t.credit or _ZERO) if t.category == TransactionCategory.LOAN_IN else (t.debit or _ZERO)
            if amount < self._threshold:
                continue
            section = "§269SS" if t.category == TransactionCategory.LOAN_IN else "§269T"
            direction = "received" if t.category == TransactionCategory.LOAN_IN else "repaid"
            exceptions.append(ComplianceException(
                rule_id=self.rule_id,
                rule_name=self.name,
                regulatory_citation=self.regulatory_citation,
                severity=self.severity,
                description=(
                    f"Cash loan/deposit of ₹{amount:,.0f} {direction} on {t.date} — "
                    f"violates {section} (cash loans >= ₹20,000 are prohibited)."
                ),
                investor_risk_framing=(
                    f"A cash loan/deposit of ₹{amount:,.0f} was {direction} on {t.date}, "
                    f"breaching {section} of the Income Tax Act. The penalty under {section} equals "
                    "the full loan amount. This creates a contingent tax liability of equal magnitude "
                    "and signals unreported financing arrangements that a due-diligence team must resolve."
                ),
                triggering_transaction_ids=[t.transaction_id],
                evidence={"amount": float(amount), "date": str(t.date), "direction": direction},
            ))
        return exceptions


# ─────────────────────────────────────────────────────────────────────────────
# Module assembly
# ─────────────────────────────────────────────────────────────────────────────

INDIA_RULES = [
    CashTransactionLimit269ST(),
    GSTPaymentConsistency(),
    TDSDisbursementPattern(),
    PMLAHighValueCash(),
    RelatedPartyConcentration(),
    CashLoanProhibition269SS_269T(),
]
