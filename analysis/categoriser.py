"""Categoriser v3 — hybrid confidence routing (Phase 4).

Routing, per transaction (UPGRADE_GUIDE §10):

  1. high-precision rule hit (audited subset of _RULES)  → source="rule"
  2. ML p_max ≥ τ_accept                                  → source="ml"
  3. LLM available → LLM picks from the ML top-3          → source="llm"
        LLM answer not in the top-3 / parse failure       → source="review"
  4. LLM unavailable or failed: p_max ≥ τ_review          → source="ml" (degraded accept)
                                p_max <  τ_review         → source="review"

Review rows still get a category (the ML best guess) so downstream stages
never see ``None``; ``txn.category_source == "review"`` marks them for a human.
τ and the high-precision rule list live in config/thresholds.yaml and are
chosen on validation data by ml/categoriser/evaluate.py.

**No model → v2 behaviour, unchanged.** If no model is configured or the
artifact is missing, the full v2 rule list (including the direction
catch-alls) categorises every row, exactly as before. With no
ANTHROPIC_API_KEY, both paths are deterministic.

Usage:
    from analysis.categoriser import Categoriser
    doc, stats = Categoriser().categorise(doc)                  # model if configured
    doc, stats = Categoriser(llm_enabled=False).categorise(doc) # no LLM
    doc, stats = Categoriser(model=None).categorise(doc)        # v2 rules only
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from config.logging import safe_exc
from config.settings import get_settings
from config.thresholds import get_thresholds
from llm.client import LLMClient, LLMUnavailable, get_llm_client
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
    (re.compile(r"\bSALARY\b|\bPAYROLL\b|\bWAGES\b", re.I), False, TransactionCategory.SALARY),

    # ── TAX ───────────────────────────────────────────────────────────────
    # v2: added GST PMT (ICICI format), professional tax, ESI, PF
    (re.compile(
        r"GST\s*PAYMENT|GST\s*PMT|TDS\s*PAYMENT|INCOME\s*TAX|GSTIN|TCS\s*PAYMENT"
        r"|PROFESSIONAL\s*TAX|PT\s*PAYMENT|\bESI\s*PAYMENT\b|\bPF\s*PAYMENT\b"
        r"|\bPROVIDENT\s*FUND\b",
        re.I,
    ), None, TransactionCategory.TAX),

    # ── FOUNDER WITHDRAWAL ────────────────────────────────────────────────
    (re.compile(r"FOUNDER\s*WITHDRAWAL|DIRECTOR\s*WITHDRAWAL|\bPERSONAL\b.*WITHDRAWAL"
                r"|WITHDRAWAL.*\bPERSONAL\b|\bPERSONAL\s*A/?C\b", re.I),
     False, TransactionCategory.FOUNDER_WITHDRAWAL),
    # Synthetic gen uses "{Name} PERSONAL" for founder withdrawals
    (re.compile(r"\bPERSONAL\b$", re.I), False, TransactionCategory.FOUNDER_WITHDRAWAL),

    # ── BANK FEES ─────────────────────────────────────────────────────────
    # v2: added cheque-return charges, late fees, penalty
    (re.compile(
        r"BANK\s*CHARGES|SERVICE\s*CHARGES|PROCESSING\s*FEE|ANNUAL\s*FEE"
        r"|FEE\s*DEDUCTION|PLATFORM\s*FEE|TRANSACTION\s*CHARGES|NEFT\s*CHARGES"
        r"|IMPS\s*CHARGES|CHEQUE\s*BOUNCE|CHEQUE\s*RETURN\s*CHARGES"
        r"|\bPENALTY\b|\bLATE\s*FEE\b|BOUNCE\s*CHARGES",
        re.I,
    ), None, TransactionCategory.FEES),

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


# Short names for the rule audit / reports (same order as _RULES).
RULE_NAMES: tuple[str, ...] = (
    "salary_keyword", "tax_keyword", "founder_withdrawal_keyword", "personal_suffix",
    "bank_fees_keyword", "loan_in_keyword", "loan_out_emi", "self_transfer",
    "revenue_invoice_credit", "revenue_deposit_credit", "gateway_debit_fee",
    "rail_debit_vendor", "catchall_credit_revenue", "catchall_debit_vendor",
)
assert len(RULE_NAMES) == len(_RULES)


def match_rule_fields(description: str, is_credit: bool) -> tuple[int, TransactionCategory] | None:
    """First matching rule for a (narration, direction) pair. See :func:`match_rule`."""
    for idx, (pattern, requires_credit, category) in enumerate(_RULES):
        if requires_credit is not None and requires_credit != is_credit:
            continue
        if pattern is None or pattern.search(description):
            return idx, category
    return None  # unreachable with catch-all rules, but kept for type safety


def match_rule(txn: CanonicalTransaction) -> tuple[int, TransactionCategory] | None:
    """Return ``(rule index in _RULES, category)`` of the first matching rule, or None.

    Public so the data platform can record which rule categorised a row
    (``categorisations.rule_id``) without duplicating the rule loop.
    """
    return match_rule_fields(txn.description, txn.credit is not None)


def match_high_precision_rule(
    txn: CanonicalTransaction, allowed: frozenset[int] | set[int] | tuple[int, ...],
) -> tuple[int, TransactionCategory] | None:
    """First-match rule, kept only if it is in the audited high-precision list.

    First-match semantics are preserved: if an earlier non-allowed rule fires,
    no later rule is consulted (the row goes to the model instead).
    """
    hit = match_rule(txn)
    if hit is not None and hit[0] in allowed:
        return hit
    return None


def _rule_classify(txn: CanonicalTransaction) -> TransactionCategory | None:
    """Return the first matching rule's category, or None if no rule fired."""
    hit = match_rule(txn)
    return hit[1] if hit is not None else None


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
  OTHER              – does not fit any above category

