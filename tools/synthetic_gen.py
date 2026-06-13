#!/usr/bin/env python3
"""Synthetic Indian bank statement generator — HDFC and ICICI formats.

CLI:
    python -m tools.synthetic_gen --bank hdfc --profile healthy_saas --count 5 --output data/synthetic/
    python -m tools.synthetic_gen --bank icici --profile burning_startup --count 3 --flags structuring,round_tripping --output data/synthetic/
"""
from __future__ import annotations

import argparse
import json
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

MARGIN = 10 * mm
_BANK_CODES = ["SBIN", "HDFC", "ICIC", "UTIB", "KKBK", "YESB", "PUNB"]

# Customer roster per profile
_SAAS_CUSTOMERS: list[tuple[str, str]] = [
    ("cust_001", "Acme Solutions Pvt Ltd"),
    ("cust_002", "BrightPath Tech Inc"),
    ("cust_003", "CoreLogic Systems"),
    ("cust_004", "DataMesh Analytics"),
    ("cust_005", "Epsilon Systems Ltd"),
    ("cust_006", "FuturaTech Inc"),
    ("cust_007", "GrowthHub Digital"),
    ("cust_008", "HorizonAI Labs"),
    ("cust_009", "InnovateCo Systems"),
    ("cust_010", "JetStream Digital"),
    ("cust_011", "KineticApps Pvt Ltd"),
    ("cust_012", "LuminAI Technologies"),
    ("cust_013", "MetaPulse Systems"),
    ("cust_014", "NexaData Inc"),
    ("cust_015", "OmniFlow Solutions"),
    ("cust_016", "PinnacleTech Ltd"),
    ("cust_017", "QuantumSoft Labs"),
    ("cust_018", "RapidBase Systems"),
    ("cust_019", "SwiftData Analytics"),
    ("cust_020", "TitanCode Inc"),
    ("cust_021", "UltraGrid Tech"),
    ("cust_022", "VortexAI Solutions"),
    ("cust_023", "WarpSpeed Systems"),
    ("cust_024", "XcelSoft Inc"),
    ("cust_025", "YieldTech Analytics"),
    ("cust_026", "ZenithApps Pvt Ltd"),
    ("cust_027", "AlphaStack Systems"),
    ("cust_028", "BetaCloud Technologies"),
    ("cust_029", "GammaTech Solutions"),
    ("cust_030", "DeltaAI Inc"),
    ("cust_031", "EtaTech Systems"),
    ("cust_032", "ZetaSoft Labs"),
    ("cust_033", "ThetaApps Digital"),
    ("cust_034", "IotaData Systems"),
    ("cust_035", "KappaSolutions Ltd"),
]

_STARTUP_CUSTOMERS: list[tuple[str, str]] = [
    ("cust_001", "GlobalTech Solutions"),
    ("cust_002", "FutureSoft Inc"),
    ("cust_003", "InnovatePro Systems"),
    ("cust_004", "DigitalEdge Tech"),
    ("cust_005", "CloudNex Labs"),
    ("cust_006", "NetPulse Analytics"),
    ("cust_007", "DataForce Solutions"),
    ("cust_008", "VisionTech Inc"),
    ("cust_009", "PrimeSoft Digital"),
    ("cust_010", "ApexDigital Systems"),
]

_SERVICES_CUSTOMERS: list[tuple[str, str]] = [
    ("cust_001", "Bharti Enterprises"),
    ("cust_002", "Mahindra Solutions"),
    ("cust_003", "Tata Projects Ltd"),
    ("cust_004", "Reliance Infra"),
    ("cust_005", "ONGC Digital"),
    ("cust_006", "IndusInd Consulting"),
    ("cust_007", "Maruti Systems"),
    ("cust_008", "Bajaj InfoTech"),
    ("cust_009", "Hero Technologies"),
    ("cust_010", "LnT Digital"),
    ("cust_011", "Wipro Advisory"),
    ("cust_012", "HCL Consulting"),
    ("cust_013", "Infosys BPM Ltd"),
    ("cust_014", "TCS Projects"),
    ("cust_015", "Cognizant India"),
]

