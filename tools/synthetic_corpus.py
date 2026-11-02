"""Corpus-mode extensions for ``tools/synthetic_gen.py`` (UPGRADE_GUIDE Phase 3).

``synthetic_gen`` is not rewritten. It exposes a few opt-in hook points that are
inert unless ``synthetic_gen.corpus_mode(hooks)`` is active; with default
arguments every code path and every RNG draw of the original generator is
unchanged. This module supplies the hook object (:class:`CorpusHooks`) and the
per-company driver (:func:`generate_company`) used by ``datasets/build_corpus.py``.

What corpus mode adds, all of it SYNTHETIC:

* Per-company randomised parameters: company identity, revenue scale, customer
  count, vendor and employee names drawn from shared name universes.
* Narration diversity: several templates per bank x rail, random reference IDs,
  merchant-name variants, casing/spacing noise, truncation at bank-specific
  widths. Each row keeps a ``template_id`` (labels only) so evaluation can be
  grouped by template. This exists so that evaluation is not trivially
  template matching; the text is still produced by templates.
* Latent-health customer churn (B2B profiles): ``health ~ N(0,1)`` drives a
  monthly hazard; observable precursors (late payment, downgrade, skipped
  month) are noisy, partial proxies. Labels are never written into narrations.
* Three anomaly injectors that the rule detectors in ``analysis/risk.py`` don't
  target (``kind="ml_only"``), with guards that keep those detectors quiet.
* A ``txn_time`` for every row (needed by the off-hours injector, so time of
  day is present for all rows and is not itself a label).

Determinism: every random draw comes from ``random.Random`` instances seeded
from the company seed (plus the global ``random`` module, which the original
generator uses and :func:`generate_company` seeds). No ``uuid4``, no wall clock.
"""
from __future__ import annotations

import hashlib
import math
import random
import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from tools import synthetic_gen as sg

B2B_PROFILES: tuple[str, ...] = ("healthy_saas", "burning_startup", "services_firm")

DEFAULT_PROFILE_MIX: dict[str, float] = {
    "healthy_saas": 0.30,
    "burning_startup": 0.20,
    "services_firm": 0.20,
    "ecommerce": 0.15,
    "restaurant": 0.15,
}

# Max customer count per B2B profile (log-uniform from 5). SaaS reaches the
# guide's 200; a services firm with 200 enterprise clients at its ticket size
# would be implausible, so its ceiling is lower.
_MAX_CUSTOMERS = {"healthy_saas": 200, "burning_startup": 80, "services_firm": 40}

# ── Churn model parameters (see docs/ml/corpus_card.md) ─────────────────────
HAZARD_INTERCEPT = {"healthy_saas": -3.0, "services_firm": -2.6, "burning_startup": -2.2}
HAZARD_HEALTH_COEF = -1.0
HAZARD_TENURE_COEF = -0.35
HAZARD_NOISE_SD = 0.5
SIGNALLER_P = 0.55
DELAY_BASE_P = {"healthy_saas": 0.10, "services_firm": 0.20, "burning_startup": 0.08}
SKIP_BASE_P = {"healthy_saas": 0.03, "services_firm": 0.35, "burning_startup": 0.05}
WINDOW_DELAY_BOOST = 0.35
WINDOW_DOWNGRADE_P = 0.50
WINDOW_SKIP_BOOST = 0.25
PRE_PERIOD_SHARE = 0.60

ML_ONLY_FLAGS: tuple[str, ...] = (
    "ml_vendor_amount_outlier",
    "ml_first_time_large_counterparty",
    "ml_off_hours_burst",
)
ML_ONLY_P = 0.15

# Legacy injector excluded in corpus mode: latent churn is the single churn source.
EXCLUDED_RULE_FLAGS = frozenset({"customer_churn"})

# Row-level anomaly flag string -> kind (labels only; gold truth JSON is untouched).
ANOMALY_KIND: dict[str, str] = {
    "structuring": "rule",
    "round_tripping": "rule",
    "founder_over_extraction": "rule",
    "revenue_concentration": "rule",
    "related_party_leakage": "rule",
    "compliance_269st": "compliance",
    "compliance_gst_gap": "compliance",
    "compliance_tds_gap": "compliance",
    "compliance_pmla_cash": "compliance",
    "compliance_rpt_concentration": "compliance",
    "compliance_cash_loan": "compliance",
    "aggregator_settlement": "scenario",
    "inter_account_transfer": "scenario",
    "fx_inflow": "scenario",
    **{f: "ml_only" for f in ML_ONLY_FLAGS},
}

# ─────────────────────────────────────────────────────────────────────────────
# Name universes (fixed lists, no randomness at import time)
# ─────────────────────────────────────────────────────────────────────────────

_PREFIXES = [
    "Aarav", "Nimbus", "Zenith", "Vertex", "Saffron", "Indus", "Kaveri", "Lotus", "Peacock",
    "Sahyadri", "Ganga", "Vindhya", "Arka", "Aditya", "Neel", "Trident", "Orbit", "Pinnacle",
    "Quantum", "Helix", "Banyan", "Monsoon", "Deccan", "Coromandel", "Malabar", "Konark",
    "Ajanta", "Nalanda", "Agastya", "Utkarsh", "Pragati", "Samarth", "Shakti", "Tejas",
    "Vayu", "Agni", "Prithvi", "Akash", "Sagar", "Himalaya", "Narmada", "Godavari",
    "Krishna", "Tapti", "Chambal", "Yamuna", "Sutlej", "Beas", "Ravi", "Jhelum",
    "Brahma", "Meru", "Kailash", "Aravali", "Nilgiri", "Satpura", "Shivalik", "Garuda",
    "Mayur", "Hamsa",
]
_INDUSTRIES = [
    "Technologies", "Retail", "Logistics", "Foods", "Fintech", "Healthcare", "Infra",
    "Engineering", "Pharma", "Textiles", "Motors", "Analytics", "Media", "Edutech",
    "Agritech", "Industries", "Ventures", "Systems", "Networks", "Labs", "Consumer",
    "Apparel", "Electricals", "Polymers", "Chemicals", "Realty", "Hospitality", "Travels",
    "Exports", "Steel", "Packaging", "Biotech", "Software", "Digital", "Mobility",
    "Energy", "Solar", "Finserv", "Insurance Brokers", "Securities",
]
_LEGAL = ["Pvt Ltd", "Ltd", "LLP", "Private Limited", "Pvt Ltd", "Ltd"]

