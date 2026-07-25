"""Tests for pipeline.related_party — RelatedPartyTagger (Step 2.1)."""
from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from pipeline.related_party import (
    Affiliate,
    RelatedPartyTagger,
    _affiliate_cleaned_names,
    _clean_name,
    _stage1_exact,
)
from schema.canonical import CanonicalTransaction, SourceReference, StatementDocument


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _src(row: int = 0) -> SourceReference:
    return SourceReference(file_path="test.pdf", page=1, row=row, raw_text="")


def _txn(desc: str, *, txn_id: str = "t1", credit: Decimal | None = None) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=txn_id,
        date=datetime.date(2024, 4, 1),
        description=desc,
        credit=credit or Decimal("1000"),
        balance=Decimal("50000"),
        source_reference=_src(),
    )


def _doc(*transactions: CanonicalTransaction) -> StatementDocument:
    return StatementDocument(
        account_id="ACC001",
        statement_period_start=datetime.date(2024, 4, 1),
        statement_period_end=datetime.date(2024, 4, 30),
        source_format="HDFC_DIGITAL",
        transactions=list(transactions),
    )


# ─────────────────────────────────────────────────────────────────────────────
# _clean_name
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanName:
    def test_strips_neft_prefix(self) -> None:
        assert _clean_name("NEFT/ACME PVT LTD/REF12345") == "acme"

    def test_strips_rtgs_prefix(self) -> None:
        assert _clean_name("RTGS/MEHTA ENTERPRISES") == "mehta"

    def test_strips_upi_prefix(self) -> None:
        assert _clean_name("UPI-Razorpay-Pvt Ltd") == "razorpay"

    def test_strips_narrative_prefix_payment_from(self) -> None:
        assert _clean_name("PAYMENT FROM SUNRISE SOLUTIONS LTD") == "sunrise"

    def test_strips_narrative_prefix_transfer_from(self) -> None:
        assert _clean_name("TRANSFER FROM SUNRISE TECH") == "sunrise tech"

    def test_strips_narrative_prefix_received_from(self) -> None:
        assert _clean_name("RECEIVED FROM APEX CORP") == "apex"

    def test_strips_trailing_ref(self) -> None:
        assert _clean_name("MEHTA ENTERPRISES REF98765") == "mehta"

    def test_strips_trailing_inv(self) -> None:
        assert _clean_name("MEHTA ENTERPRISES INV-2024-001") == "mehta"

    def test_strips_opaque_alphanumeric_token(self) -> None:
        result = _clean_name("ACME PVT LTD ABCD1234EF")
        assert "ABCD1234EF".lower() not in result

    def test_strips_pvt_ltd(self) -> None:
        assert _clean_name("Acme Pvt Ltd") == "acme"

    def test_strips_private_limited(self) -> None:
        assert _clean_name("Acme Private Limited") == "acme"

    def test_strips_llp(self) -> None:
        assert _clean_name("Sharma LLP") == "sharma"

    def test_strips_incorporated(self) -> None:
        assert _clean_name("Brightpath Incorporated") == "brightpath"

    def test_normalises_whitespace(self) -> None:
        assert _clean_name("  Acme   Corp  ") == "acme"

    def test_returns_lowercase(self) -> None:
        assert _clean_name("ACME PVT LTD") == _clean_name("ACME PVT LTD").lower()

    def test_empty_string(self) -> None:
        assert _clean_name("") == ""

    def test_plain_name_passthrough(self) -> None:
        assert _clean_name("razorpay") == "razorpay"

    def test_imps_prefix_stripped(self) -> None:
        assert _clean_name("IMPS-Acme Pvt Ltd") == "acme"


# ─────────────────────────────────────────────────────────────────────────────
# _affiliate_cleaned_names
# ─────────────────────────────────────────────────────────────────────────────

class TestAffiliateCleaning:
    def test_includes_cleaned_main_name(self) -> None:
        aff = Affiliate(name="Acme Private Limited", relationship="founder_company")
        names = _affiliate_cleaned_names(aff)
        assert "acme" in names

    def test_includes_cleaned_aliases(self) -> None:
        aff = Affiliate(
            name="Mehta Enterprises",
            relationship="director",
            aliases=["Mehta Entr Pvt Ltd", "MEHTA ENT"],
        )
        names = _affiliate_cleaned_names(aff)
        assert "mehta" in names

    def test_excludes_empty_strings(self) -> None:
        aff = Affiliate(name="LLP", relationship="sister")  # LLP alone strips to ""
        names = _affiliate_cleaned_names(aff)
        assert "" not in names


# ─────────────────────────────────────────────────────────────────────────────
# _stage1_exact
# ─────────────────────────────────────────────────────────────────────────────

