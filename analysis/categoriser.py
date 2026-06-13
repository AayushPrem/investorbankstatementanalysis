"""Categoriser — assigns TransactionCategory to each CanonicalTransaction.

Architecture: deterministic-first.
  1. Rule layer  — regex patterns on description + debit/credit direction.
                   Handles the vast majority of Indian bank narrations.
  2. LLM layer   — Claude Haiku for anything the rules don't resolve.
                   Batched (up to 50 per API call) to minimise cost.
                   Disabled gracefully when ANTHROPIC_API_KEY is absent.

Usage:
    from analysis.categoriser import Categoriser
    doc = Categoriser().categorise(doc)          # mutates and returns doc
    doc = Categoriser(llm_enabled=False).categorise(doc)  # rules only
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from schema.canonical import CanonicalTransaction, StatementDocument, TransactionCategory

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Rule layer
# ─────────────────────────────────────────────────────────────────────────────

# Each rule is (description_regex | None, requires_credit | None, category).
# requires_credit:  True  → only match credits
#                   False → only match debits
#                   None  → match either direction
# Rules are evaluated in order; first match wins.

_RULES: list[tuple[re.Pattern[str] | None, bool | None, TransactionCategory]] = [
    # ── SALARY ────────────────────────────────────────────────────────────
    (re.compile(r"\bSALARY\b|\bPAYROLL\b", re.I), False, TransactionCategory.SALARY),

    # ── TAX ───────────────────────────────────────────────────────────────
    (re.compile(r"GST\s*PAYMENT|TDS\s*PAYMENT|INCOME\s*TAX|GSTIN|TCS\s*PAYMENT", re.I),
     None, TransactionCategory.TAX),

    # ── FOUNDER WITHDRAWAL ────────────────────────────────────────────────
    (re.compile(r"FOUNDER\s*WITHDRAWAL|DIRECTOR\s*WITHDRAWAL|\bPERSONAL\b.*WITHDRAWAL"
                r"|WITHDRAWAL.*\bPERSONAL\b|\bPERSONAL\s*A/?C\b", re.I),
     False, TransactionCategory.FOUNDER_WITHDRAWAL),
    # Synthetic gen uses "{Name} PERSONAL" for founder withdrawals
    (re.compile(r"\bPERSONAL\b$", re.I), False, TransactionCategory.FOUNDER_WITHDRAWAL),

    # ── BANK FEES ─────────────────────────────────────────────────────────
    (re.compile(r"BANK\s*CHARGES|SERVICE\s*CHARGES|PROCESSING\s*FEE|ANNUAL\s*FEE"
                r"|FEE\s*DEDUCTION|PLATFORM\s*FEE|TRANSACTION\s*CHARGES|NEFT\s*CHARGES"
                r"|IMPS\s*CHARGES|CHEQUE\s*BOUNCE", re.I),
     None, TransactionCategory.FEES),

    # ── LOAN INFLOW ───────────────────────────────────────────────────────
    (re.compile(r"\bLOAN\b|\bCC\s*LIMIT\b|\bOD\s*LIMIT\b|\bCREDIT\s*FACILITY\b", re.I),
     True, TransactionCategory.LOAN_IN),

    # ── LOAN OUTFLOW / EMI ────────────────────────────────────────────────
    (re.compile(r"\bEMI\b|\bLOAN\s*REPAY|\bLOAN\s*INSTALLMENT\b|\bLOAN\s*DR\b", re.I),
     False, TransactionCategory.LOAN_OUT),

    # ── TRANSFER (self / internal) ────────────────────────────────────────
    (re.compile(r"SELF\s*TRANSFER|OWN\s*ACCOUNT|INTERNAL\s*TRANSFER", re.I),
     None, TransactionCategory.TRANSFER),

    # ── REVENUE — invoice-referenced credit ───────────────────────────────
    (re.compile(r"INV[-/]?\d+|INVOICE", re.I), True, TransactionCategory.REVENUE),

    # ── REVENUE — cash/cheque/POS deposits ───────────────────────────────
    (re.compile(r"CASH\s*DEP|CHQ\s*DEP|CHEQUE\s*DEP|POS\s*CR|UPI\s*CR|PAYTM\s*CR"
                r"|RAZORPAY\s*CR|PAYMENT\s*GATEWAY", re.I),
     True, TransactionCategory.REVENUE),

    # ── VENDOR — named payment processors that are debit fees ─────────────
    # (Razorpay, Stripe, etc. debiting fees are FEES; credits are REVENUE handled above)
    (re.compile(r"RAZORPAY|STRIPE|PAYTM\s*FEE|PAYMENT\s*GATEWAY\s*FEE", re.I),
     False, TransactionCategory.FEES),

    # ── VENDOR PAYMENT — NEFT/RTGS/IMPS/UPI debit (catch-all for debits) ─
    (re.compile(r"NEFT|RTGS|IMPS|UPI|ECS\s*DR|ACH\s*DR", re.I),
     False, TransactionCategory.VENDOR_PAYMENT),

    # ── REVENUE — any remaining credit ────────────────────────────────────
    (None, True, TransactionCategory.REVENUE),

    # ── VENDOR PAYMENT — any remaining debit ──────────────────────────────
    (None, False, TransactionCategory.VENDOR_PAYMENT),
]


def _rule_classify(txn: CanonicalTransaction) -> TransactionCategory | None:
    """Return the first matching rule's category, or None if no rule fired."""
    is_credit = txn.credit is not None
    for pattern, requires_credit, category in _RULES:
        if requires_credit is not None and requires_credit != is_credit:
            continue
        if pattern is None or pattern.search(txn.description):
            return category
    return None  # unreachable with catch-all rules, but kept for type safety


