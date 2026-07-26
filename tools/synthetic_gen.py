#!/usr/bin/env python3
"""Synthetic Indian bank statement generator — HDFC and ICICI formats.

Generates realistic bank statements with:
  - Real Indian company / vendor names
  - Seasonal revenue patterns (Diwali peak, FY-start dip, March year-end push)
  - Payment delays (customers sometimes pay 15–45 days late)
  - Salary hike in April (Indian FY start)
  - Probabilistic risk flags ("realistic" mode mimics real-world incidence rates)
  - Variable duration: 3 – 24 months

CLI:
    python -m tools.synthetic_gen --bank hdfc --profile healthy_saas --months 12 --output data/synthetic/
    python -m tools.synthetic_gen --bank icici --profile burning_startup --months 6 --risk-mode realistic
    python -m tools.synthetic_gen --bank hdfc --profile ecommerce --months 9 --flags structuring,founder_extraction
"""
from __future__ import annotations

import argparse
import json
import random
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Optional

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MARGIN = 10 * mm
_BANK_CODES = ["SBIN", "HDFC", "ICIC", "UTIB", "KKBK", "YESB", "PUNB", "BARB", "CITI", "AXIS"]

# Indian FY seasonal revenue multipliers (month → multiplier)
_SEASONAL = {
    1: 0.88,  # Jan — post-holiday slowdown
    2: 0.92,  # Feb
    3: 1.20,  # Mar — year-end budget flush, Q4 close
    4: 0.78,  # Apr — new FY, budgets not yet approved
    5: 0.88,  # May
    6: 0.95,  # Jun — Q1 close
    7: 0.98,  # Jul
    8: 0.97,  # Aug
    9: 1.05,  # Sep — Q2 close
    10: 1.10, # Oct — Diwali season begins, enterprise buying picks up
    11: 1.18, # Nov — Diwali peak, year-end deals
    12: 1.08, # Dec — Q3 close, international enterprise budgets
}

# ─────────────────────────────────────────────────────────────────────────────
# Realistic company / vendor name lists
# ─────────────────────────────────────────────────────────────────────────────

# SaaS B2B customers — mid-to-large Indian enterprises
_SAAS_CUSTOMERS: list[tuple[str, str]] = [
    ("cust_001", "Tata Consultancy Services Ltd"),
    ("cust_002", "Wipro Technologies Ltd"),
    ("cust_003", "HCL Technologies Ltd"),
    ("cust_004", "Infosys BPM Ltd"),
    ("cust_005", "Mahindra & Mahindra Ltd"),
    ("cust_006", "Bajaj Auto Ltd"),
    ("cust_007", "HDFC Life Insurance Co Ltd"),
    ("cust_008", "Axis Bank Ltd"),
    ("cust_009", "Kotak Mahindra Bank Ltd"),
    ("cust_010", "Reliance Retail Ventures Ltd"),
    ("cust_011", "Zepto Grocery Pvt Ltd"),
    ("cust_012", "PhonePe Pvt Ltd"),
    ("cust_013", "Meesho Pvt Ltd"),
    ("cust_014", "Swiggy Instamart Pvt Ltd"),
    ("cust_015", "Cred Fintech Pvt Ltd"),
    ("cust_016", "Groww Fintech Pvt Ltd"),
    ("cust_017", "BharatPe Pvt Ltd"),
    ("cust_018", "Lenskart Solutions Pvt Ltd"),
    ("cust_019", "Boat Lifestyle Pvt Ltd"),
    ("cust_020", "Sugar Cosmetics Pvt Ltd"),
    ("cust_021", "Wakefit Innovations Pvt Ltd"),
    ("cust_022", "Mensa Brands Pvt Ltd"),
    ("cust_023", "MamaEarth Consumer Pvt Ltd"),
    ("cust_024", "FreshMenu Foods Pvt Ltd"),
    ("cust_025", "Zetwerk Manufacturing Pvt Ltd"),
    ("cust_026", "Delhivery Ltd"),
    ("cust_027", "Shadowfax Technologies Pvt Ltd"),
    ("cust_028", "Udaan B2B Ltd"),
    ("cust_029", "Moglix Enterprises Pvt Ltd"),
    ("cust_030", "Nykaa Fashion Ltd"),
    ("cust_031", "Purplle Beauty Pvt Ltd"),
    ("cust_032", "CleverTap Pvt Ltd"),
    ("cust_033", "Leadsquared Pvt Ltd"),
    ("cust_034", "WebEngage Pvt Ltd"),
    ("cust_035", "Postman Corp India Pvt Ltd"),
]

# Burning startup customers — smaller, more volatile
_STARTUP_CUSTOMERS: list[tuple[str, str]] = [
    ("cust_001", "Plum Benefits Pvt Ltd"),
    ("cust_002", "Jarvis AI Pvt Ltd"),
    ("cust_003", "Hyperverge Technologies Pvt Ltd"),
    ("cust_004", "Soulpage IT Solutions Pvt Ltd"),
    ("cust_005", "Vernacular AI Labs Pvt Ltd"),
    ("cust_006", "Pepper Content Pvt Ltd"),
    ("cust_007", "CureFit Health Pvt Ltd"),
    ("cust_008", "Bounce Bikes Pvt Ltd"),
    ("cust_009", "OTO Capital Pvt Ltd"),
    ("cust_010", "InCred Financial Services Ltd"),
]

# Professional services customers — large enterprise
_SERVICES_CUSTOMERS: list[tuple[str, str]] = [
    ("cust_001", "ONGC Petro Additions Ltd"),
    ("cust_002", "Steel Authority of India Ltd"),
    ("cust_003", "Power Grid Corporation Ltd"),
    ("cust_004", "NTPC Ltd"),
    ("cust_005", "Hindustan Aeronautics Ltd"),
    ("cust_006", "Bharat Electronics Ltd"),
    ("cust_007", "Indian Oil Corporation Ltd"),
    ("cust_008", "GAIL India Ltd"),
    ("cust_009", "Coal India Ltd"),
    ("cust_010", "Bharat Heavy Electricals Ltd"),
    ("cust_011", "National Thermal Power Corp"),
    ("cust_012", "Airports Authority of India"),
    ("cust_013", "BEML Ltd"),
    ("cust_014", "Mazagon Dock Shipbuilders Ltd"),
    ("cust_015", "Rail Vikas Nigam Ltd"),
]

# Food & beverage vendors
_FOOD_VENDORS = [
    "ITC Agri Business Ltd",
    "Mother Dairy Fruits & Vegetables Pvt Ltd",
    "Amul Dairy Gujarat Cooperative",
    "Nestlé India Pvt Ltd",
    "Britannia Industries Ltd",
    "Parle Products Pvt Ltd",
    "Haldiram Foods International Pvt Ltd",
    "McCain Foods India Pvt Ltd",
    "Del Monte Foods India Pvt Ltd",
    "Keya Foods International Pvt Ltd",
]

# SaaS / cloud / software vendors (realistic B2B toolstack)
_TECH_VENDORS = [
    ("Amazon Web Services India Pvt Ltd", 48000),
    ("Microsoft Azure India Pvt Ltd", 35000),
    ("Google Cloud India Pvt Ltd", 28000),
    ("Atlassian Network Services Pvt Ltd", 12000),
    ("GitHub Inc", 8000),
    ("Figma Inc", 6000),
    ("Notion Labs Inc", 3500),
    ("Freshworks Inc", 18000),
    ("Zoho Corp Pvt Ltd", 14000),
    ("Clevertap India Pvt Ltd", 22000),
    ("Sendgrid Twilio India Pvt Ltd", 9000),
    ("Datadog India Pvt Ltd", 16000),
    ("Cloudflare India Pvt Ltd", 7000),
    ("Postman Corp India Pvt Ltd", 5000),
    ("Mixpanel India Pvt Ltd", 11000),
]

# Office / operations vendors
_OFFICE_VENDORS = [
    ("Smartworks Coworking Spaces Pvt Ltd", 150000),
    ("WeWork India Management Pvt Ltd", 180000),
    ("Awfis Space Solutions Ltd", 120000),
    ("Regus Management Group Pvt Ltd", 160000),
]

# Logistics / delivery
_LOGISTICS_VENDORS = [
    "Delhivery Ltd",
    "Blue Dart Express Ltd",
    "Ekart Logistics Pvt Ltd",
    "Xpressbees Logistics Pvt Ltd",
    "DTDC Express Ltd",
    "Ecom Express Ltd",
    "Shadowfax Technologies Pvt Ltd",
]

# ─────────────────────────────────────────────────────────────────────────────
# Probabilistic risk flag profiles (realistic real-world incidence rates)
# ─────────────────────────────────────────────────────────────────────────────