class TestStage1Exact:
    def _map(self, *affs: Affiliate):
        return [(a, _affiliate_cleaned_names(a)) for a in affs]

    def test_exact_hit(self) -> None:
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = _stage1_exact("acme", self._map(aff))
        assert result is aff

    def test_alias_hit(self) -> None:
        aff = Affiliate(
            name="Mehta Enterprises",
            relationship="director",
            aliases=["mehta entr"],
        )
        result = _stage1_exact("mehta entr", self._map(aff))
        assert result is aff

    def test_no_match_returns_none(self) -> None:
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        assert _stage1_exact("razorpay", self._map(aff)) is None

    def test_empty_desc_returns_none(self) -> None:
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        assert _stage1_exact("", self._map(aff)) is None

    def test_empty_affiliate_list_returns_none(self) -> None:
        assert _stage1_exact("acme", []) is None


# ─────────────────────────────────────────────────────────────────────────────
# RelatedPartyTagger — exact match (Stage 1)
# ─────────────────────────────────────────────────────────────────────────────

class TestStage1Tagging:
    def _tagger(self) -> RelatedPartyTagger:
        return RelatedPartyTagger(llm_enabled=False)

    def test_exact_match_sets_is_related_party(self) -> None:
        doc = _doc(_txn("NEFT/Acme Pvt Ltd/REF123", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].is_related_party is True

    def test_exact_match_sets_related_party_match_name(self) -> None:
        doc = _doc(_txn("NEFT/Acme Pvt Ltd/REF123", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].related_party_match == "Acme Pvt Ltd"

    def test_exact_match_sets_counterparty(self) -> None:
        doc = _doc(_txn("NEFT/Acme Pvt Ltd/REF123", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].counterparty == "acme"

    def test_non_matching_not_tagged(self) -> None:
        doc = _doc(_txn("NEFT/Razorpay/REF999", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].is_related_party is not True

    def test_non_matching_still_gets_counterparty(self) -> None:
        doc = _doc(_txn("NEFT/Razorpay/REF999", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].counterparty == "razorpay"

    def test_multiple_transactions_only_matching_tagged(self) -> None:
        t1 = _txn("NEFT/Acme Pvt Ltd/REF1", txn_id="t1")
        t2 = _txn("RTGS/Amazon Pay/REF2", txn_id="t2")
        doc = _doc(t1, t2)
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director_company")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].is_related_party is True
        assert result.transactions[1].is_related_party is not True

    def test_alias_match(self) -> None:
        doc = _doc(_txn("NEFT/Mehta Entr/INV001", txn_id="t1"))
        aff = Affiliate(
            name="Mehta Enterprises",
            relationship="director",
            aliases=["Mehta Entr"],
        )
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].is_related_party is True
        assert result.transactions[0].related_party_match == "Mehta Enterprises"

    def test_empty_affiliates_no_tagging(self) -> None:
        doc = _doc(_txn("Acme Pvt Ltd", txn_id="t1"))
        result = self._tagger().tag(doc, [])
        assert result.transactions[0].is_related_party is None

    def test_empty_affiliates_counterparty_populated(self) -> None:
        doc = _doc(_txn("NEFT/Acme Pvt Ltd/REF1", txn_id="t1"))
        result = self._tagger().tag(doc, [])
        assert result.transactions[0].counterparty == "acme"

    def test_empty_transactions_returns_doc(self) -> None:
        doc = _doc()
        aff = Affiliate(name="Acme", relationship="director")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions == []

    def test_multiple_affiliates_correct_one_matched(self) -> None:
        doc = _doc(_txn("RTGS/Mehta Enterprises", txn_id="t1"))
        aff1 = Affiliate(name="Acme Pvt Ltd", relationship="director")
        aff2 = Affiliate(name="Mehta Enterprises", relationship="founder_family")
        result = self._tagger().tag(doc, [aff1, aff2])
        assert result.transactions[0].related_party_match == "Mehta Enterprises"

    def test_neft_with_cr_stripped(self) -> None:
        doc = _doc(_txn("NEFT CR Mehta Enterprises REF12345", txn_id="t1"))
        aff = Affiliate(name="Mehta Enterprises", relationship="founder_family")
        result = self._tagger().tag(doc, [aff])
        assert result.transactions[0].is_related_party is True

    def test_all_transactions_get_counterparty(self) -> None:
        t1 = _txn("Salary payment", txn_id="t1")
        t2 = _txn("NEFT/Acme/REF1", txn_id="t2")
        doc = _doc(t1, t2)
        result = RelatedPartyTagger(llm_enabled=False).tag(doc, [])
        for txn in result.transactions:
            assert txn.counterparty is not None


# ─────────────────────────────────────────────────────────────────────────────
# RelatedPartyTagger — LLM fuzzy match (Stage 2)
# ─────────────────────────────────────────────────────────────────────────────

class TestStage2LLMTagging:
    """Stage 2 tests always mock _llm_call to avoid real API calls."""

    def _tagger_with_key(self) -> RelatedPartyTagger:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            return RelatedPartyTagger(llm_enabled=True)

    def test_llm_not_called_when_stage1_matches_all(self) -> None:
        doc = _doc(_txn("NEFT/Acme Pvt Ltd/REF1", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        with patch.object(tagger, "_llm_call") as mock_llm:
            tagger.tag(doc, [aff])
        mock_llm.assert_not_called()

    def test_llm_called_for_unmatched_transactions(self) -> None:
        doc = _doc(_txn("Some ambiguous narration XYZ", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        with patch.object(
            tagger, "_llm_call", return_value=[("Acme Pvt Ltd", 0.92)]
        ) as mock_llm:
            result = tagger.tag(doc, [aff])
        mock_llm.assert_called_once()
        assert result.transactions[0].is_related_party is True
        assert result.transactions[0].related_party_match == "Acme Pvt Ltd"

    def test_llm_match_below_threshold_not_tagged(self) -> None:
        doc = _doc(_txn("Some ambiguous narration XYZ", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        with patch.object(
            tagger, "_llm_call", return_value=[("Acme Pvt Ltd", 0.5)]
        ):
            result = tagger.tag(doc, [aff])
        assert result.transactions[0].is_related_party is not True

    def test_llm_null_match_not_tagged(self) -> None:
        doc = _doc(_txn("Random narration", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        with patch.object(
            tagger, "_llm_call", return_value=[(None, 0.0)]
        ):
            result = tagger.tag(doc, [aff])
        assert result.transactions[0].is_related_party is not True

    def test_llm_error_falls_back_gracefully(self) -> None:
        doc = _doc(_txn("Some narration", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        with patch.object(
            tagger, "_llm_call", side_effect=RuntimeError("API timeout")
        ):
            # Should not raise
            result = tagger.tag(doc, [aff])
        assert result.transactions[0].is_related_party is not True

    def test_llm_disabled_without_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            tagger = RelatedPartyTagger(llm_enabled=True)
        assert tagger._llm_enabled is False

    def test_llm_disabled_explicitly(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            tagger = RelatedPartyTagger(llm_enabled=False)
        assert tagger._llm_enabled is False

    def test_stage1_and_stage2_combined(self) -> None:
        """Stage 1 matches t1; Stage 2 matches t2 via LLM."""
        t1 = _txn("NEFT/Acme Pvt Ltd/REF1", txn_id="t1")
        t2 = _txn("Fuzzy Description", txn_id="t2")
        doc = _doc(t1, t2)
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        # LLM should be called only for t2 (unmatched)
        with patch.object(
            tagger, "_llm_call", return_value=[("Acme Pvt Ltd", 0.9)]
        ) as mock_llm:
            result = tagger.tag(doc, [aff])
        mock_llm.assert_called_once()
        assert result.transactions[0].is_related_party is True  # Stage 1
        assert result.transactions[1].is_related_party is True  # Stage 2

    def test_llm_unknown_affiliate_name_ignored(self) -> None:
        """LLM returns a name not in affiliate list — should not tag."""
        doc = _doc(_txn("Some narration", txn_id="t1"))
        aff = Affiliate(name="Acme Pvt Ltd", relationship="director")
        tagger = self._tagger_with_key()
        with patch.object(
            tagger, "_llm_call", return_value=[("Ghost Company", 0.95)]
        ):
            result = tagger.tag(doc, [aff])
        assert result.transactions[0].is_related_party is not True


# ─────────────────────────────────────────────────────────────────────────────
# _llm_call internals
# ─────────────────────────────────────────────────────────────────────────────

class TestLLMCallFallback:
    def test_import_error_returns_no_match(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            tagger = RelatedPartyTagger(llm_enabled=True)
        pairs = [("acme", ["Acme Pvt Ltd"])]

        with patch.dict("sys.modules", {"anthropic": None}):
            result = tagger._llm_call(pairs, ["Acme Pvt Ltd"])

        assert result == [(None, 0.0)]

    def test_malformed_json_falls_back(self) -> None:
        """Any exception inside _llm_call returns the no-match fallback."""
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            tagger = RelatedPartyTagger(llm_enabled=True)

        pairs = [("acme", ["Acme Pvt Ltd"])]
        # Patch at the json.loads level so we don't need the anthropic package
        with patch("pipeline.related_party.json.loads", side_effect=ValueError("bad json")):
            result = tagger._llm_call(pairs, ["Acme Pvt Ltd"])

        assert result == [(None, 0.0)]


# ─────────────────────────────────────────────────────────────────────────────
# Affiliate model
# ─────────────────────────────────────────────────────────────────────────────

class TestAffiliateModel:
    def test_name_required(self) -> None:
        with pytest.raises(Exception):
            Affiliate(relationship="director")  # type: ignore[call-arg]

    def test_relationship_required(self) -> None:
        with pytest.raises(Exception):
            Affiliate(name="Acme")  # type: ignore[call-arg]

    def test_aliases_default_empty(self) -> None:
        aff = Affiliate(name="Acme", relationship="director")
        assert aff.aliases == []

    def test_aliases_accepted(self) -> None:
        aff = Affiliate(name="Acme", relationship="director", aliases=["Acme Ltd"])
        assert "Acme Ltd" in aff.aliases