When a transaction lists "candidates", a statistical model has narrowed it \
down: answer with one of those candidates, or REVIEW if none of them fits.\
"""


def _build_user_message(
    txns: list[CanonicalTransaction], candidates: list[list[str]] | None = None,
) -> str:
    lines = []
    for i, txn in enumerate(txns, 1):
        direction = "CREDIT" if txn.credit is not None else "DEBIT"
        line = f"{i}. [{direction}] {txn.description}"
        if candidates is not None:
            line += f" (candidates: {', '.join(candidates[i - 1])})"
        lines.append(line)
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
    """Per-document routing counts.

    Invariant: rule_matched + ml_matched + llm_matched + review + fallback_other == total.
    """

    total: int = 0
    rule_matched: int = 0
    llm_matched: int = 0
    fallback_other: int = 0
    ml_matched: int = 0
    review: int = 0
    model_version: str | None = None   # None → v2 rules-only path

    @property
    def rule_match_rate(self) -> float:
        return self.rule_matched / self.total if self.total else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# Routing decision (pure; unit-tested directly)
# ─────────────────────────────────────────────────────────────────────────────

SOURCE_RULE = "rule"
SOURCE_ML = "ml"
SOURCE_LLM = "llm"
SOURCE_REVIEW = "review"
SOURCE_DEFAULT = "default"   # v2 path: OTHER after a failed / disabled LLM


def route_ml(p_max: float, tau_accept: float) -> bool:
    """Step 2: accept the model's argmax outright?"""
    return p_max >= tau_accept


def route_without_llm(p_max: float, tau_review: float) -> str:
    """Step 4 (no LLM available or the LLM call failed)."""
    return SOURCE_ML if p_max >= tau_review else SOURCE_REVIEW


def route_llm_answer(answer: str | None, candidates: list[str]) -> str:
    """Step 3: an LLM answer is accepted only if it is one of the ML top-k."""
    return SOURCE_LLM if answer is not None and answer in candidates else SOURCE_REVIEW


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

_AUTO = "auto"