_REALISTIC_FLAG_PROBS: dict[str, dict[str, float]] = {
    "healthy_saas": {
        # Operational risk flags
        "revenue_concentration": 0.25,
        "customer_churn":        0.20,
        "related_party_leakage": 0.12,
        "founder_extraction":    0.08,
        "round_tripping":        0.03,
        "structuring":           0.02,
        # Compliance fault flags — low incidence for a well-run SaaS
        "compliance_269st":             0.04,
        "compliance_gst_gap":           0.05,
        "compliance_tds_gap":           0.05,
        "compliance_pmla_cash":         0.02,
        "compliance_rpt_concentration": 0.07,
        "compliance_cash_loan":         0.03,
    },
    "burning_startup": {
        # Operational risk flags
        "customer_churn":        0.50,
        "founder_extraction":    0.35,
        "revenue_concentration": 0.20,
        "related_party_leakage": 0.22,
        "round_tripping":        0.08,
        "structuring":           0.05,
        # Compliance fault flags — elevated; startups often cut corners under cash pressure
        "compliance_gst_gap":           0.20,
        "compliance_tds_gap":           0.18,
        "compliance_rpt_concentration": 0.15,
        "compliance_cash_loan":         0.10,
        "compliance_269st":             0.05,
        "compliance_pmla_cash":         0.05,
    },
    "services_firm": {
        # Operational risk flags
        "revenue_concentration": 0.55,  # B2G is inherently concentrated
        "customer_churn":        0.18,
        "related_party_leakage": 0.18,
        "founder_extraction":    0.12,
        "round_tripping":        0.04,
        "structuring":           0.03,
        # Compliance fault flags — informal financing and RPT common in services
        "compliance_rpt_concentration": 0.15,
        "compliance_cash_loan":         0.08,
        "compliance_gst_gap":           0.08,
        "compliance_tds_gap":           0.06,
        "compliance_269st":             0.04,
        "compliance_pmla_cash":         0.04,
    },
    "ecommerce": {
        # Operational risk flags
        "structuring":           0.12,  # cash-intensive, higher risk
        "revenue_concentration": 0.08,  # Razorpay aggregates all
        "founder_extraction":    0.18,
        "round_tripping":        0.06,
        "customer_churn":        0.05,
        "related_party_leakage": 0.15,
        # Compliance fault flags — cash COD float creates cash-limit and PMLA risk
        "compliance_pmla_cash":         0.15,
        "compliance_269st":             0.10,
        "compliance_gst_gap":           0.10,
        "compliance_rpt_concentration": 0.12,
        "compliance_tds_gap":           0.08,
        "compliance_cash_loan":         0.06,
    },
    "restaurant": {
        # Operational risk flags
        "structuring":           0.25,  # cash-heavy business
        "related_party_leakage": 0.20,
        "founder_extraction":    0.22,
        "round_tripping":        0.05,
        "customer_churn":        0.03,
        "revenue_concentration": 0.04,
        # Compliance fault flags — highest incidence; cash-dominant, informal operations
        "compliance_pmla_cash":         0.30,
        "compliance_269st":             0.25,
        "compliance_gst_gap":           0.18,
        "compliance_rpt_concentration": 0.20,
        "compliance_tds_gap":           0.15,
        "compliance_cash_loan":         0.10,
    },
}


def auto_flags_for_profile(profile: str) -> list[str]:
    """Randomly select risk flags based on realistic per-profile probabilities."""
    probs = _REALISTIC_FLAG_PROBS.get(profile, {})
    return [flag for flag, prob in probs.items() if random.random() < prob]


# ─────────────────────────────────────────────────────────────────────────────
# Internal data types
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _Txn:
    txn_id: str
    date: date
    description: str
    ref_no: str
    value_date: date
    debit: Decimal | None
    credit: Decimal | None
    balance: Decimal = field(default=Decimal("0"))
    category: str = "OTHER"
    customer_id: str | None = None
    is_related_party: bool = False
    related_party_match: str | None = None
    anomaly_flags: list[str] = field(default_factory=list)


@dataclass
class _Company:
    name: str
    account_no: str
    branch: str
    ifsc: str
    founder_name: str
    founder_surname: str
    opening_balance: Decimal


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inr(amount: Decimal | None) -> str:
    if amount is None:
        return ""
    integer_part, _, decimal_part = f"{abs(amount):.2f}".partition(".")
    if len(integer_part) <= 3:
        return f"{integer_part}.{decimal_part}"
    last3 = integer_part[-3:]
    rest = integer_part[:-3]
    groups: list[str] = []
    while len(rest) > 2:
        groups.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        groups.append(rest)
    groups.reverse()
    return ",".join(groups) + "," + last3 + "." + decimal_part


def _inr_signed(amount: Decimal | None) -> str:
    """Same digit-grouping as _inr(), but preserves a negative sign.

    Used only for the running/closing balance column, which can legitimately
    go negative during an overdraft period. _inr() abs()es its input (correct
    for debit/credit amounts, which are never negative by construction) — using
    it for balance would silently print a negative balance as positive, making
    the rendered PDF's own numbers internally inconsistent (the running-balance
    arithmetic the Validator checks would then never add up).
    """
    if amount is None:
        return ""
    return ("-" if amount < 0 else "") + _inr(amount)


def _bcode() -> str:
    return random.choice(_BANK_CODES) + str(random.randint(1_000_000, 9_999_999))


def _ref() -> str:
    return str(random.randint(100_000_000_000, 999_999_999_999))


def _hex8() -> str:
    return uuid.uuid4().hex[:8].upper()


def _rand_day(year: int, month: int, lo: int = 1, hi: int = 28) -> date:
    return date(year, month, random.randint(lo, hi))


# ─────────────────────────────────────────────────────────────────────────────
# Narration builders — bank-specific
# ─────────────────────────────────────────────────────────────────────────────

def _narration(bank: str, mode: str, party: str, inv: int = 0) -> str:
    if bank == "hdfc":
        if mode == "credit":
            return f"NEFT/{_bcode()}/{party.upper()}/INV{inv:06d}"
        if mode == "debit":
            return f"NEFT/{_bcode()}/{party.upper()}/{_hex8()}"
        if mode == "salary":
            return f"ECS/SALARY/{party.upper()}/{_hex8()}"
        if mode == "cash":
            return f"CASH DEP-{party.upper()}"
        if mode == "gst":
            return f"NEFT/{_bcode()}/GST PAYMENT/GSTIN{random.randint(10**14, 10**15-1)}"
        if mode == "tds":
            return f"NEFT/{_bcode()}/TDS PAYMENT/TAN{random.randint(10**8, 10**9-1)}"
        if mode == "advance_tax":
            return f"NEFT/{_bcode()}/INCOME TAX PAYMENT/BSR{random.randint(10**6,10**7-1)}"
        if mode == "loan":
            return f"NEFT/{_bcode()}/{party.upper()}/LOAN{random.randint(1000,9999)}"
        if mode == "cash_loan_in":
            return f"CASH DEP-LOAN RECEIVED/{party.upper()}"
        if mode == "cash_loan_out":
            return f"CASH WITH-LOAN REPAID/{party.upper()}"
        if mode == "founder":
            return f"NEFT/{_bcode()}/{party.upper()} PERSONAL/{_hex8()}"
        if mode == "razorpay":
            return f"NEFT/{_bcode()}/RAZORPAY SOFTWARE PVT LTD/SETL{inv:06d}"
        if mode == "vendor":
            return f"NEFT/{_bcode()}/{party.upper()}/{_hex8()}"
    else:  # icici
        if mode == "credit":
            return f"NEFT CR-{_bcode()}-{party.upper()}-INV{inv:06d}"
        if mode == "debit":
            return f"NEFT DR-{_bcode()}-{party.upper()}"
        if mode == "salary":
            return f"ECS DR-SALARY-{party.upper()}-{_hex8()}"
        if mode == "cash":
            return f"CASH DEPOSIT-{party.upper()}-{_hex8()}"
        if mode == "gst":
            return f"NEFT DR-CBSB{random.randint(10000,99999)}-GST PMT-{random.randint(10**14, 10**15-1)}"
        if mode == "tds":
            return f"NEFT DR-CBSB{random.randint(10000,99999)}-TDS PAYMENT-TAN{random.randint(10**8,10**9-1)}"
        if mode == "advance_tax":
            return f"NEFT DR-CBSB{random.randint(10000,99999)}-INCOME TAX PAYMENT-BSR{random.randint(10**6,10**7-1)}"
        if mode == "loan":
            return f"NEFT CR-{_bcode()}-{party.upper()}-LOAN{random.randint(1000,9999)}"
        if mode == "cash_loan_in":
            return f"CASH DEPOSIT-LOAN RECEIPT/{party.upper()}-{_hex8()}"
        if mode == "cash_loan_out":
            return f"CASH WITHDRAWAL-LOAN REPAID/{party.upper()}-{_hex8()}"
        if mode == "founder":
            return f"NEFT DR-{_bcode()}-{party.upper()} PERSONAL"
        if mode == "razorpay":
            return f"NEFT CR-{_bcode()}-RAZORPAY SOFTWARE PVT LTD-SETL{inv:06d}"
        if mode == "vendor":
            return f"NEFT DR-{_bcode()}-{party.upper()}"
    return f"TXN/{party.upper()}/{_hex8()}"