_FOOD_VENDORS = [
    "Agmark Fresh Pvt Ltd", "Modi Agro Foods", "Parle Distributors",
    "Mother Dairy Supplies", "Haldiram Distribution", "ITC Foods Supply",
    "Britannia Trade Link", "Nestle India Distribution",
]

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
    """Format Decimal as Indian number string — e.g. 1,50,000.00."""
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
    """Return bank-appropriate narration string."""
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
            return f"NEFT/CBSB{random.randint(10000,99999)}/GST PAYMENT/GSTIN{random.randint(10**14, 10**15-1)}"
        if mode == "loan":
            return f"NEFT/{_bcode()}/{party.upper()}/LOAN{random.randint(1000,9999)}"
        if mode == "founder":
            return f"NEFT/{_bcode()}/{party.upper()} PERSONAL/{_hex8()}"
        if mode == "razorpay":
            return f"NEFT/{_bcode()}/RAZORPAY SOFTWARE/SETL{inv:06d}"
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
        if mode == "loan":
            return f"NEFT CR-{_bcode()}-{party.upper()}-LOAN{random.randint(1000,9999)}"
        if mode == "founder":
            return f"NEFT DR-{_bcode()}-{party.upper()} PERSONAL"
        if mode == "razorpay":
            return f"NEFT CR-{_bcode()}-RAZORPAY SOFTWARE-SETL{inv:06d}"
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
# Transaction generators — one per profile
# ─────────────────────────────────────────────────────────────────────────────

def _months(start: date, n: int) -> list[tuple[int, int]]:
    """Return list of (year, month) tuples for n months starting from start."""
    result = []
    y, m = start.year, start.month
    for _ in range(n):
        result.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return result


def _gen_healthy_saas(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    inv = 1000
    # Stable per-customer monthly payment (₹20K–₹60K)
    customer_amounts = {cid: Decimal(str(random.randint(20, 60) * 1000)) for cid, _ in _SAAS_CUSTOMERS}
    employees = [
        ("Arun Kumar", Decimal("80000")),
        ("Meena Nair", Decimal("95000")),
        ("Suresh Iyer", Decimal("120000")),
        ("Deepa Reddy", Decimal("85000")),
        ("Vikram Joshi", Decimal("110000")),
    ]

    for month_idx, (yr, mo) in enumerate(_months(start, n_months)):
        # 3% MoM growth — unlock a few more customers each month
        active_count = 30 + min(month_idx * 2, 5)
        active_customers = _SAAS_CUSTOMERS[:active_count]

        # Revenue — customers pay on random dates 5th-25th
        for cid, cname in active_customers:
            amt = customer_amounts[cid] * Decimal(str(1 + 0.03 * month_idx))
            amt = amt.quantize(Decimal("1"))
            pay_date = _rand_day(yr, mo, 5, 25)
            txns.append(_make_txn(
                pay_date,
                _narration(bank, "credit", cname, inv),
                credit=amt,
                category="REVENUE",
                customer_id=cid,
            ))
            inv += 1

        # Salaries — 1st of month
        for emp_name, sal in employees:
            txns.append(_make_txn(
                date(yr, mo, 1),
                _narration(bank, "salary", emp_name),
                debit=sal,
                category="SALARY",
            ))

        # AWS
        txns.append(_make_txn(
            _rand_day(yr, mo, 1, 5),
            _narration(bank, "debit", "Amazon Web Services"),
            debit=Decimal("48000"),
            category="VENDOR_PAYMENT",
        ))
        # Office rent
        txns.append(_make_txn(
            date(yr, mo, 1),
            _narration(bank, "debit", "Regus Office Spaces"),
            debit=Decimal("150000"),
            category="VENDOR_PAYMENT",
        ))
        # Misc vendor
        misc = Decimal(str(random.randint(20, 80) * 1000))
        txns.append(_make_txn(
            _rand_day(yr, mo, 8, 20),
            _narration(bank, "debit", "Miscellaneous Vendor"),
            debit=misc,
            category="VENDOR_PAYMENT",
        ))
        # GST payment
        rev_total = sum(customer_amounts[c] for c, _ in active_customers)
        gst = (rev_total * Decimal("0.18")).quantize(Decimal("1"))
        txns.append(_make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "gst", ""),
            debit=gst,
            category="TAX",
        ))

    return txns


