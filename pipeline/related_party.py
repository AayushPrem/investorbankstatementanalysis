"""Related-party tagger — identifies transactions involving affiliates.

Two-stage pipeline:
  Stage 1 (deterministic) — clean the description, compare against every
    affiliate name and alias.  Handles common noise: payment prefixes,
    reference numbers, legal-form suffixes.
  Stage 2 (LLM fuzzy) — for descriptions that didn't exactly match, ask
    Claude whether the cleaned text is plausibly the same entity as any
    affiliate.  Only fires when llm_enabled=True and ANTHROPIC_API_KEY is set.

Usage:
    from pipeline.related_party import Affiliate, RelatedPartyTagger

    affiliates = [
        Affiliate(name="Mehta Enterprises", relationship="director_company",
                  aliases=["Mehta Entr", "Mehta Ent Pvt Ltd"]),
    ]
    doc = RelatedPartyTagger().tag(doc, affiliates)
"""
from __future__ import annotations

import json
import logging
import os
import re

from pydantic import BaseModel, Field

from schema.canonical import StatementDocument

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Affiliate model
# ─────────────────────────────────────────────────────────────────────────────

class Affiliate(BaseModel):
    """A person or entity related to the investee company."""
    name: str
    relationship: str  # e.g. "director", "spouse_of_founder", "sister_company"
    aliases: list[str] = Field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Name cleaning
# ─────────────────────────────────────────────────────────────────────────────

_PREFIX_RE = re.compile(
    r"^(?:NEFT|RTGS|IMPS|UPI|NACH|ACH|ECS|NETBANKING|ONLINE\s+TRANSFER)"
    r"[\s/\-]*(?:CR|DR)?[\s/\-]*",
    re.I,
)
_NARRATIVE_PREFIX_RE = re.compile(
    r"^(?:BY\s+TRANSFER\s+FROM|INWARD\s+CLEARING|INWARD\s+REMITTANCE|"
    r"TO\s+TRANSFER|FUND\s+TRANSFER\s+TO|TRANSFER\s+FROM|PAYMENT\s+FROM|"
    r"PAYMENT\s+TO|RECEIVED\s+FROM|BEING\s+PAYMENT)\s+",
    re.I,
)
# Trailing reference / invoice tokens
_TRAILING_REF_RE = re.compile(
    r"[\s/\-]+(?:INV|TXN|REF|ORD|PO|REC|RCPT|RCVD|VCH|CHQ|NEFT|SETL|SETTLEMENT)"
    r"[\s/\-]*[\w\-]+$",
    re.I,
)
# Long opaque alphanumeric bank-ref codes
_OPAQUE_TOKEN_RE = re.compile(r"\s+[A-Z0-9]{8,}\s*$")
_LEGAL_SUFFIX_RE = re.compile(
    r"\b(?:PVT\.?\s*LTD\.?|PRIVATE\s+LIMITED|LLP|LTD\.?|INC\.?|CORP\.?|"
    r"CO\.?\s*LTD\.?|LIMITED|INCORPORATED|ENTERPRISES?|INDUSTRIES|"
    r"ASSOCIATES?|SOLUTIONS?|SERVICES?|TECHNOLOGIES?)\b",
    re.I,
)
_WHITESPACE_RE = re.compile(r"\s+")
_TRAILING_PUNCT_RE = re.compile(r"[\s\-/,_.]+$")
# IFSC-style bank routing codes embedded in NEFT/RTGS narrations:
#   "NEFT/SBIN0000001/COUNTERPARTY/REF" → strip "SBIN0000001/"
_BANK_CODE_RE = re.compile(r"^[A-Z]{4,5}[0-9]{7}[\s/\-]+", re.I)


def _clean_name(s: str) -> str:
    """Strip payment noise and normalise a bank narration to its core entity name."""
    s = _PREFIX_RE.sub("", s)
    s = _NARRATIVE_PREFIX_RE.sub("", s)
    s = _BANK_CODE_RE.sub("", s)   # IFSC routing codes after payment prefix
    s = _TRAILING_REF_RE.sub("", s)
    s = _OPAQUE_TOKEN_RE.sub("", s)
    s = _LEGAL_SUFFIX_RE.sub("", s)
    s = _TRAILING_PUNCT_RE.sub("", s)
    return _WHITESPACE_RE.sub(" ", s).strip().lower()