def _make_txn(
    txn_date: date,
    desc: str,
    *,
    debit: Decimal | None = None,
    credit: Decimal | None = None,
    category: str = "OTHER",
    customer_id: str | None = None,
    is_related_party: bool = False,
    related_party_match: str | None = None,
    anomaly_flags: list[str] | None = None,
) -> _Txn:
    return _Txn(
        txn_id=f"txn_{uuid.uuid4().hex[:10]}",
        date=txn_date,
        description=desc,
        ref_no=_ref(),
        value_date=txn_date,
        debit=debit,
        credit=credit,
        category=category,
        customer_id=customer_id,
        is_related_party=is_related_party,
        related_party_match=related_party_match,
        anomaly_flags=anomaly_flags or [],
    )


# ─────────────────────────────────────────────────────────────────────────────
# Company configurations per profile
# ─────────────────────────────────────────────────────────────────────────────

_COMPANY_CONFIGS: dict[str, _Company] = {
    "healthy_saas": _Company(
        name="TechFlow Solutions Pvt Ltd",
        account_no="50100" + str(random.randint(100000000, 999999999)),
        branch="Bengaluru Koramangala",
        ifsc="HDFC0001234",
        founder_name="Rahul",
        founder_surname="Sharma",
        opening_balance=Decimal("1500000"),
    ),
    "burning_startup": _Company(
        name="Velocify Digital Pvt Ltd",
        account_no="50100" + str(random.randint(100000000, 999999999)),
        branch="Mumbai BKC",
        ifsc="ICIC0005678",
        founder_name="Priya",
        founder_surname="Gupta",
        opening_balance=Decimal("5000000"),
    ),
    "services_firm": _Company(
        name="Nexus Consulting Services LLP",
        account_no="50100" + str(random.randint(100000000, 999999999)),
        branch="Delhi Connaught Place",
        ifsc="HDFC0002345",
        founder_name="Amit",
        founder_surname="Patel",
        opening_balance=Decimal("2000000"),
    ),
    "ecommerce": _Company(
        name="Kartify Commerce Pvt Ltd",
        account_no="50100" + str(random.randint(100000000, 999999999)),
        branch="Gurugram Cyber City",
        ifsc="ICIC0006789",
        founder_name="Sunita",
        founder_surname="Kumar",
        opening_balance=Decimal("2500000"),
    ),
    "restaurant": _Company(
        name="Masala Garden Foods Pvt Ltd",
        account_no="50100" + str(random.randint(100000000, 999999999)),
        branch="Pune Koregaon Park",
        ifsc="HDFC0003456",
        founder_name="Rajesh",
        founder_surname="Singh",
        opening_balance=Decimal("800000"),
    ),
}


# ─────────────────────────────────────────────────────────────────────────────
# Month helpers
# ─────────────────────────────────────────────────────────────────────────────

def _months(start: date, n: int) -> list[tuple[int, int]]:
    result = []
    y, m = start.year, start.month
    for _ in range(n):
        result.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def _seasonal(month: int) -> Decimal:
    return Decimal(str(_SEASONAL.get(month, 1.0)))


def _april_hike(base: Decimal, month: int, month_idx: int) -> Decimal:
    """Apply salary hike in April (Indian FY start)."""
    if month == 4 and month_idx > 0:
        return (base * Decimal("1.10")).quantize(Decimal("1"))
    return base


def _delayed_date(yr: int, mo: int, delay_days: int) -> date:
    """Push a payment by delay_days — may spill into next month."""
    d = _rand_day(yr, mo, 5, 20) + timedelta(days=delay_days)
    return d


# ─────────────────────────────────────────────────────────────────────────────
# Transaction generators — one per profile
# ─────────────────────────────────────────────────────────────────────────────

def _gen_healthy_saas(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    inv = 1000
    base_amounts = {cid: Decimal(str(random.randint(25, 70) * 1000)) for cid, _ in _SAAS_CUSTOMERS}

    employees = [
        ("Arun Kumar",     Decimal("85000")),
        ("Meena Nair",     Decimal("95000")),
        ("Suresh Iyer",    Decimal("130000")),
        ("Deepa Reddy",    Decimal("90000")),
        ("Vikram Joshi",   Decimal("115000")),
        ("Pooja Menon",    Decimal("80000")),
        ("Sanjay Khanna",  Decimal("110000")),
    ]

    # Pick a random tech vendor stack (5-7 vendors)
    selected_tech = random.sample(_TECH_VENDORS, random.randint(5, 7))
    office_vendor = random.choice(_OFFICE_VENDORS)

    # Track which month employees got a hike
    salary_base = {emp: sal for emp, sal in employees}

    for month_idx, (yr, mo) in enumerate(_months(start, n_months)):
        season = _seasonal(mo)
        active_count = min(28 + month_idx * 2, len(_SAAS_CUSTOMERS))
        active_customers = _SAAS_CUSTOMERS[:active_count]

        # Revenue — with 15% chance of payment delay (spills to this month from prev delayed)
        for cid, cname in active_customers:
            amt = (base_amounts[cid] * Decimal(str(1 + 0.025 * month_idx)) * season).quantize(Decimal("1"))
            # 15% probability of delay (simulates net-30/45 payment terms)
            delay = random.randint(15, 40) if random.random() < 0.15 else 0
            pay_date = _rand_day(yr, mo, 5, 25)
            if delay:
                pay_date = pay_date + timedelta(days=delay)
                # If spills past month end, keep it within this month
                if pay_date.month != mo:
                    pay_date = date(yr, mo, 28)
            txns.append(_make_txn(
                pay_date, _narration(bank, "credit", cname, inv),
                credit=amt, category="REVENUE", customer_id=cid,
            ))
            inv += 1

        # Salaries — with April hike
        for emp, base_sal in list(salary_base.items()):
            sal = _april_hike(base_sal, mo, month_idx)
            salary_base[emp] = sal
            txns.append(_make_txn(
                date(yr, mo, random.choice([1, 2, 3, 5])),
                _narration(bank, "salary", emp),
                debit=sal, category="SALARY",
            ))

        # Tech vendors (subscription billing — consistent day, slight growth)
        for vname, vbase in selected_tech:
            price_growth = Decimal(str(1 + 0.004 * month_idx))  # ~5% annual price increase
            amt = (Decimal(str(vbase)) * price_growth).quantize(Decimal("100"))
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 8),
                _narration(bank, "vendor", vname),
                debit=amt, category="VENDOR_PAYMENT",
            ))

        # Office rent
        txns.append(_make_txn(
            date(yr, mo, 1),
            _narration(bank, "vendor", office_vendor[0]),
            debit=Decimal(str(office_vendor[1])), category="VENDOR_PAYMENT",
        ))

        # GST on revenue
        rev_estimate = sum(base_amounts[c] for c, _ in active_customers[:20])
        gst = (rev_estimate * _seasonal(mo) * Decimal("0.18")).quantize(Decimal("1"))
        txns.append(_make_txn(
            date(yr, mo, 20) if mo != 3 else date(yr, mo, 28),  # March GST on 28th
            _narration(bank, "gst", ""),
            debit=gst, category="TAX",
        ))

        # TDS payment (7th of every month for prior month's TDS)
        if month_idx > 0:
            tds = (rev_estimate * Decimal("0.02")).quantize(Decimal("1"))
            txns.append(_make_txn(
                date(yr, mo, 7),
                _narration(bank, "tds", ""),
                debit=tds, category="TAX",
            ))

        # Advance tax (June, Sep, Dec, Mar installments)
        if mo in (6, 9, 12, 3):
            adv_tax = Decimal(str(random.randint(50, 150) * 1000))
            txns.append(_make_txn(
                date(yr, mo, 15),
                _narration(bank, "advance_tax", ""),
                debit=adv_tax, category="TAX",
            ))

    return txns


