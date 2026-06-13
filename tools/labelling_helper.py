#!/usr/bin/env python3
"""Interactive CLI labelling helper for building ground-truth JSON files.

Usage:
    python -m tools.labelling_helper path/to/statement.pdf --id my_statement_001

What it does:
    1. Extracts raw rows from the PDF using pdfplumber (rough — will miss some)
    2. Shows you each row and asks you to label it
    3. Saves a .truth.json file you then move to data/gold/labels/

You will correct/verify the output before using it in the gold set.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# Category vocabulary — same as canonical schema
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIES = [
    "REVENUE",
    "VENDOR_PAYMENT",
    "SALARY",
    "LOAN_IN",
    "LOAN_OUT",
    "TAX",
    "TRANSFER",
    "FOUNDER_WITHDRAWAL",
    "FEES",
    "OTHER",
    "SKIP",  # skip this row (header, balance row, etc.)
]

CAT_SHORTCUTS = {
    "r": "REVENUE",
    "v": "VENDOR_PAYMENT",
    "s": "SALARY",
    "li": "LOAN_IN",
    "lo": "LOAN_OUT",
    "t": "TAX",
    "tr": "TRANSFER",
    "fw": "FOUNDER_WITHDRAWAL",
    "f": "FEES",
    "o": "OTHER",
    "sk": "SKIP",
    "?": "OTHER",
}


# ─────────────────────────────────────────────────────────────────────────────
# PDF extraction (rough — good enough for labelling)
# ─────────────────────────────────────────────────────────────────────────────

def _extract_rows(pdf_path: Path) -> list[dict[str, str]]:
    """Extract raw rows from PDF using pdfplumber. Returns list of row dicts."""
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except ImportError:
        print("ERROR: pdfplumber not installed. Run: pip install pdfplumber")
        sys.exit(1)

    rows: list[dict[str, str]] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_num, page in enumerate(pdf.pages):
            tables = page.extract_tables()
            if not tables:
                # Fallback: extract words as a single row
                text = page.extract_text() or ""
                for line in text.splitlines():
                    line = line.strip()
                    if line:
                        rows.append({"_raw": line, "_page": str(page_num)})
                continue

            for table in tables:
                for row_num, row in enumerate(table):
                    cells = [str(c or "").strip() for c in row]
                    raw_text = " | ".join(cells)
                    rows.append({
                        "_raw": raw_text,
                        "_page": str(page_num),
                        "_row": str(row_num),
                        "_cells": json.dumps(cells),
                    })
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for parsing amounts
# ─────────────────────────────────────────────────────────────────────────────

def _parse_inr(s: str) -> Decimal | None:
    """Parse Indian number string like '1,50,000.00' to Decimal."""
    s = s.strip().replace(",", "").replace("₹", "").replace("Rs", "").strip()
    if not s:
        return None
    try:
        return Decimal(s)
    except InvalidOperation:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Interactive labelling session
# ─────────────────────────────────────────────────────────────────────────────

_KNOWN_CUSTOMERS: dict[str, str] = {}  # name → customer_id


def _assign_customer_id(name: str) -> str:
    key = name.strip().lower()
    if key not in _KNOWN_CUSTOMERS:
        # Check if user wants to map to existing
        print(f"\n  Known customers so far: {list(_KNOWN_CUSTOMERS.values()) or '(none)'}")
        existing = input(
            f"  Customer ID for '{name}' (Enter = auto-generate, or type existing ID): "
        ).strip()
        if existing:
            _KNOWN_CUSTOMERS[key] = existing
        else:
            cid = "cust_" + re.sub(r"[^a-z0-9]", "_", key)[:20]
            _KNOWN_CUSTOMERS[key] = cid
    return _KNOWN_CUSTOMERS[key]


def _prompt_category(row_text: str) -> str:
    print(f"\n  Row: {row_text[:100]}")
    print("  Categories:")
    print("    r=REVENUE  v=VENDOR  s=SALARY  li=LOAN_IN  lo=LOAN_OUT")
    print("    t=TAX  tr=TRANSFER  fw=FOUNDER_WITHDRAWAL  f=FEES  o=OTHER  sk=SKIP")
    while True:
        raw = input("  Category [sk]: ").strip().lower() or "sk"
        if raw in CAT_SHORTCUTS:
            return CAT_SHORTCUTS[raw]
        if raw.upper() in CATEGORIES:
            return raw.upper()
        print(f"  Unknown: '{raw}'. Try again.")


def _prompt_date(suggestion: str = "") -> str:
    hint = f" [{suggestion}]" if suggestion else ""
    raw = input(f"  Date (YYYY-MM-DD){hint}: ").strip()
    if not raw and suggestion:
        return suggestion
    # Try to parse to validate
    try:
        date.fromisoformat(raw)
        return raw
    except ValueError:
        print("  Invalid date, enter YYYY-MM-DD")
        return _prompt_date(suggestion)


def _prompt_amount(label: str) -> Decimal | None:
    raw = input(f"  {label} (blank=none): ").strip().replace(",", "")
    if not raw:
        return None
    try:
        return Decimal(raw)
    except InvalidOperation:
        print("  Invalid amount")
        return _prompt_amount(label)


def _label_row(row: dict[str, str], idx: int, total: int) -> dict[str, object] | None:
    """Interactively label one row. Returns dict or None if skipped."""
    raw = row.get("_raw", "")
    print(f"\n{'─'*60}")
    print(f"  [{idx}/{total}]  Page {row.get('_page','?')}  Row {row.get('_row','?')}")

    cat = _prompt_category(raw)
    if cat == "SKIP":
        return None

    txn_date = _prompt_date()
    description = input(f"  Description [{raw[:60]}]: ").strip() or raw[:120]

    print("  Debit or Credit?  d=debit  c=credit")
    side = input("  [c]: ").strip().lower() or "c"
    debit: Decimal | None = None
    credit: Decimal | None = None
    if side == "d":
        debit = _prompt_amount("Debit amount (₹)")
    else:
        credit = _prompt_amount("Credit amount (₹)")

    balance = _prompt_amount("Closing Balance (₹)")

    customer_id: str | None = None
    if cat == "REVENUE":
        counterparty = input("  Counterparty name: ").strip()
        if counterparty:
            customer_id = _assign_customer_id(counterparty)

    is_rp = False
    related_party_match: str | None = None
    rp_ans = input("  Related party? [y/N]: ").strip().lower()
    if rp_ans == "y":
        is_rp = True
        related_party_match = input("  Match name: ").strip() or None

    return {
        "transaction_id": f"real_{idx:04d}",
        "date": txn_date,
        "description": description,
        "debit": str(debit) if debit is not None else None,
        "credit": str(credit) if credit is not None else None,
        "balance": str(balance) if balance is not None else None,
        "category": cat,
        "customer_id": customer_id,
        "is_related_party": is_rp,
        "related_party_match": related_party_match,
        "anomaly_flags": [],
    }


# ─────────────────────────────────────────────────────────────────────────────
# Save ground truth JSON
# ─────────────────────────────────────────────────────────────────────────────

def _save_truth(
    statement_id: str,
    pdf_path: Path,
    labelled: list[dict[str, object]],
    opening: Decimal | None,
    output_path: Path,
) -> None:
    revenue_txns = [t for t in labelled if t["category"] == "REVENUE"]
    expense_txns = [t for t in labelled if t["category"] not in ("REVENUE", "TRANSFER", "LOAN_IN")]

    total_rev = sum(Decimal(str(t["credit"])) for t in revenue_txns if t.get("credit"))
    total_exp = sum(Decimal(str(t["debit"])) for t in expense_txns if t.get("debit"))

    customer_ids: set[str] = {
        str(t["customer_id"]) for t in revenue_txns if t.get("customer_id")
    }

    closing_str = None
    for t in reversed(labelled):
        if t.get("balance"):
            closing_str = str(t["balance"])
            break

    truth = {
        "statement_id": statement_id,
        "bank": "unknown",
        "profile": "real",
        "account_id": "redacted",
        "period_start": labelled[0]["date"] if labelled else "",
        "period_end": labelled[-1]["date"] if labelled else "",
        "opening_balance": str(opening) if opening else "unknown",
        "closing_balance": closing_str or "unknown",
        "metrics": {
            "true_monthly_burn_rate": "manual_review_required",
            "true_runway_months": "manual_review_required",
            "true_customer_count": len(customer_ids),
            "true_total_revenue": str(total_rev.quantize(Decimal("1"))),
            "true_total_expenses": str(total_exp.quantize(Decimal("1"))),
        },
        "injected_flags": [],
        "transactions": labelled,
        "_labelling_notes": "Hand-labelled via tools/labelling_helper.py — verify and correct before using in regression",
    }

    output_path.write_text(json.dumps(truth, indent=2), encoding="utf-8")
    print(f"\nSaved: {output_path}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Interactive bank statement labelling helper")
    parser.add_argument("pdf", type=Path, help="Path to the bank statement PDF")
    parser.add_argument("--id", dest="statement_id", required=True,
                        help="Unique statement ID (e.g. real_hdfc_stmt_001)")
    parser.add_argument("--output", type=Path, default=Path("data/gold/labels"),
                        help="Output directory for the truth JSON")
    args = parser.parse_args()

    if not args.pdf.exists():
        print(f"ERROR: File not found: {args.pdf}")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"  BSAA Labelling Helper")
    print(f"  Statement: {args.pdf.name}")
    print(f"  ID: {args.statement_id}")
    print(f"{'='*60}")
    print("\nExtracting rows from PDF...")

    rows = _extract_rows(args.pdf)
    print(f"Found {len(rows)} raw rows across all pages.")
    print("\nYou will now label each row. Type 'sk' or press Enter to skip rows that are")
    print("headers, footers, opening/closing balance lines, or non-transaction rows.\n")

    opening_raw = input("Opening balance for this statement (₹, blank=unknown): ").strip()
    opening = _parse_inr(opening_raw)

    labelled: list[dict[str, object]] = []
    for i, row in enumerate(rows, start=1):
        try:
            result = _label_row(row, i, len(rows))
            if result is not None:
                labelled.append(result)
        except KeyboardInterrupt:
            print("\n\nInterrupted — saving what we have so far...")
            break

    print(f"\n{'='*60}")
    print(f"Labelled {len(labelled)} transactions.")

    # Post-labelling: set bank and period
    bank = input("Bank name (hdfc/icici/sbi/axis/kotak/other): ").strip().lower() or "unknown"
    notes = input("Any notes about this statement (e.g. which flags you spotted): ").strip()

    args.output.mkdir(parents=True, exist_ok=True)
    output_path = args.output / f"{args.statement_id}.truth.json"

    # Patch bank into each transaction id prefix
    for t in labelled:
        t["transaction_id"] = f"real_{bank}_{t['transaction_id'].split('_')[-1]}"

    _save_truth(args.statement_id, args.pdf, labelled, opening, output_path)

    if notes:
        # Append notes to the saved file
        data = json.loads(output_path.read_text())
        data["_labelling_notes"] = notes
        output_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    print(f"\nNext steps:")
    print(f"  1. Open {output_path} and review/correct any errors")
    print(f"  2. Copy the PDF to data/gold/{args.statement_id}.pdf")
    print(f"  3. Run: make regression  (once Step 1.4 is built)")


if __name__ == "__main__":
    main()