def _affiliate_cleaned_names(aff: Affiliate) -> set[str]:
    """All cleaned representations of an affiliate's name + aliases."""
    return {_clean_name(n) for n in [aff.name] + list(aff.aliases)} - {""}


# ─────────────────────────────────────────────────────────────────────────────
# Tagger
# ─────────────────────────────────────────────────────────────────────────────

_LLM_CONFIDENCE_THRESHOLD = 0.8
_LLM_BATCH_SIZE = 30


class RelatedPartyTagger:
    """Tags transactions with is_related_party, related_party_match, counterparty."""

    def __init__(self, llm_enabled: bool = True) -> None:
        self._llm_enabled = llm_enabled and bool(os.getenv("ANTHROPIC_API_KEY"))
        if llm_enabled and not os.getenv("ANTHROPIC_API_KEY"):
            log.warning(
                "ANTHROPIC_API_KEY not set — RelatedPartyTagger using exact-match only"
            )

    # ── Public API ────────────────────────────────────────────────────────────

    def tag(
        self, doc: StatementDocument, affiliates: list[Affiliate]
    ) -> StatementDocument:
        """Populate is_related_party, related_party_match, counterparty on each
        transaction. Mutates doc.transactions in place and returns the doc.
        """
        if not affiliates:
            # Still populate counterparty even with no affiliates
            for i, txn in enumerate(doc.transactions):
                if txn.counterparty is None:
                    doc.transactions[i] = txn.model_copy(
                        update={"counterparty": _clean_name(txn.description) or txn.description}
                    )
            return doc

        aff_clean_map: list[tuple[Affiliate, set[str]]] = [
            (aff, _affiliate_cleaned_names(aff)) for aff in affiliates
        ]
        aff_names = [a.name for a in affiliates]
        unmatched_indices: list[int] = []

        # ── Stage 1: deterministic exact match ────────────────────────────────
        for i, txn in enumerate(doc.transactions):
            cleaned = _clean_name(txn.description)
            counterparty = cleaned or txn.description
            match = _stage1_exact(cleaned, aff_clean_map)

            if match is not None:
                doc.transactions[i] = txn.model_copy(update={
                    "counterparty":        counterparty,
                    "is_related_party":    True,
                    "related_party_match": match.name,
                })
            else:
                if txn.counterparty is None:
                    doc.transactions[i] = txn.model_copy(
                        update={"counterparty": counterparty}
                    )
                unmatched_indices.append(i)

        # ── Stage 2: LLM fuzzy match ───────────────────────────────────────────
        if unmatched_indices and self._llm_enabled:
            for batch_start in range(0, len(unmatched_indices), _LLM_BATCH_SIZE):
                chunk = unmatched_indices[batch_start : batch_start + _LLM_BATCH_SIZE]
                batch_txns = [doc.transactions[i] for i in chunk]
                pairs = [(_clean_name(t.description), aff_names) for t in batch_txns]
                try:
                    results = self._llm_call(pairs, aff_names)
                except Exception as exc:
                    log.warning("LLM batch call raised unexpectedly (%s) — skipping batch", exc)
                    continue

                for idx, (matched_name, confidence) in zip(chunk, results):
                    if matched_name and confidence >= _LLM_CONFIDENCE_THRESHOLD:
                        matched_aff = next(
                            (a for a in affiliates if a.name == matched_name), None
                        )
                        if matched_aff:
                            txn = doc.transactions[idx]
                            doc.transactions[idx] = txn.model_copy(update={
                                "is_related_party":    True,
                                "related_party_match": matched_aff.name,
                            })

        n_tagged = sum(1 for t in doc.transactions if t.is_related_party)
        log.info(
            "RelatedPartyTagger: %d / %d transactions tagged as related-party",
            n_tagged, len(doc.transactions),
        )
        return doc

    # ── LLM call ──────────────────────────────────────────────────────────────

    def _llm_call(
        self,
        pairs: list[tuple[str, list[str]]],
        aff_names: list[str],
    ) -> list[tuple[str | None, float]]:
        """Ask Claude whether each cleaned description matches any affiliate.

        Returns list of (matched_name | None, confidence).
        """
        try:
            import anthropic  # type: ignore[import-untyped]
        except ImportError:
            log.warning("anthropic package not installed — skipping LLM fuzzy match")
            return [(None, 0.0)] * len(pairs)

        lines = "\n".join(f"{i+1}. {desc}" for i, (desc, _) in enumerate(pairs))
        user_msg = (
            f"Known affiliates: {json.dumps(aff_names)}\n\n"
            "For each description below, decide if it refers to the same entity "
            "as one of the known affiliates (accounting for abbreviations, "
            "alternate spellings, legal-form variations).\n\n"
            f"{lines}\n\n"
            "Respond ONLY with a JSON array, one object per item:\n"
            '[{"match": "<affiliate_name or null>", "confidence": <0.0-1.0>}, ...]'
        )

        try:
            client = anthropic.Anthropic()
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=512,
                system=(
                    "You are a financial analyst. Determine if bank transaction "
                    "descriptions match known affiliate entities. "
                    "Respond only with valid JSON — no explanation."
                ),
                messages=[{"role": "user", "content": user_msg}],
            )
            raw = json.loads(response.content[0].text.strip())
            if not isinstance(raw, list) or len(raw) != len(pairs):
                raise ValueError("Unexpected response shape")
            return [
                (item.get("match") or None, float(item.get("confidence", 0)))
                for item in raw
            ]
        except Exception as exc:
            log.warning("LLM fuzzy match failed (%s) — falling back to no-match", exc)
            return [(None, 0.0)] * len(pairs)


# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _stage1_exact(
    cleaned_desc: str,
    aff_clean_map: list[tuple[Affiliate, set[str]]],
) -> Affiliate | None:
    for aff, cleaned_names in aff_clean_map:
        if cleaned_desc and cleaned_desc in cleaned_names:
            return aff
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Counterparty extraction — for auto-suggesting related parties in the UI
# ─────────────────────────────────────────────────────────────────────────────

_SKIP_ENTITY = re.compile(
    r"gst\s*(payment|pmt)|tds\s*payment|income\s*tax|advance\s*tax"
    r"|salary|payroll|wages|esi|pf\s*(payment)?|provident\s*fund"
    r"|professional\s*tax|opening\s*bal|closing\s*bal|cash\s*dep"
    r"|service\s*charge|bank\s*charge|processing\s*fee|late\s*fee"
    r"|cheque\s*(return|bounce)|penalty|self\s*transfer|own\s*account"
    r"|neft\s*charge|imps\s*charge|razorpay\s*fee|stripe\s*fee",
    re.I,
)


def extract_counterparties(
    doc: "StatementDocument",
) -> list[tuple[str, int, float]]:
    """Extract unique counterparty names from transaction narrations.

    Uses the same ``_clean_name`` normalisation as the tagger so names
    shown in the UI match what the tagger will match against.

    Returns a list of *(display_name, txn_count, total_volume_inr)*
    sorted by volume descending.
    """
    from collections import defaultdict
    from decimal import Decimal

    agg: dict[str, dict] = defaultdict(lambda: {"count": 0, "amount": Decimal("0")})

    for txn in doc.transactions:
        raw = txn.description
        if _SKIP_ENTITY.search(raw):
            continue

        # Re-use the existing counterparty field if already populated
        if txn.counterparty:
            key = txn.counterparty.title()
        else:
            cleaned = _clean_name(raw)
            if not cleaned or len(cleaned) < 3:
                continue
            key = cleaned.title()

        # Skip pure numeric/hex strings (bank refs leaked through)
        if re.match(r"^[A-F0-9\s]{6,}$", key, re.I):
            continue

        agg[key]["count"] += 1
        agg[key]["amount"] += (txn.debit or Decimal("0")) + (txn.credit or Decimal("0"))

    result = [
        (name, data["count"], float(data["amount"]))
        for name, data in agg.items()
        if data["count"] >= 1
    ]
    return sorted(result, key=lambda x: x[2], reverse=True)