def _gen_burning_startup(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    inv = 2000
    customer_amounts = {cid: Decimal(str(random.randint(40, 80) * 1000)) for cid, _ in _STARTUP_CUSTOMERS}
    employees = [
        ("Ravi Shankar", Decimal("120000")),
        ("Ananya Bose", Decimal("140000")),
        ("Kiran Rao", Decimal("130000")),
        ("Pooja Menon", Decimal("115000")),
        ("Sanjay Desai", Decimal("160000")),
        ("Neha Verma", Decimal("145000")),
        ("Tarun Khanna", Decimal("170000")),
        ("Shruti Agarwal", Decimal("155000")),
    ]

    for month_idx, (yr, mo) in enumerate(_months(start, n_months)):
        # Flat/declining customer base
        active_count = max(10 - month_idx, 7)
        active_customers = _STARTUP_CUSTOMERS[:active_count]

        for cid, cname in active_customers:
            pay_date = _rand_day(yr, mo, 5, 25)
            txns.append(_make_txn(
                pay_date,
                _narration(bank, "credit", cname, inv),
                credit=customer_amounts[cid],
                category="REVENUE",
                customer_id=cid,
            ))
            inv += 1

        # Increasing salaries (hiring)
        for emp_name, sal in employees:
            txns.append(_make_txn(
                date(yr, mo, 1),
                _narration(bank, "salary", emp_name),
                debit=sal + Decimal(str(month_idx * 5000)),
                category="SALARY",
            ))

        # High vendor burn
        for vendor, base_amt in [
            ("Amazon Web Services", Decimal("180000")),
            ("Google Cloud Platform", Decimal("120000")),
            ("Regus Office Spaces", Decimal("250000")),
            ("Digital Marketing Agency", Decimal(str(random.randint(200, 500) * 1000))),
            ("Legal Advisory Firm", Decimal(str(random.randint(50, 150) * 1000))),
        ]:
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 15),
                _narration(bank, "debit", vendor),
                debit=base_amt,
                category="VENDOR_PAYMENT",
            ))

        # GST
        rev = sum(customer_amounts[c] for c, _ in active_customers)
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
    # Large lumpy payments — not every customer pays every month
    customer_amounts = {
        cid: Decimal(str(random.randint(200, 1200) * 10000))
        for cid, _ in _SERVICES_CUSTOMERS
    }
    employees = [
        ("Kavita Sharma", Decimal("150000")),
        ("Manish Tiwari", Decimal("165000")),
        ("Ruchi Agarwal", Decimal("140000")),
        ("Abhishek Singh", Decimal("180000")),
    ]

    for (yr, mo) in _months(start, n_months):
        # 5-12 clients pay this month (random subset)
        paying = random.sample(_SERVICES_CUSTOMERS, random.randint(5, 12))
        for cid, cname in paying:
            pay_date = _rand_day(yr, mo, 5, 28)
            txns.append(_make_txn(
                pay_date,
                _narration(bank, "credit", cname, inv),
                credit=customer_amounts[cid],
                category="REVENUE",
                customer_id=cid,
            ))
            inv += 1

        for emp_name, sal in employees:
            txns.append(_make_txn(
                date(yr, mo, 2),
                _narration(bank, "salary", emp_name),
                debit=sal,
                category="SALARY",
            ))

        txns.append(_make_txn(
            date(yr, mo, 1),
            _narration(bank, "debit", "Regus Office Spaces"),
            debit=Decimal("200000"),
            category="VENDOR_PAYMENT",
        ))
        txns.append(_make_txn(
            _rand_day(yr, mo, 5, 20),
            _narration(bank, "debit", "Travel Expense Reimbursements"),
            debit=Decimal(str(random.randint(30, 120) * 1000)),
            category="VENDOR_PAYMENT",
        ))

    return txns


def _gen_ecommerce(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []
    setl = 5000

    for (yr, mo) in _months(start, n_months):
        # Razorpay daily settlement payouts (every 2-3 days)
        d = date(yr, mo, 1)
        while d.month == mo:
            payout = Decimal(str(random.randint(80, 300) * 1000))
            txns.append(_make_txn(
                d,
                _narration(bank, "razorpay", "Razorpay Software", setl),
                credit=payout,
                category="REVENUE",
                customer_id="cust_razorpay_aggregated",
            ))
            setl += 1
            d += timedelta(days=random.randint(2, 3))

        # Inventory vendors
        for vendor, amt in [
            ("Flipkart Commerce", Decimal(str(random.randint(500, 2000) * 1000))),
            ("Amazon Seller Services", Decimal(str(random.randint(300, 1500) * 1000))),
            ("Myntra Designs", Decimal(str(random.randint(200, 800) * 1000))),
        ]:
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 10),
                _narration(bank, "debit", vendor),
                debit=amt,
                category="VENDOR_PAYMENT",
            ))

        # Logistics
        txns.append(_make_txn(
            _rand_day(yr, mo, 5, 25),
            _narration(bank, "debit", "Delhivery Logistics"),
            debit=Decimal(str(random.randint(150, 500) * 1000)),
            category="VENDOR_PAYMENT",
        ))
        # Salaries
        for emp, sal in [("Logistics Head", Decimal("130000")), ("Tech Lead", Decimal("180000")),
                         ("Ops Manager", Decimal("120000")), ("Data Analyst", Decimal("95000")),
                         ("Customer Success", Decimal("75000"))]:
            txns.append(_make_txn(
                date(yr, mo, 1),
                _narration(bank, "salary", emp),
                debit=sal,
                category="SALARY",
            ))
        # Payment gateway fees
        txns.append(_make_txn(
            _rand_day(yr, mo, 5, 10),
            _narration(bank, "debit", "Razorpay Fee Deduction"),
            debit=Decimal(str(random.randint(15, 45) * 1000)),
            category="FEES",
        ))

    return txns