# Prefixes 0..44 feed customers/vendors; 45.. are held back for "never seen"
# counterparties used by the ml_first_time_large_counterparty injector.
_SEEN_PREFIXES = _PREFIXES[:45]
_NOVEL_PREFIXES = _PREFIXES[45:]
_NOVEL_TAILS = ["Trading Co", "Enterprises", "Commercial Agencies", "Impex", "Global Services"]

_FIRST = [
    "Aarti", "Abhay", "Aditi", "Ajay", "Akshay", "Alok", "Amrita", "Anil", "Anjali", "Ankit",
    "Arjun", "Asha", "Bhavna", "Chetan", "Deepak", "Divya", "Gaurav", "Geeta", "Harish",
    "Isha", "Jatin", "Jyoti", "Kamal", "Kavya", "Lalit", "Madhu", "Manoj", "Meera", "Mohit",
    "Nandini", "Naveen", "Nisha", "Pankaj", "Parul", "Prakash", "Preeti", "Rahul", "Rekha",
    "Rohit", "Sakshi", "Sandeep", "Shalini", "Sunil", "Swati", "Tanvi", "Uday", "Varun",
    "Vidya", "Yash", "Zoya",
]
_LAST = [
    "Agarwal", "Bhat", "Chopra", "Das", "Desai", "Ghosh", "Hegde", "Iyer", "Jain", "Kapoor",
    "Kulkarni", "Malhotra", "Mehta", "Mishra", "Nair", "Pandey", "Pillai", "Rao", "Reddy",
    "Saxena", "Sen", "Shetty", "Sinha", "Srinivasan", "Thakur", "Trivedi", "Varma", "Yadav",
    "Bose", "Chauhan", "Dutta", "Fernandes", "Gill", "Joshi", "Khan", "Menon", "Naidu",
    "Patil", "Rastogi", "Tiwari",
]
_CITIES = [
    ("Bengaluru", "Indiranagar"), ("Mumbai", "Andheri East"), ("Delhi", "Nehru Place"),
    ("Pune", "Baner"), ("Hyderabad", "Madhapur"), ("Chennai", "T Nagar"),
    ("Gurugram", "Udyog Vihar"), ("Noida", "Sector 62"), ("Ahmedabad", "SG Highway"),
    ("Kolkata", "Salt Lake"), ("Jaipur", "C Scheme"), ("Kochi", "Kakkanad"),
]

# Vendor kind per original generator vendor name. Names not listed here
# (related parties, round-trip counterparties, platform fees) are never remapped.
_ORIGINAL_KIND: dict[str, str] = {
    **{n: "saas_tool" for n, _ in sg._TECH_VENDORS},
    "Amazon Web Services India Pvt Ltd": "cloud",
    "Microsoft Azure India Pvt Ltd": "cloud",
    "Google Cloud India Pvt Ltd": "cloud",
    **{n: "cowork" for n, _ in sg._OFFICE_VENDORS},
    "Performance Marketing Agency India Ltd": "marketing",
    "Khaitan & Co Legal Advisory": "legal",
    "Travel Connections India Ltd": "travel",
    **{n: "food" for n in sg._FOOD_VENDORS},
    **{n: "logistics" for n in sg._LOGISTICS_VENDORS},
    "Flipkart Commerce Solutions Pvt Ltd": "marketplace",
    "Amazon Seller Services Pvt Ltd": "marketplace",
    "Myntra Designs Pvt Ltd": "marketplace",
    "Koregaon Park Property Trust": "landlord",
    "MSEDCL Electricity Board": "utility",
}

# Constant counterparty names used by the legacy injectors. Left as-is they would
# be identical in every company, so a model could memorise "this name = anomaly";
# corpus mode remaps each one per company (customer- or vendor-like name).
_INJECTED_PARTIES: dict[str, str] = {
    "Hyperion Mega Corp Ltd": "customer",          # revenue_concentration whale
    "Enterprise Software Sales": "customer",       # compliance_gst_gap revenue
    "Zenith Capital Advisory LLP": "advisory",     # round_tripping counterparty
    "Director Personal Account": "person",         # compliance_cash_loan lender
}

_KIND_TAILS: dict[str, list[str]] = {
    "advisory": ["Capital Advisors LLP", "Finance Consultants Pvt Ltd", "Advisory Services LLP"],
    "cloud": ["Cloud Services Pvt Ltd", "Datacenters Ltd", "Hosting Pvt Ltd"],
    "saas_tool": ["Software Pvt Ltd", "SaaS Labs Pvt Ltd", "Cloud Apps LLP", "Tech Pvt Ltd"],
    "cowork": ["Workspaces Pvt Ltd", "Coworking LLP", "Business Centres Ltd"],
    "marketing": ["Media Pvt Ltd", "Digital Marketing LLP", "Adworks Pvt Ltd"],
    "legal": ["Legal Associates", "& Partners Advocates", "Law Chambers LLP"],
    "travel": ["Tours & Travels", "Travel Desk Pvt Ltd", "Holidays Pvt Ltd"],
    "food": ["Agro Foods Pvt Ltd", "Dairy Products Ltd", "Fresh Produce LLP", "Spices Pvt Ltd"],
    "logistics": ["Logistics Pvt Ltd", "Express Cargo Ltd", "Couriers LLP"],
    "marketplace": ["Ecommerce Services Pvt Ltd", "Marketplace Solutions Ltd"],
    "landlord": ["Estates", "Properties LLP", "Realty Trust"],
    "utility": ["Power Distribution Ltd", "Electricity Board", "Utilities Ltd"],
}


def _vendor_pool(kind: str) -> list[str]:
    tails = _KIND_TAILS[kind]
    return [f"{p} {t}" for p in _SEEN_PREFIXES for t in tails]