class Categoriser:
    """Assigns TransactionCategory to every transaction in a StatementDocument."""

    BATCH_SIZE = get_thresholds().categoriser.llm_batch_size  # config/thresholds.yaml
    TOP_K = 3

    def __init__(
        self,
        llm_enabled: bool = True,
        llm_model: str | None = None,
        batch_size: int = BATCH_SIZE,
        model: Any = _AUTO,
        tau_accept: float | None = None,
        tau_review: float | None = None,
        high_precision_rules: tuple[int, ...] | None = None,
    ) -> None:
        """``model``: "auto" (configured registry model, else v2), ``None`` (force
        v2 rules-only) or a ``ml.categoriser.infer.CategoriserModel``."""
        settings = get_settings()
        # None → the configured model (config/settings.py, env LLM_MODEL)
        self._llm_model = llm_model or settings.llm_model
        self._batch_size = batch_size
        # Disable LLM if the caller requests it or if no API key is configured
        self._llm_enabled = llm_enabled and settings.llm_available
        if llm_enabled and not settings.llm_available:
            log.warning("ANTHROPIC_API_KEY not set — Categoriser running without the LLM layer")

        if model == _AUTO:
            from ml.categoriser.infer import resolve_model
            model = resolve_model()
        self._model = model
        cfg = get_thresholds().categoriser
        self._tau_accept = cfg.tau_accept if tau_accept is None else tau_accept
        self._tau_review = cfg.tau_review if tau_review is None else tau_review
        self._hp_rules = frozenset(
            cfg.high_precision_rules if high_precision_rules is None else high_precision_rules
        )
        if not 0.0 <= self._tau_review <= self._tau_accept <= 1.0:
            raise ValueError("need 0 <= tau_review <= tau_accept <= 1")

    @property
    def model_version(self) -> str | None:
        return None if self._model is None else str(self._model.version)

    def categorise(self, doc: StatementDocument) -> tuple[StatementDocument, CategorisationStats]:
        """Assign category to every transaction. Mutates doc in place.

        Returns the doc (for chaining) and a CategorisationStats summary.
        """
        if self._model is None:
            stats = self._categorise_v2(doc)
        else:
            stats = self._categorise_v3(doc)
        log.info(
            "Categorised %d transactions: %d rule / %d ml / %d llm / %d review / %d other (model=%s)",
            stats.total, stats.rule_matched, stats.ml_matched, stats.llm_matched,
            stats.review, stats.fallback_other, stats.model_version,
        )
        return doc, stats

    # ── v2: full rule list, LLM for leftovers (unchanged behaviour) ─────────

    def _categorise_v2(self, doc: StatementDocument) -> CategorisationStats:
        stats = CategorisationStats(total=len(doc.transactions))
        unmatched: list[CanonicalTransaction] = []

        for txn in doc.transactions:
            cat = _rule_classify(txn)
            if cat is not None:
                txn.category = cat
                txn.category_source = SOURCE_RULE
                txn.category_confidence = None
                stats.rule_matched += 1
            else:
                unmatched.append(txn)

        if unmatched:
            if self._llm_enabled:
                try:
                    llm_cats = self._llm_categorise_batch(unmatched)
                except Exception as exc:
                    log.warning(
                        "LLM batch failed (%s) — assigning OTHER to %d transactions",
                        safe_exc(exc), len(unmatched),
                    )
                    llm_cats = [TransactionCategory.OTHER] * len(unmatched)
                for txn, cat in zip(unmatched, llm_cats, strict=True):
                    txn.category = cat
                    txn.category_confidence = None
                    if cat == TransactionCategory.OTHER:
                        txn.category_source = SOURCE_DEFAULT
                        stats.fallback_other += 1
                    else:
                        txn.category_source = SOURCE_LLM
                        stats.llm_matched += 1
            else:
                for txn in unmatched:
                    txn.category = TransactionCategory.OTHER
                    txn.category_source = SOURCE_DEFAULT
                    txn.category_confidence = None
                stats.fallback_other += len(unmatched)
        return stats

    # ── v3: rule → ML → LLM(top-3) → review ────────────────────────────────

    def _categorise_v3(self, doc: StatementDocument) -> CategorisationStats:
        model = self._model
        stats = CategorisationStats(total=len(doc.transactions), model_version=self.model_version)

        to_model: list[CanonicalTransaction] = []
        for txn in doc.transactions:
            hit = match_high_precision_rule(txn, self._hp_rules)
            if hit is not None:
                txn.category = hit[1]
                txn.category_source = SOURCE_RULE
                txn.category_confidence = 1.0
                stats.rule_matched += 1
            else:
                to_model.append(txn)
        if not to_model:
            return stats

        proba = model.predict_proba_transactions(to_model)
        classes: list[str] = list(model.classes)
        pending: list[tuple[CanonicalTransaction, list[str], float]] = []
        for txn, row in zip(to_model, proba, strict=True):
            best = int(row.argmax())
            p_max = float(row[best])
            txn.category = TransactionCategory(classes[best])
            txn.category_confidence = p_max
            if route_ml(p_max, self._tau_accept):
                txn.category_source = SOURCE_ML
                stats.ml_matched += 1
            else:
                cands = [c for c, _ in _top_k(row, classes, self.TOP_K)]
                pending.append((txn, cands, p_max))

        llm_answers: list[Any] | None = None
        if pending and self._llm_enabled:
            try:
                llm_answers = self._llm_categorise_batch(
                    [t for t, _, _ in pending], candidates=[c for _, c, _ in pending],
                )
            except Exception as exc:
                log.warning("LLM batch failed (%s) — routing %d rows without the LLM",
                            safe_exc(exc), len(pending))
                llm_answers = None

        for i, (txn, cands, p_max) in enumerate(pending):
            ans = llm_answers[i] if llm_answers is not None else None
            if ans is LLM_REVIEW:
                source = SOURCE_REVIEW
            elif isinstance(ans, TransactionCategory):
                source = route_llm_answer(ans.value, cands)
                if source == SOURCE_LLM:
                    txn.category = ans
            else:
                source = route_without_llm(p_max, self._tau_review)
            txn.category_source = source
            if source == SOURCE_LLM:
                stats.llm_matched += 1
            elif source == SOURCE_ML:
                stats.ml_matched += 1
            else:
                stats.review += 1
        return stats

    # ── LLM batch ──────────────────────────────────────────────────────────

    def _llm_categorise_batch(
        self, txns: list[CanonicalTransaction], candidates: list[list[str]] | None = None,
    ) -> list[Any]:
        """v2 (no candidates): list[TransactionCategory], OTHER on failure.
        v3 (candidates): TransactionCategory, LLM_REVIEW (the LLM said REVIEW or
        gave an unknown label → review queue) or None (failed call / unparseable
        batch → the caller applies the no-LLM τ_review rule)."""
        client = get_llm_client(enabled=True, model=self._llm_model)
        results: list[Any] = []

        for i in range(0, len(txns), self._batch_size):
            batch = txns[i : i + self._batch_size]
            cands = None if candidates is None else candidates[i : i + self._batch_size]
            results.extend(self._llm_call(client, batch, cands))

        return results

    def _llm_call(
        self, client: LLMClient, txns: list[CanonicalTransaction],
        candidates: list[list[str]] | None = None,
    ) -> list[Any]:
        failed: list[Any] = (
            [TransactionCategory.OTHER] * len(txns) if candidates is None else [None] * len(txns)
        )
        try:
            text = client.complete(
                _build_user_message(txns, candidates), system=_SYSTEM_PROMPT, max_tokens=256,
            )
        except LLMUnavailable:
            log.warning("LLM unavailable — no LLM answer for batch of %d", len(txns))
            return failed
        except Exception as exc:
            log.warning("LLM call failed (%s) — no LLM answer for batch of %d",
                        safe_exc(exc), len(txns))
            return failed
        if candidates is not None:
            parsed = _parse_llm_review_response(text, len(txns))
            return parsed
        return _parse_llm_response(text, len(txns))


LLM_REVIEW = "REVIEW"   # sentinel: the LLM declined all candidates


def _parse_llm_review_response(text: str, n: int) -> list[Any]:
    """v3 parse. Per item: TransactionCategory, or LLM_REVIEW for REVIEW/unknown.
    A malformed or wrong-length response gives ``[None] * n`` (no usable answer)."""
    try:
        raw = json.loads(text.strip())
    except Exception:
        return [None] * n
    if not isinstance(raw, list) or len(raw) != n:
        return [None] * n
    out: list[Any] = []
    for item in raw:
        try:
            out.append(TransactionCategory(str(item).upper()))
        except ValueError:
            out.append(LLM_REVIEW)
    return out


def _top_k(row: Any, classes: list[str], k: int) -> list[tuple[str, float]]:
    from ml.categoriser.infer import top_k
    return top_k(row, classes, k)