def _gen_restaurant(bank: str, company: _Company, start: date, n_months: int) -> list[_Txn]:
    txns: list[_Txn] = []

    for (yr, mo) in _months(start, n_months):
        # Daily cash/UPI deposits
        d = date(yr, mo, 1)
        while d.month == mo:
            daily_amt = Decimal(str(random.randint(25, 80) * 1000))
            desc = (_narration(bank, "cash", "Koregaon Park Branch")
                    if random.random() < 0.5 else
                    f"UPI/CR/{random.randint(9000000000, 9999999999)}/SWIGGY PAYOUT")
            txns.append(_make_txn(
                d,
                desc,
                credit=daily_amt,
                category="REVENUE",
                customer_id="cust_daily_sales",
            ))
            d += timedelta(days=1)

        # Food vendors — weekly
        for vendor in random.sample(_FOOD_VENDORS, 4):
            txns.append(_make_txn(
                _rand_day(yr, mo, 1, 28),
                _narration(bank, "debit", vendor),
                debit=Decimal(str(random.randint(80, 300) * 1000)),
                category="VENDOR_PAYMENT",
            ))

        # Staff salaries (15 staff × ~₹25K avg)
        for i in range(1, 16):
            txns.append(_make_txn(
                date(yr, mo, 5),
                _narration(bank, "salary", f"Staff Member {i:02d}"),
                debit=Decimal(str(random.randint(20, 35) * 1000)),
                category="SALARY",
            ))

        # GST (restaurant: 5% on food)
        monthly_rev = Decimal(str(random.randint(70, 200) * 10000))
        txns.append(_make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "gst", ""),
            debit=(monthly_rev * Decimal("0.05")).quantize(Decimal("1")),
            category="TAX",
        ))
        # Utilities
        txns.append(_make_txn(
            _rand_day(yr, mo, 10, 20),
            _narration(bank, "debit", "MSEDCL Electricity"),
            debit=Decimal(str(random.randint(15, 40) * 1000)),
            category="VENDOR_PAYMENT",
        ))

    return txns


# ─────────────────────────────────────────────────────────────────────────────
# Red flag injectors
# ─────────────────────────────────────────────────────────────────────────────

def _inject_structuring(
    txns: list[_Txn], company: _Company, bank: str, start: date
) -> tuple[list[_Txn], dict[str, object]]:
    """Cluster of cash deposits just under ₹2 lakh — §269ST avoidance pattern."""
    new_txns: list[_Txn] = []
    for i in range(5):
        amt = Decimal(str(random.randint(155000, 198000)))
        d = start + timedelta(days=random.randint(0, 6))
        t = _make_txn(
            d,
            _narration(bank, "cash", "Main Branch"),
            credit=amt,
            category="REVENUE",
            anomaly_flags=["structuring"],
        )
        new_txns.append(t)
    flag: dict[str, object] = {
        "flag_type": "structuring",
        "description": "5 cash deposits between ₹1.55L and ₹1.98L within a 7-day window",
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
    }
    return txns + new_txns, flag