_VENDOR_POOLS: dict[str, list[str]] = {k: _vendor_pool(k) for k in _KIND_TAILS}
_CUSTOMER_UNIVERSE: list[str] = [
    f"{p} {ind} {_LEGAL[(i + j) % len(_LEGAL)]}"
    for i, p in enumerate(_SEEN_PREFIXES)
    for j, ind in enumerate(_INDUSTRIES)
]
_NOVEL_VENDORS: list[str] = [f"{p} {t}" for p in _NOVEL_PREFIXES for t in _NOVEL_TAILS]


def normalise_key(name: str) -> str:
    """Canonical counterparty key (labels only): upper case, alnum + single spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", name.upper())).strip()


# ─────────────────────────────────────────────────────────────────────────────
# Narration templates
# ─────────────────────────────────────────────────────────────────────────────
# (rail, format). Fields: P party, INV invoice no, BC bank code+digits, UTR,
# IFSC, REF12, H8 hex, VPA, PSP, BK bank short name, L4 last-4, GSTIN, TAN, BSR,
# CPIN, LOAN.
_T = tuple[str, str]
_TEMPLATES: dict[str, dict[str, list[_T]]] = {
    "hdfc": {
        "credit": [
            ("neft", "NEFT/{BC}/{P}/INV{INV:06d}"),
            ("neft", "NEFT CR-{IFSC}-{P}-{UTR}"),
            ("neft", "NEFTCR/{UTR}/{P}"),
            ("rtgs", "RTGS CR-{IFSC}-{P}-{UTR}"),
            ("imps", "IMPS-{REF12}-{P}-{BK}-XXXXXXXX{L4}-INV{INV}"),
            ("upi", "UPI-{P}-{VPA}@{PSP}-{REF12}-PAYMENT FROM PHONE"),
        ],
        "debit": [
            ("neft", "NEFT/{BC}/{P}/{H8}"),
            ("neft", "NEFT DR-{IFSC}-{P}-NETBANK, MUM-{UTR}"),
            ("rtgs", "RTGS DR-{IFSC}-{P}-{UTR}"),
            ("imps", "IMPS-{REF12}-{P}-{BK}-XXXXXXXX{L4}"),
            ("ib", "IB FUNDS TRANSFER DR-{L4}{L4}-{P}"),
        ],
        "salary": [
            ("ecs", "ECS/SALARY/{P}/{H8}"),
            ("neft", "NEFT DR-{IFSC}-{P}-SALARY {REF12}"),
            ("ach", "ACH D- SALARY-{P}-{REF12}"),
            ("ecs", "BULK SAL PAYOUT/{P}/{H8}"),
            ("imps", "IMPS-{REF12}-{P}-{BK}-XXXXXXXX{L4}-SAL"),
        ],
        "cash": [
            ("cash", "CASH DEP-{P}"),
            ("cash", "BY CASH-{P}"),
            ("cash", "CASH DEPOSIT BY SELF-{P}"),
            ("cash", "CSH DEP {P}"),
        ],
        "gst": [
            ("tax", "NEFT/{BC}/GST PAYMENT/GSTIN{GSTIN}"),
            ("tax", "GST PMT-CPIN{CPIN}-{UTR}"),
            ("tax", "IB TAX PAYMENT GSTIN{GSTIN}"),
            ("tax", "NEFT DR-{IFSC}-GST PAYMENT-{UTR}"),
        ],
        "tds": [
            ("tax", "NEFT/{BC}/TDS PAYMENT/TAN{TAN}"),
            ("tax", "CBDT TDS PAYMENT-TAN{TAN}-{REF12}"),
            ("tax", "IB TDS PAYMENT/{CPIN}"),
        ],
        "advance_tax": [
            ("tax", "NEFT/{BC}/INCOME TAX PAYMENT/BSR{BSR}"),
            ("tax", "CBDT INCOME TAX PAYMENT-BSR{BSR}"),
            ("tax", "ADV TAX CHALLAN 280-{REF12}"),
        ],
        "loan": [
            ("neft", "NEFT/{BC}/{P}/LOAN{LOAN}"),
            ("neft", "NEFT CR-{IFSC}-{P}-LOAN A/C {LOAN}"),
        ],
        "cash_loan_in": [
            ("cash", "CASH DEP-LOAN RECEIVED/{P}"),
            ("cash", "BY CASH-LOAN FROM {P}"),
        ],
        "cash_loan_out": [
            ("cash", "CASH WITH-LOAN REPAID/{P}"),
            ("cash", "CASH WDL-LOAN REPAYMENT {P}"),
        ],
        "founder": [
            ("neft", "NEFT/{BC}/{P} PERSONAL/{H8}"),
            ("imps", "IMPS-{REF12}-{P}-{BK}-XXXXXXXX{L4}-SELF"),
            ("neft", "NEFT DR-{IFSC}-{P}-OWN USE-{UTR}"),
        ],
        "razorpay": [
            ("neft", "NEFT/{BC}/RAZORPAY SOFTWARE PVT LTD/SETL{INV:06d}"),
            ("neft", "NEFT CR-{IFSC}-RAZORPAY SOFTWARE PRIVATE LIMITED-{UTR}"),
            ("ach", "ACH C- RAZORPAY SETTLEMENT-{REF12}"),
            ("neft", "RAZORPAY PAYMENTS SETL {INV}"),
        ],
        "upi_debit": [
            ("upi", "UPI-{P}-{VPA}@{PSP}-{REF12}-PAY"),
            ("upi", "UPI/DR/{REF12}/{P}/{PSP}"),
            ("imps", "IMPS-{REF12}-{P}-{BK}-XXXXXXXX{L4}"),
        ],
    },
    "icici": {
        "credit": [
            ("neft", "NEFT CR-{BC}-{P}-INV{INV:06d}"),
            ("neft", "NEFT-{UTR}-{P}-{BK}"),
            ("rtgs", "RTGS/{UTR}/{P}"),
            ("imps", "MMT/IMPS/{REF12}/{P}/{BK}"),
            ("upi", "UPI/{REF12}/{P}/{VPA}@{PSP}/PAYMENT"),
            ("neft", "INF/NEFT/{UTR}/{P}"),
        ],
        "debit": [
            ("neft", "NEFT DR-{BC}-{P}"),
            ("neft", "NEFT-{UTR}-{P}-{BK}"),
            ("rtgs", "RTGS/{UTR}/{P}"),
            ("imps", "MMT/IMPS/{REF12}/{P}/{BK}"),
            ("ib", "BIL/ONL/{REF12}/{P}"),
        ],
        "salary": [
            ("ecs", "ECS DR-SALARY-{P}-{H8}"),
            ("neft", "NEFT-{UTR}-SALARY {P}"),
            ("ach", "ACH/SALARY/{P}/{REF12}"),
            ("imps", "MMT/IMPS/{REF12}/{P}/SAL"),
        ],
        "cash": [
            ("cash", "CASH DEPOSIT-{P}-{H8}"),
            ("cash", "BY CASH DEPOSIT {P}"),
            ("cash", "CAM/CASH DEP/{P}/{REF12}"),
        ],
        "gst": [
            ("tax", "NEFT DR-CBSB{L4}1-GST PMT-{GSTIN}"),
            ("tax", "GST PAYMENT/CPIN {CPIN}"),
            ("tax", "BIL/ONL/{REF12}/GSTIN{GSTIN}"),
        ],
        "tds": [
            ("tax", "NEFT DR-CBSB{L4}1-TDS PAYMENT-TAN{TAN}"),
            ("tax", "BIL/ONL/{REF12}/TDS PAYMENT {TAN}"),
            ("tax", "TDS PAYMENT/CHALLAN {CPIN}"),
        ],
        "advance_tax": [
            ("tax", "NEFT DR-CBSB{L4}1-INCOME TAX PAYMENT-BSR{BSR}"),
            ("tax", "BIL/ONL/{REF12}/ADVANCE TAX {BSR}"),
        ],
        "loan": [
            ("neft", "NEFT CR-{BC}-{P}-LOAN{LOAN}"),
            ("neft", "INF/NEFT/{UTR}/{P} LOAN"),
        ],
        "cash_loan_in": [
            ("cash", "CASH DEPOSIT-LOAN RECEIPT/{P}-{H8}"),
            ("cash", "BY CASH DEPOSIT LOAN {P}"),
        ],
        "cash_loan_out": [
            ("cash", "CASH WITHDRAWAL-LOAN REPAID/{P}-{H8}"),
            ("cash", "CAM/CASH WDL/LOAN {P}"),
        ],
        "founder": [
            ("neft", "NEFT DR-{BC}-{P} PERSONAL"),
            ("imps", "MMT/IMPS/{REF12}/{P}/SELF"),
            ("neft", "NEFT-{UTR}-{P}-OWN ACCOUNT"),
        ],
        "razorpay": [
            ("neft", "NEFT CR-{BC}-RAZORPAY SOFTWARE PVT LTD-SETL{INV:06d}"),
            ("neft", "INF/NEFT/{UTR}/RAZORPAY SOFTWARE PRIVATE LIMITED"),
            ("ach", "ACH/RAZORPAY SETTLEMENT/{REF12}"),
        ],
        "upi_debit": [
            ("upi", "UPI/{REF12}/{P}/{VPA}@{PSP}/PAY"),
            ("upi", "UPI/DR/{REF12}/{P}"),
            ("imps", "MMT/IMPS/{REF12}/{P}/{BK}"),
        ],
    },
}
_TEMPLATES["hdfc"]["vendor"] = _TEMPLATES["hdfc"]["debit"]
_TEMPLATES["icici"]["vendor"] = _TEMPLATES["icici"]["debit"]

_NARRATION_WIDTH = {"hdfc": 60, "icici": 80}
_BANK_SHORT = ["HDFC", "ICIC", "SBIN", "UTIB", "KKBK", "YESB", "PUNB", "BARB", "IDFB", "INDB"]
_PSPS = ["okhdfcbank", "okicici", "oksbi", "okaxis", "ybl", "paytm", "ibl", "axl"]

_TAX_KEYS = {"gst": "TAX GST", "tds": "TAX TDS", "advance_tax": "TAX ADVANCE"}

# Inline narrations (built in synthetic_gen without _narration): (regex, rail, key, kind).
_INLINE = [
    (re.compile(r"^UPI/CR/\d+/SWIGGY"), "upi", "SWIGGY", "upi_swiggy"),
    (re.compile(r"^UPI/CR/\d+/ZOMATO"), "upi", "ZOMATO", "upi_zomato"),
    (re.compile(r"^CASH DEP(OSIT)?-"), "cash", "CASH", "cash"),
    (re.compile(r"^(SWIFT CR|INWARD REMITTANCE)"), "swift", "FX REMITTANCE", "fx"),
    (re.compile(r"(OWN ACCOUNT|SELF) TRANSFER"), "neft", "SELF TRANSFER", "self_transfer"),
]

_LEGAL_RE = re.compile(r"\b(PVT\.? LTD\.?|PRIVATE LIMITED)$")


def _party_variants(name: str) -> list[str]:
    up = name.upper()
    out = [up]
    if _LEGAL_RE.search(up):
        base = _LEGAL_RE.sub("", up).strip()
        out += [f"{base} PVT LTD", f"{base} PRIVATE LIMITED", f"{base} PVT. LTD.", base]
    if "TECHNOLOGIES" in up:
        out.append(up.replace("TECHNOLOGIES", "TECH"))
    if " & " in up:
        out.append(up.replace(" & ", " AND "))
    words = up.split()
    if len(words) >= 3:
        out.append(" ".join(words[:2]))
    seen: dict[str, None] = {}
    for v in out:
        seen.setdefault(v, None)
    return list(seen)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _sub_seed(seed: int, purpose: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{seed}|{purpose}".encode()).digest()[:8], "big")


def _last_day(yr: int, mo: int) -> date:
    nxt = date(yr + (mo == 12), mo % 12 + 1, 1)
    return nxt - timedelta(days=1)


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


# ─────────────────────────────────────────────────────────────────────────────
# Company spec and latent customers
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CompanySpec:
    index: int
    company_id: str
    seed: int
    profile: str
    bank: str
    n_months: int
    start: date
    revenue_scale: float
    n_customers: int          # 0 for aggregated-revenue profiles
    ml_flags: tuple[str, ...]


def make_spec(
    index: int,
    master_seed: int,
    *,
    profile_mix: dict[str, float] | None = None,
    max_months: int = 24,
    enable_ml_only: bool = True,
) -> CompanySpec:
    """Draw one company's parameters from a seed derived from (master_seed, index)."""
    seed = _sub_seed(master_seed, f"company|{index}")
    rng = random.Random(_sub_seed(seed, "spec"))
    mix = profile_mix or DEFAULT_PROFILE_MIX
    profiles = sorted(mix)
    profile = rng.choices(profiles, weights=[mix[p] for p in profiles])[0]
    bank = rng.choice(["hdfc", "icici"])
    n_months = rng.randint(min(12, max_months), max_months)
    start_offset = rng.randint(0, 32)          # 2022-04 .. 2024-12
    start = _add_months(date(2022, 4, 1), start_offset)
    revenue_scale = round(math.exp(rng.gauss(0.0, 0.5)), 4)
    if profile in B2B_PROFILES:
        hi = _MAX_CUSTOMERS[profile]
        n_customers = int(round(math.exp(rng.uniform(math.log(5), math.log(hi)))))
    else:
        n_customers = 0
    ml_flags = tuple(f for f in ML_ONLY_FLAGS if rng.random() < ML_ONLY_P) if enable_ml_only else ()
    cid = "co_" + hashlib.sha256(f"{master_seed}|{index}".encode()).hexdigest()[:10]
    return CompanySpec(index, cid, seed, profile, bank, n_months, start, revenue_scale,
                       n_customers, ml_flags)