def _gen_burning_startup(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    inv = 2000
    base_amounts = {cid: Decimal(str(random.randint(50, 100) * 1000)) for cid, _ in _STARTUP_CUSTOMERS}

    employees = [
        ("Ravi Shankar",    Decimal("130000")),
        ("Ananya Bose",     Decimal("150000")),
        ("Kiran Rao",       Decimal("140000")),
        ("Pooja Menon",     Decimal("120000")),
        ("Sanjay Desai",    Decimal("170000")),
        ("Neha Verma",      Decimal("155000")),
        ("Tarun Khanna",    Decimal("180000")),
        ("Shruti Agarwal",  Decimal("160000")),
        ("Nikhil Batra",    Decimal("145000")),
    ]
    salary_base = {emp: sal for emp, sal in employees}

    for month_idx, (yr, mo) in enumerate(_months(start, n_months)):
        # Declining customer base (startup losing ground)
        active_count = max(len(_STARTUP_CUSTOMERS) - month_idx, 5)
        active_customers = _STARTUP_CUSTOMERS[:active_count]

        for cid, cname in active_customers:
            pay_date = _rand_day(yr, mo, 5, 25)
            txns.append(_make_txn(
                pay_date, _narration(bank, "credit", cname, inv),
                credit=base_amounts[cid], category="REVENUE", customer_id=cid,
            ))
            inv += 1

        # Rapid hiring — new employee joins every 2 months
        if month_idx % 2 == 0 and month_idx > 0:
            new_name = f"New Hire {month_idx // 2}"
            new_sal = Decimal(str(random.randint(100, 200) * 1000))
            employees.append((new_name, new_sal))
            salary_base[new_name] = new_sal

        for emp, base_sal in list(salary_base.items()):
            sal = _april_hike(base_sal, mo, month_idx)
            sal += Decimal(str(month_idx * 3000))  # incremental hike each month (aggressive hiring)
            salary_base[emp] = sal
            txns.append(_make_txn(
                date(yr, mo, random.choice([1, 2, 3, 5])),
                _narration(bank, "salary", emp),
                debit=sal, category="SALARY",
            ))

        # Heavy vendor burn
        heavy_vendors = [
            ("Amazon Web Services India Pvt Ltd", Decimal(str(random.randint(150, 350) * 1000))),
            ("Google Cloud India Pvt Ltd", Decimal(str(random.randint(100, 280) * 1000))),
            ("Smartworks Coworking Spaces Pvt Ltd", Decimal("280000")),
            ("Performance Marketing Agency India Ltd", Decimal(str(random.randint(250, 600) * 1000))),
            ("Khaitan & Co Legal Advisory", Decimal(str(random.randint(60, 180) * 1000))),
        ]
        for vname, vamt in heavy_vendors:
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 15),
                _narration(bank, "vendor", vname),
                debit=vamt, category="VENDOR_PAYMENT",
            ))

        # GST
        rev = sum(base_amounts[c] for c, _ in active_customers)
        txns.append(_make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "gst", ""),
            debit=(rev * Decimal("0.18")).quantize(Decimal("1")),
            category="TAX",
        ))

    return txns


def _gen_services_firm(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    inv = 3000
    base_amounts = {
        cid: Decimal(str(random.randint(150, 1200) * 10000))
        for cid, _ in _SERVICES_CUSTOMERS
    }
    employees = [
        ("Kavita Sharma",   Decimal("155000")),
        ("Manish Tiwari",   Decimal("175000")),
        ("Ruchi Agarwal",   Decimal("145000")),
        ("Abhishek Singh",  Decimal("190000")),
        ("Prateek Bahl",    Decimal("165000")),
    ]
    salary_base = {emp: sal for emp, sal in employees}

    for (yr, mo) in _months(start, n_months):
        # Lumpy payments — 4-10 clients pay, each with possible 30-day delay
        paying = random.sample(_SERVICES_CUSTOMERS, random.randint(4, 10))
        for cid, cname in paying:
            delay = random.randint(10, 35) if random.random() < 0.30 else 0  # 30% of clients pay late
            pay_date = _rand_day(yr, mo, 5, 28)
            if delay:
                pay_date += timedelta(days=delay)
                if pay_date.month != mo:
                    pay_date = date(yr, mo, 28)
            txns.append(_make_txn(
                pay_date, _narration(bank, "credit", cname, inv),
                credit=base_amounts[cid], category="REVENUE", customer_id=cid,
            ))
            inv += 1

        for emp, base_sal in list(salary_base.items()):
            sal = _april_hike(base_sal, mo, sum(1 for m in _months(date.today(), 12) if m == (yr, mo)))
            salary_base[emp] = sal
            txns.append(_make_txn(
                date(yr, mo, 2),
                _narration(bank, "salary", emp),
                debit=sal, category="SALARY",
            ))

        txns.append(_make_txn(
            date(yr, mo, 1),
            _narration(bank, "vendor", "Awfis Space Solutions Ltd"),
            debit=Decimal("200000"), category="VENDOR_PAYMENT",
        ))
        txns.append(_make_txn(
            _rand_day(yr, mo, 5, 20),
            _narration(bank, "vendor", "Travel Connections India Ltd"),
            debit=Decimal(str(random.randint(40, 140) * 1000)), category="VENDOR_PAYMENT",
        ))

        # GST on billed amount
        billed = sum(base_amounts[cid] for cid, _ in paying)
        txns.append(_make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "gst", ""),
            debit=(billed * Decimal("0.18")).quantize(Decimal("1")),
            category="TAX",
        ))

    return txns


def _gen_ecommerce(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    setl = 5000

    for (yr, mo) in _months(start, n_months):
        season = _seasonal(mo)
        d = date(yr, mo, 1)
        while d.month == mo:
            payout = (Decimal(str(random.randint(80, 300) * 1000)) * season).quantize(Decimal("1000"))
            txns.append(_make_txn(
                d, _narration(bank, "razorpay", "", setl),
                credit=payout, category="REVENUE", customer_id="cust_razorpay_aggregated",
            ))
            setl += 1
            d += timedelta(days=random.randint(2, 3))

        # Inventory vendors with seasonal scaling
        for vendor, base_amt in [
            ("Flipkart Commerce Solutions Pvt Ltd", Decimal(str(random.randint(600, 2500) * 1000))),
            ("Amazon Seller Services Pvt Ltd",       Decimal(str(random.randint(400, 1800) * 1000))),
            ("Myntra Designs Pvt Ltd",               Decimal(str(random.randint(250, 900) * 1000))),
        ]:
            scaled = (base_amt * season).quantize(Decimal("1000"))
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 10),
                _narration(bank, "vendor", vendor),
                debit=scaled, category="VENDOR_PAYMENT",
            ))

        # Logistics — seasonal
        logistics_vendor = random.choice(_LOGISTICS_VENDORS)
        logistic_cost = (Decimal(str(random.randint(150, 500) * 1000)) * season).quantize(Decimal("1000"))
        txns.append(_make_txn(
            _rand_day(yr, mo, 5, 25),
            _narration(bank, "vendor", logistics_vendor),
            debit=logistic_cost, category="VENDOR_PAYMENT",
        ))

        # Salaries
        for emp, sal in [
            ("Logistics Head",   Decimal("135000")),
            ("Tech Lead",        Decimal("185000")),
            ("Ops Manager",      Decimal("125000")),
            ("Data Analyst",     Decimal("98000")),
            ("Customer Success", Decimal("78000")),
        ]:
            txns.append(_make_txn(
                date(yr, mo, 1),
                _narration(bank, "salary", emp),
                debit=sal, category="SALARY",
            ))

        # Platform fees (Razorpay ~1.5% of settlements)
        txns.append(_make_txn(
            _rand_day(yr, mo, 5, 10),
            _narration(bank, "vendor", "Razorpay Software Pvt Ltd Fee Deduction"),
            debit=Decimal(str(random.randint(20, 55) * 1000)), category="FEES",
        ))

        # GST
        gst_base = (Decimal(str(random.randint(50, 150) * 1000)) * season).quantize(Decimal("1"))
        txns.append(_make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "gst", ""),
            debit=(gst_base * Decimal("0.18")).quantize(Decimal("1")),
            category="TAX",
        ))

    return txns