def _inject_round_tripping(
    txns: list[_Txn], company: _Company, bank: str, start: date
) -> tuple[list[_Txn], dict[str, object]]:
    """Large outflow followed by matching inflow from same counterparty 3-7 days later."""
    amt = Decimal(str(random.randint(500, 1500) * 1000))
    party = "Zenith Capital Advisory"
    out_date = start + timedelta(days=random.randint(5, 20))
    in_date = out_date + timedelta(days=random.randint(3, 7))
    out_txn = _make_txn(
        out_date,
        _narration(bank, "debit", party),
        debit=amt,
        category="TRANSFER",
        anomaly_flags=["round_tripping"],
    )
    in_txn = _make_txn(
        in_date,
        _narration(bank, "credit", party, random.randint(1, 9999)),
        credit=amt * Decimal("0.98"),  # 2% fee — realistic
        category="TRANSFER",
        anomaly_flags=["round_tripping"],
    )
    flag: dict[str, object] = {
        "flag_type": "round_tripping",
        "description": f"Outflow of {_inr(amt)} to {party}, matching inflow 3-7 days later",
        "triggering_transaction_ids": [out_txn.txn_id, in_txn.txn_id],
    }
    return txns + [out_txn, in_txn], flag


def _inject_founder_extraction(
    txns: list[_Txn], company: _Company, bank: str, start: date
) -> tuple[list[_Txn], dict[str, object]]:
    """Monthly withdrawals to founder personal exceeding declared salary by 3x."""
    declared_salary = Decimal("150000")
    new_txns: list[_Txn] = []
    for yr, mo in _months(start, 3):
        amt = declared_salary * Decimal("3.5")
        t = _make_txn(
            _rand_day(yr, mo, 20, 28),
            _narration(bank, "founder", f"{company.founder_name} {company.founder_surname}", ""),
            debit=amt,
            category="FOUNDER_WITHDRAWAL",
            anomaly_flags=["founder_over_extraction"],
        )
        new_txns.append(t)
    flag: dict[str, object] = {
        "flag_type": "founder_extraction",
        "description": (
            f"Monthly founder withdrawals of {_inr(declared_salary * Decimal('3.5'))} "
            f"exceed declared salary of {_inr(declared_salary)} by 3.5x"
        ),
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "declared_founder_salary": str(declared_salary),
    }
    return txns + new_txns, flag


def _inject_customer_churn(
    txns: list[_Txn], company: _Company, bank: str, start: date
) -> tuple[list[_Txn], dict[str, object]]:
    """20% of recurring customers stop paying in months 2-3."""
    mths = _months(start, 3)
    # Identify REVENUE txns from month 1 with customer_ids
    m1_yr, m1_mo = mths[0]
    m1_customers: set[str] = set()
    for t in txns:
        if t.category == "REVENUE" and t.customer_id and t.date.year == m1_yr and t.date.month == m1_mo:
            m1_customers.add(t.customer_id)

    churn_count = max(1, int(len(m1_customers) * 0.20))
    churned = set(random.sample(sorted(m1_customers), min(churn_count, len(m1_customers))))

    # Remove their payments from months 2 & 3
    filtered = [
        t for t in txns
        if not (
            t.customer_id in churned
            and t.category == "REVENUE"
            and (t.date.year, t.date.month) in mths[1:]
        )
    ]

    flag: dict[str, object] = {
        "flag_type": "customer_churn",
        "description": f"{churn_count} customers ({churn_count/max(len(m1_customers),1)*100:.0f}%) stopped paying after month 1",
        "triggering_transaction_ids": [],
        "churned_customer_ids": sorted(churned),
    }
    return filtered, flag


def _inject_revenue_concentration(
    txns: list[_Txn], company: _Company, bank: str, start: date
) -> tuple[list[_Txn], dict[str, object]]:
    """70%+ of revenue from one counterparty."""
    new_txns: list[_Txn] = []
    inv = 9000
    whale = ("cust_whale", "Hyperion Mega Corp Ltd")
    for yr, mo in _months(start, 3):
        # Calculate current month revenue to make whale ≈ 75% of total
        month_rev = sum(
            t.credit for t in txns
            if t.credit and t.category == "REVENUE"
            and t.date.year == yr and t.date.month == mo
        )
        whale_amt = (month_rev * Decimal("3")).quantize(Decimal("1000"))
        t = _make_txn(
            _rand_day(yr, mo, 10, 20),
            _narration(bank, "credit", whale[1], inv),
            credit=whale_amt,
            category="REVENUE",
            customer_id=whale[0],
            anomaly_flags=["revenue_concentration"],
        )
        new_txns.append(t)
        inv += 1
    flag: dict[str, object] = {
        "flag_type": "revenue_concentration",
        "description": "Single customer 'Hyperion Mega Corp Ltd' contributes >70% of monthly revenue",
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "concentrated_customer_id": whale[0],
    }
    return txns + new_txns, flag