@dataclass
class CustomerLatent:
    customer_id: str
    name: str
    health: float
    acquired_month: int            # month index; negative = before the statement period
    churn_month: int | None        # first month index with no payment due (absorbing)
    signaller: bool
    lead_months: int
    delay_days: list[int] = field(default_factory=list)     # per month, 0 = on time
    multiplier: list[float] = field(default_factory=list)   # cumulative downgrade
    skip: list[bool] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Hooks object
# ─────────────────────────────────────────────────────────────────────────────

class CorpusHooks:
    """Hook implementation consulted by synthetic_gen while corpus_mode is active."""

    def __init__(self, spec: CompanySpec) -> None:
        self.spec = spec
        s = spec.seed
        self.rng_setup = random.Random(_sub_seed(s, "setup"))
        self.rng_ids = random.Random(_sub_seed(s, "ids"))
        self.rng_narr = random.Random(_sub_seed(s, "narr"))
        self.rng_time = random.Random(_sub_seed(s, "time"))
        self.rng_churn = random.Random(_sub_seed(s, "churn"))
        self.rng_ml = random.Random(_sub_seed(s, "ml"))
        self.period_end = _last_day(*sg._months(spec.start, spec.n_months)[-1])
        self.company = self._make_company()
        self._vendor_map: dict[str, str] = {}
        self._person_map: dict[str, str] = {}
        self._party_form: dict[str, str] = {}
        self._pending: tuple[str, str, str, str] | None = None   # desc, template_id, key, rail
        self.customer_list: list[tuple[str, str]] = []
        self.latents: dict[str, CustomerLatent] = {}
        if spec.profile in B2B_PROFILES:
            names = self.rng_setup.sample(_CUSTOMER_UNIVERSE, spec.n_customers)
            self.customer_list = [(f"cust_{i + 1:03d}", n) for i, n in enumerate(names)]
            self._simulate_churn()

    # ── identity ────────────────────────────────────────────────────────────
    def _make_company(self) -> sg._Company:
        r = self.rng_setup
        city, area = r.choice(_CITIES)
        first, last = r.choice(_FIRST), r.choice(_LAST)
        name = f"{r.choice(_SEEN_PREFIXES)} {r.choice(_INDUSTRIES)} {r.choice(_LEGAL)}"
        bank_code = "HDFC" if self.spec.bank == "hdfc" else "ICIC"
        default_opening = sg._COMPANY_CONFIGS[self.spec.profile].opening_balance
        opening = (default_opening * Decimal(str(self.spec.revenue_scale))
                   * Decimal(str(round(r.uniform(0.5, 2.0), 3)))).quantize(Decimal("1"))
        return sg._Company(
            name=name,
            account_no="50100" + str(r.randint(100_000_000, 999_999_999)),
            branch=f"{city} {area}",
            ifsc=f"{bank_code}000{r.randint(1000, 9999)}",
            founder_name=first,
            founder_surname=last,
            opening_balance=opening,
        )

    # ── deterministic ids / dates ───────────────────────────────────────────
    def uuid_hex(self) -> str:
        return f"{self.rng_ids.getrandbits(128):032x}"

    def txn_id(self) -> str:
        return f"txn_{self.uuid_hex()[:16]}"

    def today(self) -> date:
        return self.spec.start

    # ── customers and churn ─────────────────────────────────────────────────
    def customers(self, default: list[tuple[str, str]]) -> list[tuple[str, str]]:
        return self.customer_list if self.spec.profile in B2B_PROFILES else default

    def _simulate_churn(self) -> None:
        p = self.spec.profile
        n = self.spec.n_months
        r = self.rng_churn
        for cid, name in self.customer_list:
            health = r.gauss(0.0, 1.0)
            if r.random() < PRE_PERIOD_SHARE:
                acquired = -r.randint(1, 24)
            else:
                acquired = r.randint(1, max(1, n - 1))
            churn: int | None = None
            for t in range(max(0, acquired + 1), n):
                tenure = t - acquired
                eps = r.gauss(0.0, HAZARD_NOISE_SD)
                lam = _sigmoid(HAZARD_INTERCEPT[p] + HAZARD_HEALTH_COEF * health
                               + HAZARD_TENURE_COEF * math.log1p(tenure) + eps)
                if r.random() < lam:
                    churn = t
                    break
            signaller = churn is not None and r.random() < SIGNALLER_P
            lead = r.randint(1, 3)
            lat = CustomerLatent(cid, name, health, acquired, churn, signaller, lead)
            sig_low = _sigmoid(-health)
            mult = 1.0
            for t in range(n):
                in_window = bool(signaller and churn is not None and churn - lead <= t < churn)
                # Draw every variable every month so the stream doesn't depend on branches.
                u_delay, d_days = r.random(), r.randint(10, 40)
                u_down, f_down = r.random(), r.uniform(0.70, 0.95)
                u_skip = r.random()
                p_delay = DELAY_BASE_P[p] * (1.0 + sig_low) + (WINDOW_DELAY_BOOST if in_window else 0.0)
                p_down = WINDOW_DOWNGRADE_P if in_window else 0.03 + 0.03 * sig_low
                p_skip = SKIP_BASE_P[p] + (WINDOW_SKIP_BOOST if in_window else 0.0)
                lat.delay_days.append(d_days if u_delay < p_delay else 0)
                if u_down < p_down:
                    mult *= f_down
                lat.multiplier.append(mult)
                lat.skip.append(u_skip < p_skip and t != acquired)
            self.latents[cid] = lat

    def customer_payment(
        self, cid: str, month_idx: int, amount: Decimal, yr: int, mo: int
    ) -> tuple[Decimal, date] | None:
        lat = self.latents.get(cid)
        if lat is None:
            return None
        if month_idx < lat.acquired_month:
            return None
        if lat.churn_month is not None and month_idx >= lat.churn_month:
            return None
        if lat.skip[month_idx]:
            return None
        amt = (amount * Decimal(str(round(lat.multiplier[month_idx], 4)))).quantize(Decimal("1"))
        day = date(yr, mo, self.rng_churn.randint(3, 25))
        pay = min(day + timedelta(days=lat.delay_days[month_idx]), self.period_end)
        return amt, pay

    # ── narration ───────────────────────────────────────────────────────────
    def _map_party(self, mode: str, party: str) -> str:
        if mode == "salary":
            if party not in self._person_map:
                r = self.rng_setup
                self._person_map[party] = f"{r.choice(_FIRST)} {r.choice(_LAST)}"
            return self._person_map[party]
        inj = _INJECTED_PARTIES.get(party)
        if inj is not None:
            if party not in self._vendor_map:
                r = self.rng_setup
                if inj == "customer":
                    self._vendor_map[party] = r.choice(_CUSTOMER_UNIVERSE)
                elif inj == "person":
                    self._vendor_map[party] = f"{r.choice(_FIRST)} {r.choice(_LAST)}"
                else:
                    self._vendor_map[party] = r.choice(_VENDOR_POOLS[inj])
            return self._vendor_map[party]
        kind = _ORIGINAL_KIND.get(party)
        if kind is not None:
            if party not in self._vendor_map:
                r = self.rng_setup
                used = set(self._vendor_map.values())
                if r.random() < 0.3 and party not in used:
                    self._vendor_map[party] = party
                else:
                    choices = [v for v in _VENDOR_POOLS[kind] if v not in used]
                    self._vendor_map[party] = r.choice(choices)
            return self._vendor_map[party]
        return party

    def _render_party(self, party: str) -> str:
        r = self.rng_narr
        variants = _party_variants(party)
        if party not in self._party_form:
            self._party_form[party] = r.choice(variants)
        return r.choice(variants) if r.random() < 0.15 else self._party_form[party]

    def narration(self, bank: str, mode: str, party: str, inv: int = 0) -> str:
        r = self.rng_narr
        templates = _TEMPLATES[bank].get(mode)
        canonical = self._map_party(mode, party)
        if mode in _TAX_KEYS:
            key = _TAX_KEYS[mode]
        elif mode == "razorpay":
            key = "RAZORPAY"
        elif mode == "salary":
            key = "EMPLOYEE " + normalise_key(canonical)
        else:
            key = normalise_key(canonical) if canonical else mode.upper()
        if not templates:
            desc = f"TXN/{canonical.upper()}/{self.uuid_hex()[:8].upper()}"
            self._pending = (desc, f"{bank}:{mode}:fallback", key, "other")
            return desc
        k = r.randrange(len(templates))
        rail, fmt = templates[k]
        fields: dict[str, Any] = {
            "P": self._render_party(canonical) if canonical else "",
            "INV": inv,
            "BC": r.choice(_BANK_SHORT) + str(r.randint(1_000_000, 9_999_999)),
            "UTR": f"{r.choice(_BANK_SHORT)}N{r.randint(10**11, 10**12 - 1)}",
            "IFSC": f"{r.choice(_BANK_SHORT)}0{r.randint(100000, 999999)}",
            "REF12": str(r.randint(10**11, 10**12 - 1)),
            "H8": f"{r.getrandbits(32):08X}",
            "VPA": (canonical.split()[0].lower() if canonical else "pay") + str(r.randint(1, 999)),
            "PSP": r.choice(_PSPS),
            "BK": r.choice(_BANK_SHORT),
            "L4": str(r.randint(1000, 9999)),
            "GSTIN": str(r.randint(10**14, 10**15 - 1)),
            "TAN": str(r.randint(10**8, 10**9 - 1)),
            "BSR": str(r.randint(10**6, 10**7 - 1)),
            "CPIN": str(r.randint(10**13, 10**14 - 1)),
            "LOAN": str(r.randint(1000, 9999)),
        }
        desc = fmt.format(**fields)
        # Noise: casing / spacing, then the bank's narration column width.
        u = r.random()
        if u < 0.05:
            desc = desc.title()
        elif u < 0.10:
            desc = desc.replace("-", " - ", 1) if "-" in desc else desc.replace(" ", "  ", 1)
        desc = desc[: _NARRATION_WIDTH[bank]].rstrip()
        self._pending = (desc, f"{bank}:{mode}:{k}", key, rail)
        return desc

    # ── per-transaction metadata ────────────────────────────────────────────
    def _time_for(self, rail: str) -> str:
        r = self.rng_time
        if rail in ("upi", "imps", "card", "other"):
            if r.random() < 0.03:
                secs = r.randint(0, 6 * 3600 - 1)
            else:
                secs = int(min(max(r.gauss(14 * 3600, 3.5 * 3600), 6 * 3600), 23 * 3600 + 3599))
        elif rail == "cash":
            secs = r.randint(10 * 3600, 16 * 3600)
        else:   # neft / rtgs / ecs / ach / tax / ib / swift: batch windows in banking hours
            secs = r.choice([8, 10, 12, 14, 16, 18]) * 3600 + r.randint(0, 3599)
        return f"{secs // 3600:02d}:{secs % 3600 // 60:02d}:{secs % 60:02d}"

    def on_txn(self, txn: sg._Txn) -> None:
        pend, self._pending = self._pending, None
        if pend is not None and pend[0] == txn.description:
            _, txn.template_id, txn.counterparty_key, rail = pend
        else:
            rail, key, kind = "other", "OTHER", "other"
            for rx, r_rail, r_key, r_kind in _INLINE:
                if rx.search(txn.description):
                    rail, key, kind = r_rail, r_key, r_kind
                    break
            txn.template_id = f"{self.spec.bank}:inline:{kind}"
            txn.counterparty_key = key
        txn.txn_time = self._time_for(rail)

    @staticmethod
    def sort_key(t: sg._Txn) -> tuple[date, str, str]:
        return (t.date, t.txn_time or "", t.txn_id)