def _gen_restaurant(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []

    for (yr, mo) in _months(start, n_months):
        season = _seasonal(mo)
        d = date(yr, mo, 1)
        while d.month == mo:
            daily_amt = (Decimal(str(random.randint(25, 100) * 1000)) * season).quantize(Decimal("100"))
            if random.random() < 0.45:
                desc = _narration(bank, "cash", "Koregaon Park Branch")
            elif random.random() < 0.6:
                desc = f"UPI/CR/{random.randint(9000000000, 9999999999)}/SWIGGY PAYOUT-{_hex8()}"
            else:
                desc = f"UPI/CR/{random.randint(9000000000, 9999999999)}/ZOMATO PAYMENTS-{_hex8()}"
            txns.append(_make_txn(
                d, desc, credit=daily_amt, category="REVENUE", customer_id="cust_daily_sales",
            ))
            d += timedelta(days=1)

        # Food vendors — 4-5 per month
        for vendor in random.sample(_FOOD_VENDORS, random.randint(4, 5)):
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 28),
                _narration(bank, "vendor", vendor),
                debit=Decimal(str(random.randint(80, 350) * 1000)), category="VENDOR_PAYMENT",
            ))

        # Staff — 15 staff
        for i in range(1, 16):
            txns.append(_make_txn(
                date(yr, mo, 5),
                _narration(bank, "salary", f"Staff Member {i:02d}"),
                debit=Decimal(str(random.randint(20, 38) * 1000)), category="SALARY",
            ))

        # Rent
        txns.append(_make_txn(
            date(yr, mo, 1),
            _narration(bank, "vendor", "Koregaon Park Property Trust"),
            debit=Decimal("180000"), category="VENDOR_PAYMENT",
        ))

        # Utilities
        txns.append(_make_txn(
            _rand_day(yr, mo, 10, 20),
            _narration(bank, "vendor", "MSEDCL Electricity Board"),
            debit=Decimal(str(random.randint(18, 55) * 1000)), category="VENDOR_PAYMENT",
        ))

        # GST (restaurant: 5% on food, 18% on packaged)
        monthly_rev = (Decimal(str(random.randint(70, 250) * 10000)) * season).quantize(Decimal("1"))
        txns.append(_make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "gst", ""),
            debit=(monthly_rev * Decimal("0.05")).quantize(Decimal("1")),
            category="TAX",
        ))

    return txns


# ─────────────────────────────────────────────────────────────────────────────
# Red flag injectors
# ─────────────────────────────────────────────────────────────────────────────

# Regex helpers shared by compliance-fault injectors
_CASH_NARR_RE = re.compile(r"CASH\s*(DEP|WITH|DEPOSIT|WITHDRAWAL)", re.I)
_GST_STRIP_RE  = re.compile(r"GST\s*(PAYMENT|PMT)|GSTIN", re.I)
_TDS_STRIP_RE  = re.compile(r"TDS\s*PAYMENT", re.I)


def _inject_structuring(txns, company, bank, start):
    new_txns = []
    for i in range(5):
        amt = Decimal(str(random.randint(158000, 198000)))
        d = start + timedelta(days=random.randint(0, 6))
        t = _make_txn(d, _narration(bank, "cash", "Main Branch"), credit=amt,
                      category="REVENUE", anomaly_flags=["structuring"])
        new_txns.append(t)
    flag = {
        "flag_type": "structuring",
        "description": "5 cash deposits between ₹1.58L and ₹1.98L within a 7-day window",
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
    }
    return txns + new_txns, flag


def _inject_round_tripping(txns, company, bank, start):
    amt = Decimal(str(random.randint(500, 1500) * 1000))
    party = "Zenith Capital Advisory LLP"
    out_date = start + timedelta(days=random.randint(5, 20))
    in_date = out_date + timedelta(days=random.randint(3, 7))
    out_txn = _make_txn(out_date, _narration(bank, "debit", party),
                         debit=amt, category="TRANSFER", anomaly_flags=["round_tripping"])
    in_txn  = _make_txn(in_date, _narration(bank, "credit", party, random.randint(1, 9999)),
                         credit=amt * Decimal("0.98"), category="TRANSFER", anomaly_flags=["round_tripping"])
    flag = {
        "flag_type": "round_tripping",
        "description": f"Outflow of {_inr(amt)} to {party}, matching inflow 3-7 days later",
        "triggering_transaction_ids": [out_txn.txn_id, in_txn.txn_id],
    }
    return txns + [out_txn, in_txn], flag


def _inject_founder_extraction(txns, company, bank, start):
    declared_salary = Decimal("150000")
    new_txns = []
    for yr, mo in _months(start, 3):
        amt = declared_salary * Decimal("3.5")
        t = _make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "founder", f"{company.founder_name} {company.founder_surname}"),
            debit=amt, category="FOUNDER_WITHDRAWAL", anomaly_flags=["founder_over_extraction"],
        )
        new_txns.append(t)
    flag = {
        "flag_type": "founder_extraction",
        "description": (
            f"Monthly founder withdrawals of {_inr(declared_salary * Decimal('3.5'))} "
            f"exceed declared salary of {_inr(declared_salary)} by 3.5×"
        ),
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "declared_founder_salary": str(declared_salary),
    }
    return txns + new_txns, flag


def _inject_customer_churn(txns, company, bank, start):
    mths = _months(start, 3)
    m1_yr, m1_mo = mths[0]
    m1_customers: set[str] = set()
    for t in txns:
        if t.category == "REVENUE" and t.customer_id and t.date.year == m1_yr and t.date.month == m1_mo:
            m1_customers.add(t.customer_id)
    churn_count = max(1, int(len(m1_customers) * 0.25))
    churned = set(random.sample(sorted(m1_customers), min(churn_count, len(m1_customers))))
    filtered = [
        t for t in txns
        if not (t.customer_id in churned and t.category == "REVENUE"
                and (t.date.year, t.date.month) in mths[1:])
    ]
    flag = {
        "flag_type": "customer_churn",
        "description": f"{churn_count} customers ({churn_count/max(len(m1_customers),1)*100:.0f}%) stopped paying after month 1",
        "triggering_transaction_ids": [],
        "churned_customer_ids": sorted(churned),
    }
    return filtered, flag


def _inject_revenue_concentration(txns, company, bank, start):
    new_txns = []
    inv = 9000
    whale = ("cust_whale", "Hyperion Mega Corp Ltd")
    for yr, mo in _months(start, 3):
        month_rev = sum(
            t.credit for t in txns
            if t.credit and t.category == "REVENUE" and t.date.year == yr and t.date.month == mo
        ) or Decimal("0")
        whale_amt = (month_rev * Decimal("3")).quantize(Decimal("1000"))
        t = _make_txn(
            _rand_day(yr, mo, 10, 20), _narration(bank, "credit", whale[1], inv),
            credit=whale_amt, category="REVENUE", customer_id=whale[0],
            anomaly_flags=["revenue_concentration"],
        )
        new_txns.append(t)
        inv += 1
    flag = {
        "flag_type": "revenue_concentration",
        "description": "Single customer 'Hyperion Mega Corp Ltd' contributes >70% of monthly revenue",
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "concentrated_customer_id": whale[0],
    }
    return txns + new_txns, flag


def _inject_related_party_leakage(txns, company, bank, start):
    related_entity = f"{company.founder_surname} Family Ventures Pvt Ltd"
    new_txns = []
    for yr, mo in _months(start, 3):
        amt = Decimal(str(random.randint(100, 350) * 1000))
        t = _make_txn(
            _rand_day(yr, mo, 10, 25), _narration(bank, "debit", related_entity),
            debit=amt, category="VENDOR_PAYMENT",
            is_related_party=True, related_party_match=related_entity,
            anomaly_flags=["related_party_leakage"],
        )
        new_txns.append(t)
    flag = {
        "flag_type": "related_party_leakage",
        "description": f"Monthly payments to '{related_entity}' — same surname as founder",
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "related_entity": related_entity,
        "founder_surname": company.founder_surname,
    }
    return txns + new_txns, flag


# ─────────────────────────────────────────────────────────────────────────────
# Compliance fault injectors — one per Indian compliance rule
# ─────────────────────────────────────────────────────────────────────────────

def _inject_compliance_269st(txns, company, bank, start):
    """Single cash receipt >= ₹2L from one party — triggers §269ST cash limit rule."""
    party = "BULK MERCHANDISE TRADER"
    amt = Decimal(str(random.randint(210000, 500000)))
    d = start + timedelta(days=random.randint(5, 25))
    t = _make_txn(
        d, f"CASH DEP-{party}" if bank == "hdfc" else f"CASH DEPOSIT-{party}-{_hex8()}",
        credit=amt, category="REVENUE", anomaly_flags=["compliance_269st"],
    )
    flag = {
        "flag_type": "compliance_269st",
        "description": f"Cash receipt of ₹{amt:,.0f} from '{party}' on {d} — violates §269ST limit of ₹2,00,000",
        "triggering_transaction_ids": [t.txn_id],
    }
    return txns + [t], flag