def _inject_related_party_leakage(
    txns: list[_Txn], company: _Company, bank: str, start: date
) -> tuple[list[_Txn], dict[str, object]]:
    """Monthly payments to entity sharing founder surname."""
    related_entity = f"{company.founder_surname} Family Consultants"
    new_txns: list[_Txn] = []
    for yr, mo in _months(start, 3):
        amt = Decimal(str(random.randint(100, 300) * 1000))
        t = _make_txn(
            _rand_day(yr, mo, 10, 25),
            _narration(bank, "debit", related_entity),
            debit=amt,
            category="VENDOR_PAYMENT",
            is_related_party=True,
            related_party_match=related_entity,
            anomaly_flags=["related_party_leakage"],
        )
        new_txns.append(t)
    flag: dict[str, object] = {
        "flag_type": "related_party_leakage",
        "description": f"Monthly payments to '{related_entity}' — same surname as founder",
        "triggering_transaction_ids": [t.txn_id for t in new_txns],
        "related_entity": related_entity,
        "founder_surname": company.founder_surname,
    }
    return txns + new_txns, flag


_FLAG_INJECTORS = {
    "structuring": _inject_structuring,
    "round_tripping": _inject_round_tripping,
    "founder_extraction": _inject_founder_extraction,
    "customer_churn": _inject_customer_churn,
    "revenue_concentration": _inject_revenue_concentration,
    "related_party_leakage": _inject_related_party_leakage,
}

_GENERATORS = {
    "healthy_saas": _gen_healthy_saas,
    "burning_startup": _gen_burning_startup,
    "services_firm": _gen_services_firm,
    "ecommerce": _gen_ecommerce,
    "restaurant": _gen_restaurant,
}


# ─────────────────────────────────────────────────────────────────────────────
# Balance computation
# ─────────────────────────────────────────────────────────────────────────────

def _compute_balances(txns: list[_Txn], opening: Decimal) -> list[_Txn]:
    """Sort by date and compute running balance. Mutates in place, returns sorted list."""
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
# PDF rendering — HDFC format
# ─────────────────────────────────────────────────────────────────────────────

_HDFC_COLS = [42, 168, 78, 42, 62, 62, 72]  # points, total ≈ 526
_HDFC_HEADERS = ["Date", "Narration", "Chq./Ref.No.", "Value Dt",
                 "Withdrawal Amt.(Dr)", "Deposit Amt.(Cr)", "Closing Balance"]

_ICICI_COLS = [25, 48, 48, 155, 78, 55, 55, 62]  # points, total ≈ 526
_ICICI_HEADERS = ["S No.", "Transaction Date", "Value Date", "Description",
                  "Ref No./Cheque No.", "Debit", "Credit", "Balance"]

_HDR_BG = colors.HexColor("#1B3A6B")
_HDR_FG = colors.white
_ROW_ALT = colors.HexColor("#F2F5FA")
_GRID_CLR = colors.HexColor("#CCCCCC")


def _table_style(n_rows: int, has_sno: bool = False) -> TableStyle:
    cmds = [
        # Header
        ("BACKGROUND", (0, 0), (-1, 0), _HDR_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), _HDR_FG),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 6.5),
        ("ALIGN", (0, 0), (-1, 0), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("ROWBACKGROUND", (0, 1), (-1, -1), [colors.white, _ROW_ALT]),
        ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
        ("FONTSIZE", (0, 1), (-1, -1), 6.5),
        ("GRID", (0, 0), (-1, -1), 0.3, _GRID_CLR),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        # Right-align amount columns
        ("ALIGN", (-3, 1), (-1, -1), "RIGHT"),
    ]
    if has_sno:
        cmds.append(("ALIGN", (0, 0), (0, -1), "CENTER"))
    return TableStyle(cmds)