# ─────────────────────────────────────────────────────────────────────────────
# Revenue-scale post-pass
# ─────────────────────────────────────────────────────────────────────────────

def _scale_amount(amount: Decimal, scale: Decimal) -> Decimal:
    for q in (Decimal("1000"), Decimal("100"), Decimal("1")):
        if amount % q == 0:
            return ((amount * scale) / q).quantize(Decimal("1")) * q or q
    return (amount * scale).quantize(Decimal("0.01"))


def _apply_scale(txns: list[sg._Txn], scale: float) -> None:
    s = Decimal(str(scale))
    for t in txns:
        if t.debit is not None:
            t.debit = _scale_amount(t.debit, s)
        if t.credit is not None:
            t.credit = _scale_amount(t.credit, s)


# ─────────────────────────────────────────────────────────────────────────────
# ml_only injectors
# ─────────────────────────────────────────────────────────────────────────────

def _monthly_net(txns: list[sg._Txn]) -> dict[str, Decimal]:
    net: dict[str, Decimal] = defaultdict(Decimal)
    for t in txns:
        ym = t.date.strftime("%Y-%m")
        if t.credit is not None:
            net[ym] += t.credit
        elif t.debit is not None:
            net[ym] -= t.debit
    return dict(net)


def month_z(txns: list[sg._Txn]) -> dict[str, float]:
    """Leave-one-out z of each month's net flow (as analysis.risk.detect_spikes_drains)."""
    net = _monthly_net(txns)
    if len(net) < 3:
        return {}
    months = sorted(net)
    vals = [float(net[m]) for m in months]
    out: dict[str, float] = {}
    for i, m in enumerate(months):
        base = [v for j, v in enumerate(vals) if j != i]
        sd = statistics.pstdev(base)
        if sd > 0.0:
            out[m] = (vals[i] - statistics.mean(base)) / sd
    return out