def _inject_compliance_gst_gap(txns, company, bank, start):
    """Strip GST from 3 months and inject matching revenue — triggers GST consistency rule."""
    # Target months 2, 3, 4 from statement start (month 1 is left intact)
    target_months = {(yr, mo) for yr, mo in _months(start, 4)[1:]}

    # Strip any existing GST payments in those months
    stripped_ids = [
        t.txn_id for t in txns
        if _GST_STRIP_RE.search(t.description) and t.debit
        and (t.date.year, t.date.month) in target_months
    ]
    filtered = [
        t for t in txns
        if not (_GST_STRIP_RE.search(t.description) and t.debit
                and (t.date.year, t.date.month) in target_months)
    ]

    # Inject ₹5L–₹9L revenue for each target month with no GST companion.
    # This guarantees: per-month revenue > ₹3L floor AND helps cross the ₹40L annual gate.
    new_rev = []
    inv = 8800
    for yr, mo in sorted(target_months):
        amt = Decimal(str(random.randint(500000, 900000)))
        new_rev.append(_make_txn(
            _rand_day(yr, mo, 8, 22),
            _narration(bank, "credit", "Enterprise Software Sales", inv),
            credit=amt, category="REVENUE", anomaly_flags=["compliance_gst_gap"],
        ))
        inv += 1

    month_strs = [f"{yr}-{mo:02d}" for yr, mo in sorted(target_months)]
    flag = {
        "flag_type": "compliance_gst_gap",
        "description": f"GST payments absent for {month_strs} despite per-month revenue above ₹3L",
        "triggering_transaction_ids": stripped_ids,
        "stripped_months": month_strs,
    }
    return filtered + new_rev, flag


def _inject_compliance_tds_gap(txns, company, bank, start):
    """Remove all TDS payments — triggers TDS disbursement pattern rule (salary > ₹5L/year)."""
    stripped_ids = [t.txn_id for t in txns if _TDS_STRIP_RE.search(t.description) and t.debit]
    filtered = [t for t in txns if not (_TDS_STRIP_RE.search(t.description) and t.debit)]
    flag = {
        "flag_type": "compliance_tds_gap",
        "description": (
            f"{len(stripped_ids)} TDS payment(s) removed — salary disbursements remain, "
            "triggering TDS non-compliance detection"
        ),
        "triggering_transaction_ids": stripped_ids,
    }
    return filtered, flag


def _inject_compliance_pmla_cash(txns, company, bank, start):
    """Three cash deposits across a 24-day window totalling >= ₹10L — triggers PMLA CTR-style rule."""
    base_day = start + timedelta(days=random.randint(30, 60))
    amounts = [
        Decimal(str(random.randint(380000, 480000))),
        Decimal(str(random.randint(320000, 400000))),
        Decimal(str(random.randint(300000, 350000))),
    ]
    new_txns = []
    for i, (offset, amt) in enumerate(zip([0, 11, 23], amounts)):
        d = base_day + timedelta(days=offset)
        if bank == "hdfc":
            desc = f"CASH DEP-HIGH VALUE MERCHANT {i + 1}"
        else:
            desc = f"CASH DEPOSIT-HV MERCHANT {i + 1}-{_hex8()}"
        new_txns.append(_make_txn(
            d, desc, credit=amt, category="REVENUE", anomaly_flags=["compliance_pmla_cash"],
        ))
    total = sum(amounts)
    flag = {
        "flag_type": "compliance_pmla_cash",
        "description": (
            f"Cash deposits totalling ₹{total:,.0f} across a 23-day window — "
            "triggers PMLA CTR-style aggregate cash detection"
        ),
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "window_total": str(total),
    }
    return txns + new_txns, flag


def _inject_compliance_rpt_concentration(txns, company, bank, start):
    """Add a large related-party debit pushing RPT share above 20% — triggers concentration rule.

    Note: pipeline detection requires the related entity to be in the affiliates list.
    The ground truth JSON marks is_related_party=True for verification against truth data.
    """
    total_debits = sum(t.debit for t in txns if t.debit) or Decimal("1000000")
    # Add a debit equal to 30% of existing debits → final share ≈ 23% (0.30 / 1.30)
    rpt_amt = (total_debits * Decimal("0.30")).quantize(Decimal("1"))
    related_entity = f"{company.founder_surname} Capital Partners LLP"
    d = start + timedelta(days=random.randint(20, 45))
    t = _make_txn(
        d, _narration(bank, "debit", related_entity),
        debit=rpt_amt, category="VENDOR_PAYMENT",
        is_related_party=True, related_party_match=related_entity,
        anomaly_flags=["compliance_rpt_concentration"],
    )
    flag = {
        "flag_type": "compliance_rpt_concentration",
        "description": (
            f"₹{rpt_amt:,.0f} to '{related_entity}' pushes related-party outflow share above 20% "
            "(pipeline detection requires entity in affiliates list)"
        ),
        "triggering_transaction_ids": [t.txn_id],
        "related_entity": related_entity,
    }
    return txns + [t], flag


def _inject_compliance_cash_loan(txns, company, bank, start):
    """Cash loan receipt >= ₹20K — triggers §269SS cash loan prohibition."""
    lender = "Director Personal Account"
    amt = Decimal(str(random.randint(50000, 200000)))
    d = start + timedelta(days=random.randint(10, 30))
    t = _make_txn(
        d, _narration(bank, "cash_loan_in", lender),
        credit=amt, category="LOAN_IN", anomaly_flags=["compliance_cash_loan"],
    )
    flag = {
        "flag_type": "compliance_cash_loan",
        "description": (
            f"Cash loan receipt of ₹{amt:,.0f} from '{lender}' on {d} — "
            "violates §269SS (cash loans >= ₹20,000 are prohibited)"
        ),
        "triggering_transaction_ids": [t.txn_id],
    }
    return txns + [t], flag


def _inject_aggregator_dominated(txns, company, bank, start):
    """Add large Razorpay-settlement revenue so aggregator-settled revenue
    crosses the Wave 3.1 dominance threshold (30%) — exercises customer-id
    exclusion (pipeline/customer_identity.py) and the aggregator caveat
    surfaced on every lens's customer analytics output."""
    existing_revenue = sum(
        (t.credit for t in txns if t.category == "REVENUE" and t.credit), Decimal("0")
    ) or Decimal("300000")
    per_month = (existing_revenue * Decimal("2") / Decimal("3")).quantize(Decimal("1"))
    new_txns = []
    for yr, mo in _months(start, 3):
        d = _rand_day(yr, mo, 5, 25)
        t = _make_txn(
            d, _narration(bank, "razorpay", "", random.randint(100000, 999999)),
            credit=per_month, category="REVENUE", anomaly_flags=["aggregator_settlement"],
        )
        new_txns.append(t)
    flag = {
        "flag_type": "aggregator_dominated_revenue",
        "description": (
            f"Added ₹{per_month:,.0f}/month in Razorpay settlement revenue across 3 months, "
            "pushing aggregator-settled share well past the 30% dominance threshold."
        ),
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
    }
    return txns + new_txns, flag


def _inject_inter_account_transfers(txns, company, bank, start):
    """Add self-transfer transactions to/from another account the company
    holds — tests TRANSFER categorisation of self-transfer narrations and
    whether round-tripping detection can be fooled by same-owner transfers
    (a debit then a credit of the same amount within days, but not fraud)."""
    other_acct = f"XXXX{random.randint(1000, 9999)}"
    new_txns = []
    for yr, mo in _months(start, 2):
        amt = Decimal(str(random.randint(50, 200) * 1000))
        out_d = _rand_day(yr, mo, 5, 15)
        in_d = out_d + timedelta(days=random.randint(1, 3))
        if bank == "hdfc":
            out_desc = f"NEFT/{_bcode()}/OWN ACCOUNT TRANSFER {other_acct}/{_hex8()}"
            in_desc  = f"NEFT/{_bcode()}/OWN ACCOUNT TRANSFER {other_acct}/{_hex8()}"
        else:
            out_desc = f"NEFT DR-{_bcode()}-SELF TRANSFER {other_acct}"
            in_desc  = f"NEFT CR-{_bcode()}-SELF TRANSFER {other_acct}"
        t_out = _make_txn(out_d, out_desc, debit=amt, category="TRANSFER",
                           anomaly_flags=["inter_account_transfer"])
        t_in = _make_txn(in_d, in_desc, credit=amt, category="TRANSFER",
                          anomaly_flags=["inter_account_transfer"])
        new_txns += [t_out, t_in]
    flag = {
        "flag_type": "inter_account_transfer",
        "description": (
            f"Added {len(new_txns)} self-transfer transactions to/from another account "
            f"({other_acct}) the company holds — tests TRANSFER categorisation and "
            "round-tripping false-positive resistance."
        ),
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
    }
    return txns + new_txns, flag