# ─────────────────────────────────────────────────────────────────────────────
# LLM layer
# ─────────────────────────────────────────────────────────────────────────────

_CATEGORY_VALUES = [c.value for c in TransactionCategory]

_SYSTEM_PROMPT = """\
You are a financial analyst categorising Indian business bank transactions.
Respond ONLY with a JSON array of category strings — one per transaction, \
in the same order as the input. No explanation, no markdown.

Categories (pick exactly one per transaction):
  REVENUE            – money received from customers for goods/services
  VENDOR_PAYMENT     – payments to suppliers, contractors, service providers
  SALARY             – employee salary / payroll disbursements
  LOAN_IN            – loan received (credit to account)
  LOAN_OUT           – loan repayment, EMI, overdraft repayment
  TAX                – GST, TDS, income tax, and other government levies
  TRANSFER           – internal / own-account transfers, round-trips
  FOUNDER_WITHDRAWAL – withdrawals by founders or directors for personal use
  FEES               – bank charges, processing fees, subscription fees
  OTHER              – does not fit any above category\
"""


def _build_user_message(txns: list[CanonicalTransaction]) -> str:
    lines = []
    for i, txn in enumerate(txns, 1):
        direction = "CREDIT" if txn.credit is not None else "DEBIT"
        lines.append(f"{i}. [{direction}] {txn.description}")
    return "\n".join(lines)


def _parse_llm_response(text: str, n: int) -> list[TransactionCategory]:
    """Parse the LLM JSON array; fall back to OTHER on any parse error."""
    try:
        raw = json.loads(text.strip())
        if not isinstance(raw, list) or len(raw) != n:
            raise ValueError("Unexpected shape")
        result = []
        for item in raw:
            try:
                result.append(TransactionCategory(str(item).upper()))
            except ValueError:
                result.append(TransactionCategory.OTHER)
        return result
    except Exception:
        return [TransactionCategory.OTHER] * n


# ─────────────────────────────────────────────────────────────────────────────
# CategorisationStats — returned with the document
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class CategorisationStats:
    total: int = 0
    rule_matched: int = 0
    llm_matched: int = 0
    fallback_other: int = 0

    @property
    def rule_match_rate(self) -> float:
        return self.rule_matched / self.total if self.total else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

class Categoriser:
    """Assigns TransactionCategory to every transaction in a StatementDocument."""

    LLM_MODEL = "claude-haiku-4-5-20251001"
    BATCH_SIZE = 50

    def __init__(
        self,
        llm_enabled: bool = True,
        llm_model: str = LLM_MODEL,
        batch_size: int = BATCH_SIZE,
    ) -> None:
        self._llm_model = llm_model
        self._batch_size = batch_size
        # Disable LLM if the caller requests it or if no API key is configured
        self._llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))
        if llm_enabled and not os.getenv("ANTHROPIC_API_KEY"):
            log.warning("ANTHROPIC_API_KEY not set — Categoriser running in rule-only mode")

    def categorise(self, doc: StatementDocument) -> tuple[StatementDocument, CategorisationStats]:
        """Assign category to every transaction. Mutates doc in place.

        Returns the doc (for chaining) and a CategorisationStats summary.
        """
        stats = CategorisationStats(total=len(doc.transactions))
        unmatched: list[CanonicalTransaction] = []

        for txn in doc.transactions:
            cat = _rule_classify(txn)
            if cat is not None:
                txn.category = cat
                stats.rule_matched += 1
            else:
                unmatched.append(txn)

        if unmatched:
            if self._llm_enabled:
                try:
                    llm_cats = self._llm_categorise_batch(unmatched)
                except Exception as exc:
                    log.warning("LLM batch failed (%s) — assigning OTHER to %d transactions", exc, len(unmatched))
                    llm_cats = [TransactionCategory.OTHER] * len(unmatched)
                for txn, cat in zip(unmatched, llm_cats):
                    txn.category = cat
                    if cat == TransactionCategory.OTHER:
                        stats.fallback_other += 1
                    else:
                        stats.llm_matched += 1
            else:
                for txn in unmatched:
                    txn.category = TransactionCategory.OTHER
                stats.fallback_other += len(unmatched)

        log.info(
            "Categorised %d transactions: %d rule / %d llm / %d other",
            stats.total, stats.rule_matched, stats.llm_matched, stats.fallback_other,
        )
        return doc, stats

    # ── LLM batch ──────────────────────────────────────────────────────────

    def _llm_categorise_batch(
        self, txns: list[CanonicalTransaction]
    ) -> list[TransactionCategory]:
        try:
            import anthropic  # type: ignore[import-untyped]
        except ImportError:
            log.warning("anthropic package not installed — falling back to OTHER")
            return [TransactionCategory.OTHER] * len(txns)

        client = anthropic.Anthropic()
        results: list[TransactionCategory] = []

        for i in range(0, len(txns), self._batch_size):
            batch = txns[i : i + self._batch_size]
            results.extend(self._llm_call(client, batch))

        return results

    def _llm_call(
        self, client: object, txns: list[CanonicalTransaction]
    ) -> list[TransactionCategory]:
        try:
            response = client.messages.create(  # type: ignore[union-attr]
                model=self._llm_model,
                max_tokens=256,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": _build_user_message(txns)}],
            )
            text = response.content[0].text
            return _parse_llm_response(text, len(txns))
        except Exception as exc:
            log.warning("LLM call failed (%s) — falling back to OTHER for batch", exc)
            return [TransactionCategory.OTHER] * len(txns)