def spike_months(txns: list[sg._Txn]) -> set[str]:
    """Months the spike_drain rule would flag (mirror of analysis.risk.detect_spikes_drains)."""
    from config.thresholds import get_thresholds

    z_thr = get_thresholds().risk.spike_z
    return {m for m, z in month_z(txns).items() if abs(z) > z_thr}


# Touched months must stay this far below the spike_drain threshold (2.5).
GUARD_MAX_Z = 2.0


def _odd_amount(r: random.Random, target: float) -> Decimal:
    """Non-round amount near target: never a multiple of ₹1,000, always with paise."""
    rupees = max(int(target), 1001)
    if rupees % 1000 == 0:
        rupees += r.randint(1, 999)
    return Decimal(rupees) + Decimal(r.randint(1, 99)) / Decimal(100)


def _guarded(base: list[sg._Txn], new: list[sg._Txn]) -> bool:
    """Rule-evasion guard for spike_drain: adding `new` must leave the set of
    flagged months unchanged, the touched month(s) must not have been flagged
    before, and their leave-one-out |z| must stay <= GUARD_MAX_Z afterwards."""
    before = spike_months(base)
    touched = {t.date.strftime("%Y-%m") for t in new}
    if touched & before or spike_months(base + new) != before:
        return False
    z_after = month_z(base + new)
    return all(abs(z_after.get(m, 0.0)) <= GUARD_MAX_Z for m in touched)