def _inject_missing_month_gap(txns, company, bank, start):
    """Remove all transactions from the middle month of the statement period
    — tests that consecutive-month-dependent logic (revenue trend, customer
    count trend, churn silence window) doesn't crash or silently miscount
    across a gap with zero bank activity."""
    if not txns:
        return txns, {
            "flag_type": "missing_month_gap", "description": "No transactions to gap.",
            "triggering_transaction_ids": [],
        }
    months_present = sorted({(t.date.year, t.date.month) for t in txns})
    if len(months_present) < 3:
        return txns, {
            "flag_type": "missing_month_gap",
            "description": "Fewer than 3 months present — no middle month to remove.",
            "triggering_transaction_ids": [],
        }
    gap_yr, gap_mo = months_present[len(months_present) // 2]
    removed_ids = [t.txn_id for t in txns if (t.date.year, t.date.month) == (gap_yr, gap_mo)]
    remaining = [t for t in txns if (t.date.year, t.date.month) != (gap_yr, gap_mo)]
    flag = {
        "flag_type": "missing_month_gap",
        "description": (
            f"Removed all {len(removed_ids)} transactions from {gap_yr}-{gap_mo:02d} "
            "(middle of the statement period) to simulate a month with zero bank activity."
        ),
        "triggering_transaction_ids": removed_ids,
    }
    return remaining, flag


def _inject_fx_inflow(txns, company, bank, start):
    """Add export/SWIFT-remittance-style revenue narrations — the credited
    amount is INR (Indian banks convert FX inflows before crediting a regular
    current account), but the narration text signals a foreign-currency
    source. Tests that the categoriser still recognises these as REVENUE
    despite non-standard narration text, and that cash-focused compliance
    rules (§269ST, PMLA) correctly do not fire on wire transfers."""
    new_txns = []
    for yr, mo in _months(start, 2):
        amt = Decimal(str(random.randint(300, 900) * 1000))
        d = _rand_day(yr, mo, 5, 25)
        if bank == "hdfc":
            desc = (f"SWIFT CR-{_bcode()}-EXPORT RECEIPT USD {random.randint(3000, 9000)}-"
                     f"INV{random.randint(1000, 9999)}")
        else:
            desc = f"INWARD REMITTANCE CR-{_bcode()}-FCRA EXPORT USD {random.randint(3000, 9000)}"
        t = _make_txn(d, desc, credit=amt, category="REVENUE", anomaly_flags=["fx_inflow"])
        new_txns.append(t)
    flag = {
        "flag_type": "fx_inflow",
        "description": (
            f"Added {len(new_txns)} foreign-currency export-remittance revenue transactions "
            "(INR-converted amount, SWIFT/FCRA narration) — tests categorisation robustness "
            "against non-standard revenue narrations."
        ),
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
    }
    return txns + new_txns, flag


_FLAG_INJECTORS = {
    "structuring":                    _inject_structuring,
    "round_tripping":                 _inject_round_tripping,
    "founder_extraction":             _inject_founder_extraction,
    "customer_churn":                 _inject_customer_churn,
    "revenue_concentration":          _inject_revenue_concentration,
    "related_party_leakage":          _inject_related_party_leakage,
    # Compliance fault injectors
    "compliance_269st":               _inject_compliance_269st,
    "compliance_gst_gap":             _inject_compliance_gst_gap,
    "compliance_tds_gap":             _inject_compliance_tds_gap,
    "compliance_pmla_cash":           _inject_compliance_pmla_cash,
    "compliance_rpt_concentration":   _inject_compliance_rpt_concentration,
    "compliance_cash_loan":           _inject_compliance_cash_loan,
    # Wave 4.3 — hard-case hardening scenarios
    "aggregator_dominated_revenue":   _inject_aggregator_dominated,
    "inter_account_transfer":         _inject_inter_account_transfers,
    "missing_month_gap":              _inject_missing_month_gap,
    "fx_inflow":                      _inject_fx_inflow,
}

_GENERATORS = {
    "healthy_saas":   _gen_healthy_saas,
    "burning_startup": _gen_burning_startup,
    "services_firm":  _gen_services_firm,
    "ecommerce":      _gen_ecommerce,
    "restaurant":     _gen_restaurant,
}


# ─────────────────────────────────────────────────────────────────────────────
# Balance computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_balances(txns: list[_Txn], opening: Decimal) -> list[_Txn]:
    txns.sort(key=lambda t: t.date)
    bal = opening
    for t in txns:
        if t.credit is not None:
            bal += t.credit
        if t.debit is not None:
            bal -= t.debit
        t.balance = bal
    return txns


# ─────────────────────────────────────────────────────────────────────────────
# PDF renderers
# ─────────────────────────────────────────────────────────────────────────────

_HDFC_COLS    = [42, 168, 78, 42, 62, 62, 72]
_HDFC_HEADERS = ["Date", "Narration", "Chq./Ref.No.", "Value Dt",
                 "Withdrawal Amt.(Dr)", "Deposit Amt.(Cr)", "Closing Balance"]

_ICICI_COLS    = [25, 48, 48, 155, 78, 55, 55, 62]
_ICICI_HEADERS = ["S No.", "Transaction Date", "Value Date", "Description",
                  "Ref No./Cheque No.", "Debit", "Credit", "Balance"]

_HDR_BG  = colors.HexColor("#1B3A6B")
_HDR_FG  = colors.white
_ROW_ALT = colors.HexColor("#F2F5FA")
_GRID    = colors.HexColor("#CCCCCC")

# Paragraph style for narration/description cells — enables word-wrap
_NARRATION_STYLE = ParagraphStyle(
    "narration",
    fontName="Helvetica",
    fontSize=6.5,
    leading=8,
    wordWrap="CJK",
)


def _table_style(n_rows: int, has_sno: bool = False) -> TableStyle:
    cmds = [
        ("BACKGROUND",    (0, 0), (-1, 0),  _HDR_BG),
        ("TEXTCOLOR",     (0, 0), (-1, 0),  _HDR_FG),
        ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, 0),  6.5),
        ("ALIGN",         (0, 0), (-1, 0),  "CENTER"),
        ("VALIGN",        (0, 0), (-1,  0), "MIDDLE"),
        ("VALIGN",        (0, 1), (-1, -1), "TOP"),
        ("ROWBACKGROUND", (0, 1), (-1, -1), [colors.white, _ROW_ALT]),
        ("FONTNAME",      (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, 1), (-1, -1), 6.5),
        ("GRID",          (0, 0), (-1, -1), 0.3, _GRID),
        ("TOPPADDING",    (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING",   (0, 0), (-1, -1), 3),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 3),
        ("ALIGN",         (-3, 1), (-1, -1), "RIGHT"),
    ]
    if has_sno:
        cmds.append(("ALIGN", (0, 0), (0, -1), "CENTER"))
    return TableStyle(cmds)


def _render_hdfc(company: _Company, txns: list[_Txn], opening: Decimal, path: Path) -> None:
    styles = getSampleStyleSheet()
    period_start = txns[0].date if txns else date.today()
    period_end   = txns[-1].date if txns else date.today()

    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN)

    def _p(text): return Paragraph(text, styles["Normal"])

    story = [
        Paragraph("HDFC BANK", styles["Heading1"]),
        Paragraph("Account Statement", styles["Heading2"]),
        Spacer(1, 4 * mm),
        _p(f"<b>Account Holder:</b> {company.name}"),
        _p(f"<b>Account No:</b> {company.account_no}"),
        _p(f"<b>Branch:</b> {company.branch}"),
        _p(f"<b>IFSC:</b> {company.ifsc}"),
        _p(f"<b>Statement Period:</b> "
           f"{period_start.strftime('%d/%m/%Y')} To {period_end.strftime('%d/%m/%Y')}"),
        Spacer(1, 4 * mm),
    ]

    def _narr(text: str) -> Paragraph:
        return Paragraph(text, _NARRATION_STYLE)

    table_data = [_HDFC_HEADERS]
    table_data.append([
        period_start.strftime("%d/%m/%y"), _narr("Opening Balance"), "",
        period_start.strftime("%d/%m/%y"), "", _inr(opening), _inr_signed(opening),
    ])
    for t in txns:
        table_data.append([
            t.date.strftime("%d/%m/%y"), _narr(t.description), t.ref_no,
            t.value_date.strftime("%d/%m/%y"),
            _inr(t.debit), _inr(t.credit), _inr_signed(t.balance),
        ])

    closing  = txns[-1].balance if txns else opening
    total_dr = sum(t.debit  for t in txns if t.debit)  or Decimal("0")
    total_cr = sum(t.credit for t in txns if t.credit) or Decimal("0")

    story.append(Table(table_data, colWidths=_HDFC_COLS, repeatRows=1,
                       style=_table_style(len(table_data))))
    story += [
        Spacer(1, 4 * mm),
        _p(f"<b>Opening Balance:</b> {_inr(opening)}  "
           f"<b>Total Withdrawals:</b> {_inr(total_dr)}  "
           f"<b>Total Deposits:</b> {_inr(total_cr)}  "
           f"<b>Closing Balance:</b> {_inr_signed(closing)}"),
    ]
    doc.build(story)