def _render_hdfc(company: _Company, txns: list[_Txn], opening: Decimal, path: Path) -> None:
    styles = getSampleStyleSheet()
    normal = styles["Normal"]
    normal.fontSize = 8

    period_start = txns[0].date if txns else date.today()
    period_end = txns[-1].date if txns else date.today()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )

    def _hdr_para(text: str) -> Paragraph:
        return Paragraph(text, styles["Normal"])

    story = [
        Paragraph("HDFC BANK", styles["Heading1"]),
        Paragraph("Account Statement", styles["Heading2"]),
        Spacer(1, 4 * mm),
        _hdr_para(f"<b>Account Holder:</b> {company.name}"),
        _hdr_para(f"<b>Account No:</b> {company.account_no}"),
        _hdr_para(f"<b>Branch:</b> {company.branch}"),
        _hdr_para(f"<b>IFSC:</b> {company.ifsc}"),
        _hdr_para(
            f"<b>Statement Period:</b> "
            f"{period_start.strftime('%d/%m/%Y')} To {period_end.strftime('%d/%m/%Y')}"
        ),
        Spacer(1, 4 * mm),
    ]

    # Opening balance row
    table_data: list[list[str]] = [_HDFC_HEADERS]
    table_data.append([
        period_start.strftime("%d/%m/%y"),
        "Opening Balance",
        "",
        period_start.strftime("%d/%m/%y"),
        "",
        _inr(opening),
        _inr(opening),
    ])

    for t in txns:
        table_data.append([
            t.date.strftime("%d/%m/%y"),
            t.description[:45],  # truncate long narrations
            t.ref_no,
            t.value_date.strftime("%d/%m/%y"),
            _inr(t.debit),
            _inr(t.credit),
            _inr(t.balance),
        ])

    # Closing balance summary
    closing = txns[-1].balance if txns else opening
    total_dr = sum(t.debit for t in txns if t.debit) or Decimal("0")
    total_cr = sum(t.credit for t in txns if t.credit) or Decimal("0")

    story.append(Table(
        table_data,
        colWidths=_HDFC_COLS,
        repeatRows=1,
        style=_table_style(len(table_data)),
    ))
    story += [
        Spacer(1, 4 * mm),
        _hdr_para(f"<b>Opening Balance:</b> {_inr(opening)}  "
                  f"<b>Total Withdrawals:</b> {_inr(total_dr)}  "
                  f"<b>Total Deposits:</b> {_inr(total_cr)}  "
                  f"<b>Closing Balance:</b> {_inr(closing)}"),
    ]

    doc.build(story)


def _render_icici(company: _Company, txns: list[_Txn], opening: Decimal, path: Path) -> None:
    styles = getSampleStyleSheet()

    period_start = txns[0].date if txns else date.today()
    period_end = txns[-1].date if txns else date.today()

    doc = SimpleDocTemplate(
        str(path),
        pagesize=A4,
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=MARGIN,
    )

    def _hdr_para(text: str) -> Paragraph:
        return Paragraph(text, styles["Normal"])

    story = [
        Paragraph("ICICI Bank", styles["Heading1"]),
        Paragraph("Account Statement", styles["Heading2"]),
        Spacer(1, 4 * mm),
        _hdr_para(f"<b>Name:</b> {company.name}"),
        _hdr_para(f"<b>Account Number:</b> {company.account_no}"),
        _hdr_para("<b>Account Type:</b> Current Account"),
        _hdr_para(f"<b>Branch:</b> {company.branch}"),
        _hdr_para(f"<b>IFSC Code:</b> {company.ifsc}"),
        _hdr_para(
            f"<b>Statement From:</b> {period_start.strftime('%d-%m-%Y')}  "
            f"<b>To:</b> {period_end.strftime('%d-%m-%Y')}"
        ),
        Spacer(1, 4 * mm),
    ]

    table_data: list[list[str]] = [_ICICI_HEADERS]
    # Opening balance
    table_data.append([
        "0",
        period_start.strftime("%d-%m-%Y"),
        period_start.strftime("%d-%m-%Y"),
        "Opening Balance",
        "",
        "",
        _inr(opening),
        _inr(opening),
    ])

    for idx, t in enumerate(txns, start=1):
        table_data.append([
            str(idx),
            t.date.strftime("%d-%m-%Y"),
            t.value_date.strftime("%d-%m-%Y"),
            t.description[:45],
            t.ref_no,
            _inr(t.debit),
            _inr(t.credit),
            _inr(t.balance),
        ])

    story.append(Table(
        table_data,
        colWidths=_ICICI_COLS,
        repeatRows=1,
        style=_table_style(len(table_data), has_sno=True),
    ))

    closing = txns[-1].balance if txns else opening
    story += [
        Spacer(1, 4 * mm),
        _hdr_para(f"<b>Closing Balance:</b> {_inr(closing)}"),
    ]

    doc.build(story)


# ─────────────────────────────────────────────────────────────────────────────
# Ground truth JSON builder
# ─────────────────────────────────────────────────────────────────────────────