_InjectResult = tuple[list[sg._Txn], dict[str, Any] | None]


def _inject_ml_vendor_amount_outlier(
    txns: list[sg._Txn], company: sg._Company, bank: str, start: date, hooks: CorpusHooks
) -> _InjectResult:
    r = hooks.rng_ml
    by_vendor: dict[str, list[sg._Txn]] = defaultdict(list)
    for t in txns:
        if (t.debit is not None and t.category == "VENDOR_PAYMENT" and not t.is_related_party
                and not t.anomaly_flags and t.counterparty_key):
            by_vendor[t.counterparty_key].append(t)
    candidates = sorted(k for k, v in by_vendor.items() if len(v) >= 4)
    r.shuffle(candidates)
    for key in candidates[:10]:
        hist = by_vendor[key]
        med = float(statistics.median(float(t.debit) for t in hist if t.debit is not None))
        amount_target = med * r.uniform(3.0, 6.0)
        months = sorted({(t.date.year, t.date.month) for t in txns})
        yr, mo = months[r.randrange(len(months) // 2, len(months))]
        dom = int(statistics.median(t.date.day for t in hist))
        day = min(max(dom + r.randint(-3, 3), 1), 28)
        party = hist[0].counterparty_key or key
        t = sg._make_txn(date(yr, mo, day), sg._narration(bank, "vendor", party),
                         debit=_odd_amount(r, amount_target), category="VENDOR_PAYMENT",
                         anomaly_flags=["ml_vendor_amount_outlier"])
        t.counterparty_key = key
        if _guarded(txns, [t]):
            flag = {"flag_type": "ml_vendor_amount_outlier", "kind": "ml_only",
                    "description": f"Payment ~{amount_target / med:.1f}x the vendor's median",
                    "triggering_transaction_ids": [t.txn_id]}
            return txns + [t], flag
    return txns, None


def _inject_ml_first_time_large_counterparty(
    txns: list[sg._Txn], company: sg._Company, bank: str, start: date, hooks: CorpusHooks
) -> _InjectResult:
    r = hooks.rng_ml
    debits = sorted(float(t.debit) for t in txns if t.debit is not None)
    if len(debits) < 20:
        return txns, None
    p95 = debits[int(0.95 * (len(debits) - 1))]
    months = sorted({(t.date.year, t.date.month) for t in txns})
    for _ in range(10):
        party = r.choice(_NOVEL_VENDORS)
        target = p95 * r.uniform(1.5, 3.0)
        yr, mo = months[r.randrange(len(months) // 2, len(months))]
        d0 = date(yr, mo, r.randint(1, 20))
        n = r.randint(1, 2)
        new = []
        for i in range(n):
            t = sg._make_txn(d0 + timedelta(days=i * r.randint(2, 6)),
                             sg._narration(bank, "vendor", party),
                             debit=_odd_amount(r, target / n * r.uniform(0.85, 1.15)),
                             category="VENDOR_PAYMENT",
                             anomaly_flags=["ml_first_time_large_counterparty"])
            new.append(t)
        if all(t.date <= hooks.period_end for t in new) and _guarded(txns, new):
            flag = {"flag_type": "ml_first_time_large_counterparty", "kind": "ml_only",
                    "description": f"{n} large payment(s) to a never-seen counterparty",
                    "triggering_transaction_ids": [t.txn_id for t in new]}
            return txns + new, flag
    return txns, None


def _inject_ml_off_hours_burst(
    txns: list[sg._Txn], company: sg._Company, bank: str, start: date, hooks: CorpusHooks
) -> _InjectResult:
    r = hooks.rng_ml
    months = sorted({(t.date.year, t.date.month) for t in txns})
    for _ in range(10):
        yr, mo = months[r.randrange(len(months) // 3, len(months))]
        d = date(yr, mo, r.randint(1, 28))
        n = r.randint(3, 6)
        payees = r.sample([f"{f} {last}" for f in _FIRST[:25] for last in _LAST[:20]], n)
        secs = sorted(r.randint(3600, 4 * 3600 + 1800) for _ in range(n))
        new = []
        for payee, s in zip(payees, secs, strict=True):
            t = sg._make_txn(d, sg._narration(bank, "upi_debit", payee),
                             debit=_odd_amount(r, r.uniform(8000, 60000)), category="OTHER",
                             anomaly_flags=["ml_off_hours_burst"])
            t.txn_time = f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"
            new.append(t)
        if _guarded(txns, new):
            flag = {"flag_type": "ml_off_hours_burst", "kind": "ml_only",
                    "description": f"{n} UPI/IMPS debits to new payees between 01:00 and 04:30",
                    "triggering_transaction_ids": [t.txn_id for t in new]}
            return txns + new, flag
    return txns, None


ML_ONLY_INJECTORS = {
    "ml_vendor_amount_outlier": _inject_ml_vendor_amount_outlier,
    "ml_first_time_large_counterparty": _inject_ml_first_time_large_counterparty,
    "ml_off_hours_burst": _inject_ml_off_hours_burst,
}


# ─────────────────────────────────────────────────────────────────────────────
# Driver
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CompanyResult:
    spec: CompanySpec
    company: sg._Company
    txns: list[sg._Txn]
    injected_flags: list[dict[str, Any]]
    latents: dict[str, CustomerLatent]
    pdf_path: Path | None = None
    truth_path: Path | None = None


def generate_company(
    spec: CompanySpec,
    *,
    pdf_dir: Path | None = None,
    truth_dir: Path | None = None,
    with_ml_only: bool = True,
) -> CompanyResult:
    """Generate one company's statement in memory (and optionally its PDF).

    ``with_ml_only=False`` reproduces the same company without the ml_only
    injections (their RNG is separate), which the guard test relies on.
    """
    hooks = CorpusHooks(spec)
    company = hooks.company
    saved_state = random.getstate()
    random.seed(spec.seed)
    try:
        with sg.corpus_mode(hooks):
            txns = sg._GENERATORS[spec.profile](spec.bank, company, spec.start, spec.n_months)
            _apply_scale(txns, spec.revenue_scale)
            injected: list[dict[str, Any]] = []
            for name in sg.auto_flags_for_profile(spec.profile):
                if name in EXCLUDED_RULE_FLAGS:
                    continue
                txns, meta = sg._FLAG_INJECTORS[name](txns, company, spec.bank, spec.start)
                meta = dict(meta)
                meta["kind"] = "compliance" if name.startswith("compliance_") else "rule"
                injected.append(meta)
            if with_ml_only:
                for name in spec.ml_flags:
                    txns, ml_meta = ML_ONLY_INJECTORS[name](txns, company, spec.bank, spec.start, hooks)
                    if ml_meta is not None:
                        injected.append(ml_meta)
            txns = [t for t in txns if t.date <= hooks.period_end]
            txns = sg._compute_balances(txns, company.opening_balance)
        result = CompanyResult(spec, company, txns, injected, hooks.latents)
        if pdf_dir is not None:
            from reportlab import rl_config

            saved_inv = rl_config.invariant
            rl_config.invariant = 1
            try:
                pdf_dir.mkdir(parents=True, exist_ok=True)
                pdf = pdf_dir / f"{spec.company_id}.pdf"
                renderer = sg._render_hdfc if spec.bank == "hdfc" else sg._render_icici
                renderer(company, txns, company.opening_balance, pdf)
                result.pdf_path = pdf
                if truth_dir is not None:
                    truth_dir.mkdir(parents=True, exist_ok=True)
                    truth = truth_dir / f"{spec.company_id}.truth.json"
                    sg._build_truth(spec.company_id, spec.bank, spec.profile, company, txns,
                                    injected, company.opening_balance, truth)
                    result.truth_path = truth
            finally:
                rl_config.invariant = saved_inv
        return result
    finally:
        random.setstate(saved_state)