def _render_icici(company: _Company, txns: list[_Txn], opening: Decimal, path: Path) -> None:
    styles = getSampleStyleSheet()
    period_start = txns[0].date if txns else date.today()
    period_end   = txns[-1].date if txns else date.today()

    doc = SimpleDocTemplate(str(path), pagesize=A4,
                            leftMargin=MARGIN, rightMargin=MARGIN,
                            topMargin=MARGIN, bottomMargin=MARGIN)

    def _p(text): return Paragraph(text, styles["Normal"])

    story = [
        Paragraph("ICICI Bank", styles["Heading1"]),
        Paragraph("Account Statement", styles["Heading2"]),
        Spacer(1, 4 * mm),
        _p(f"<b>Name:</b> {company.name}"),
        _p(f"<b>Account Number:</b> {company.account_no}"),
        _p("<b>Account Type:</b> Current Account"),
        _p(f"<b>Branch:</b> {company.branch}"),
        _p(f"<b>IFSC Code:</b> {company.ifsc}"),
        _p(f"<b>Statement From:</b> {period_start.strftime('%d-%m-%Y')}  "
           f"<b>To:</b> {period_end.strftime('%d-%m-%Y')}"),
        Spacer(1, 4 * mm),
    ]

    def _narr(text: str) -> Paragraph:
        return Paragraph(text, _NARRATION_STYLE)

    table_data = [_ICICI_HEADERS]
    table_data.append([
        "0", period_start.strftime("%d-%m-%Y"), period_start.strftime("%d-%m-%Y"),
        _narr("Opening Balance"), "", "", _inr(opening), _inr_signed(opening),
    ])
    for idx, t in enumerate(txns, start=1):
        table_data.append([
            str(idx),
            t.date.strftime("%d-%m-%Y"),
            t.value_date.strftime("%d-%m-%Y"),
            _narr(t.description),
            t.ref_no,
            _inr(t.debit),
            _inr(t.credit),
            _inr_signed(t.balance),
        ])

    story.append(Table(table_data, colWidths=_ICICI_COLS, repeatRows=1,
                       style=_table_style(len(table_data), has_sno=True)))
    closing = txns[-1].balance if txns else opening
    story += [Spacer(1, 4 * mm), _p(f"<b>Closing Balance:</b> {_inr_signed(closing)}")]
    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# Ground truth JSON
# ─────────────────────────────────────────────────────────────────────────────

def _build_truth(statement_id, bank, profile, company, txns, injected_flags, opening, path):
    closing        = txns[-1].balance if txns else opening
    revenue_txns   = [t for t in txns if t.category == "REVENUE"]
    expense_txns   = [t for t in txns if t.category not in ("REVENUE", "TRANSFER")]
    total_revenue  = sum(t.credit for t in revenue_txns if t.credit) or Decimal("0")
    total_expenses = sum(t.debit  for t in expense_txns if t.debit)  or Decimal("0")

    months_covered = max(len(set((t.date.year, t.date.month) for t in txns)), 1)
    monthly_burn   = (total_expenses / months_covered).quantize(Decimal("1"))
    runway         = float(closing / monthly_burn) if monthly_burn > 0 else float("inf")
    customer_ids   = {t.customer_id for t in revenue_txns if t.customer_id}

    truth = {
        "statement_id": statement_id,
        "bank": bank,
        "profile": profile,
        "account_id": company.account_no,
        "period_start": txns[0].date.isoformat() if txns else "",
        "period_end":   txns[-1].date.isoformat() if txns else "",
        "opening_balance": str(opening),
        "closing_balance": str(closing),
        "metrics": {
            "true_monthly_burn_rate": str(monthly_burn),
            "true_runway_months":     round(runway, 1),
            "true_customer_count":    len(customer_ids),
            "true_total_revenue":     str(total_revenue.quantize(Decimal("1"))),
            "true_total_expenses":    str(total_expenses.quantize(Decimal("1"))),
        },
        "injected_flags": injected_flags,
        "transactions": [
            {
                "transaction_id":    t.txn_id,
                "date":              t.date.isoformat(),
                "description":       t.description,
                "debit":             str(t.debit)   if t.debit  else None,
                "credit":            str(t.credit)  if t.credit else None,
                "balance":           str(t.balance),
                "category":          t.category,
                "customer_id":       t.customer_id,
                "is_related_party":  t.is_related_party,
                "related_party_match": t.related_party_match,
                "anomaly_flags":     t.anomaly_flags,
            }
            for t in txns
        ],
    }
    path.write_text(json.dumps(truth, indent=2), encoding="utf-8")


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_statement(
    bank: str,
    profile: str,
    output_dir: Path,
    statement_id: Optional[str] = None,
    flags: Optional[list[str]] = None,
    risk_mode: str = "manual",   # "manual" | "realistic" | "clean"
    n_months: int = 3,
    start: Optional[date] = None,
    seed: Optional[int] = None,
) -> tuple[Path, Path]:
    """Generate one synthetic bank statement PDF + paired ground truth JSON.

    risk_mode:
      "manual"    — use flags list as-is (original behaviour)
      "realistic" — probabilistically inject flags based on profile (ignores flags)
      "clean"     — no flags injected

    Returns (pdf_path, truth_path).
    """
    if seed is not None:
        random.seed(seed)
    if start is None:
        start = date(2024, 1, 1)
    if statement_id is None:
        statement_id = f"{bank}_{profile}_{uuid.uuid4().hex[:6]}"

    company = _COMPANY_CONFIGS[profile]
    txns    = _GENERATORS[profile](bank, company, start, n_months)

    if risk_mode == "realistic":
        active_flags = auto_flags_for_profile(profile)
    elif risk_mode == "clean":
        active_flags = []
    else:
        active_flags = list(flags or [])

    injected_flags: list[dict] = []
    for flag_name in active_flags:
        if flag_name in _FLAG_INJECTORS:
            txns, flag_meta = _FLAG_INJECTORS[flag_name](txns, company, bank, start)
            injected_flags.append(flag_meta)

    txns = _compute_balances(txns, company.opening_balance)

    out_dir = output_dir / profile
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path   = out_dir / f"{statement_id}.pdf"
    truth_path = out_dir / f"{statement_id}.truth.json"

    renderer = _render_hdfc if bank == "hdfc" else _render_icici
    renderer(company, txns, company.opening_balance, pdf_path)
    _build_truth(statement_id, bank, profile, company, txns,
                 injected_flags, company.opening_balance, truth_path)

    return pdf_path, truth_path


def generate_statement_to_bytes(
    bank: str,
    profile: str,
    flags: Optional[list[str]] = None,
    risk_mode: str = "realistic",
    n_months: int = 6,
    start: Optional[date] = None,
    seed: Optional[int] = None,
) -> tuple[bytes, str, list[str]]:
    """Generate a statement in memory and return (pdf_bytes, company_name, injected_flag_names).

    For use from Streamlit without writing to disk.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        pdf_path, truth_path = generate_statement(
            bank=bank, profile=profile,
            output_dir=tmp_path,
            flags=flags, risk_mode=risk_mode,
            n_months=n_months, start=start, seed=seed,
        )
        pdf_bytes = pdf_path.read_bytes()
        truth = json.loads(truth_path.read_text(encoding="utf-8"))
        company_name = _COMPANY_CONFIGS[profile].name
        injected = [f["flag_type"] for f in truth.get("injected_flags", [])]
    return pdf_bytes, company_name, injected


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic Indian bank statements")
    parser.add_argument("--bank",      choices=["hdfc", "icici"], required=True)
    parser.add_argument("--profile",   choices=list(_GENERATORS),  required=True)
    parser.add_argument("--count",     type=int, default=1)
    parser.add_argument("--months",    type=int, default=6)
    parser.add_argument("--output",    type=Path, default=Path("data/synthetic"))
    parser.add_argument("--flags",     default="", help="comma-separated flag names (manual mode)")
    parser.add_argument("--risk-mode", choices=["manual", "realistic", "clean"], default="manual")
    parser.add_argument("--seed",      type=int, default=None)
    args = parser.parse_args()

    flag_list = [f.strip() for f in args.flags.split(",") if f.strip()]

    for i in range(args.count):
        seed = args.seed + i if args.seed is not None else None
        pdf, truth = generate_statement(
            bank=args.bank, profile=args.profile,
            output_dir=args.output,
            flags=flag_list, risk_mode=args.risk_mode,
            n_months=args.months, seed=seed,
        )
        print(f"[{i+1}/{args.count}] {pdf.name}  |  {truth.name}")

    print(f"\nDone — {args.count} statement(s) in {args.output / args.profile}/")


if __name__ == "__main__":
    main()