def _build_truth(
    statement_id: str,
    bank: str,
    profile: str,
    company: _Company,
    txns: list[_Txn],
    injected_flags: list[dict[str, object]],
    opening: Decimal,
    path: Path,
) -> None:
    closing = txns[-1].balance if txns else opening
    revenue_txns = [t for t in txns if t.category == "REVENUE"]
    expense_txns = [t for t in txns if t.category not in ("REVENUE", "TRANSFER")]

    total_revenue = sum(t.credit for t in revenue_txns if t.credit) or Decimal("0")
    total_expenses = sum(t.debit for t in expense_txns if t.debit) or Decimal("0")

    # Monthly burn = average monthly non-transfer outflows
    months_covered = 3
    monthly_burn = (total_expenses / months_covered).quantize(Decimal("1"))
    runway = float(closing / monthly_burn) if monthly_burn > 0 else float("inf")

    customer_ids: set[str] = {
        t.customer_id for t in revenue_txns if t.customer_id
    }

    truth = {
        "statement_id": statement_id,
        "bank": bank,
        "profile": profile,
        "account_id": company.account_no,
        "period_start": txns[0].date.isoformat() if txns else "",
        "period_end": txns[-1].date.isoformat() if txns else "",
        "opening_balance": str(opening),
        "closing_balance": str(closing),
        "metrics": {
            "true_monthly_burn_rate": str(monthly_burn),
            "true_runway_months": round(runway, 1),
            "true_customer_count": len(customer_ids),
            "true_total_revenue": str(total_revenue.quantize(Decimal("1"))),
            "true_total_expenses": str(total_expenses.quantize(Decimal("1"))),
        },
        "injected_flags": injected_flags,
        "transactions": [
            {
                "transaction_id": t.txn_id,
                "date": t.date.isoformat(),
                "description": t.description,
                "debit": str(t.debit) if t.debit is not None else None,
                "credit": str(t.credit) if t.credit is not None else None,
                "balance": str(t.balance),
                "category": t.category,
                "customer_id": t.customer_id,
                "is_related_party": t.is_related_party,
                "related_party_match": t.related_party_match,
                "anomaly_flags": t.anomaly_flags,
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
    statement_id: str | None = None,
    flags: list[str] | None = None,
    n_months: int = 3,
    start: date | None = None,
    seed: int | None = None,
) -> tuple[Path, Path]:
    """Generate one synthetic bank statement PDF + paired ground truth JSON.

    Returns (pdf_path, truth_path).
    """
    if seed is not None:
        random.seed(seed)
    if start is None:
        start = date(2024, 4, 1)
    if statement_id is None:
        statement_id = f"{bank}_{profile}_{uuid.uuid4().hex[:6]}"

    company = _COMPANY_CONFIGS[profile]
    gen_fn = _GENERATORS[profile]
    txns = gen_fn(bank, company, start, n_months)

    injected_flags: list[dict[str, object]] = []
    for flag_name in (flags or []):
        if flag_name in _FLAG_INJECTORS:
            txns, flag_meta = _FLAG_INJECTORS[flag_name](txns, company, bank, start)
            injected_flags.append(flag_meta)

    txns = _compute_balances(txns, company.opening_balance)

    out_dir = output_dir / profile
    out_dir.mkdir(parents=True, exist_ok=True)

    pdf_path = out_dir / f"{statement_id}.pdf"
    truth_path = out_dir / f"{statement_id}.truth.json"

    renderer = _render_hdfc if bank == "hdfc" else _render_icici
    renderer(company, txns, company.opening_balance, pdf_path)
    _build_truth(statement_id, bank, profile, company, txns, injected_flags, company.opening_balance, truth_path)

    return pdf_path, truth_path


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate synthetic Indian bank statements")
    parser.add_argument("--bank", choices=["hdfc", "icici"], required=True)
    parser.add_argument("--profile", choices=list(_GENERATORS), required=True)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output", type=Path, default=Path("data/synthetic"))
    parser.add_argument("--flags", default="", help="comma-separated: structuring,round_tripping,...")
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    flag_list = [f.strip() for f in args.flags.split(",") if f.strip()]

    for i in range(args.count):
        seed = args.seed + i if args.seed is not None else None
        pdf, truth = generate_statement(
            bank=args.bank,
            profile=args.profile,
            output_dir=args.output,
            flags=flag_list,
            seed=seed,
        )
        print(f"[{i+1}/{args.count}] {pdf.name}  |  {truth.name}")

    print(f"\nDone — {args.count} statement(s) in {args.output / args.profile}/")


if __name__ == "__main__":
    main()
