"""Tests for pipeline.customer_identity — CustomerIdentityResolver (Step 2.2)."""
from __future__ import annotations

import datetime
import math
from decimal import Decimal
from unittest.mock import patch

import pytest

from pipeline.customer_identity import (
    CustomerIdentityResolver,
    _UnionFind,
    _cosine_sim,
    _customer_id,
    clean_counterparty,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _src(row: int = 0) -> SourceReference:
    return SourceReference(file_path="test.pdf", page=1, row=row, raw_text="")


def _txn(
    desc: str,
    *,
    txn_id: str,
    category: TransactionCategory = TransactionCategory.REVENUE,
    credit: Decimal | None = None,
    debit: Decimal | None = None,
    counterparty: str | None = None,
) -> CanonicalTransaction:
    if credit is None and debit is None:
        credit = Decimal("1000")
    return CanonicalTransaction(
        transaction_id=txn_id,
        date=datetime.date(2024, 4, 1),
        description=desc,
        credit=credit,
        debit=debit,
        balance=Decimal("50000"),
        category=category,
        counterparty=counterparty,
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


def _resolver(embedding_enabled: bool = False) -> CustomerIdentityResolver:
    """Return a resolver with embeddings disabled (for Stage 2 unit tests)."""
    return CustomerIdentityResolver(embedding_enabled=embedding_enabled)


# ─────────────────────────────────────────────────────────────────────────────
# clean_counterparty
# ─────────────────────────────────────────────────────────────────────────────

class TestCleanCounterparty:
    def test_strips_neft_prefix(self) -> None:
        assert clean_counterparty("NEFT/Acme Pvt Ltd/INV-001") == "acme"

    def test_strips_rtgs_prefix(self) -> None:
        assert clean_counterparty("RTGS/Brightpath Inc") == "brightpath"

    def test_strips_imps_prefix(self) -> None:
        assert clean_counterparty("IMPS/Sunrise Technologies/REF99") == "sunrise"

    def test_strips_upi_prefix(self) -> None:
        assert clean_counterparty("UPI-Razorpay-Ltd") == "razorpay"

    def test_strips_by_transfer_from(self) -> None:
        assert clean_counterparty("BY TRANSFER FROM ACME PVT LTD") == "acme"

    def test_strips_inward_clearing(self) -> None:
        assert clean_counterparty("INWARD CLEARING SUNRISE SOLUTIONS") == "sunrise"

    def test_strips_pvt_ltd(self) -> None:
        assert clean_counterparty("Acme Pvt Ltd") == "acme"

    def test_strips_private_limited(self) -> None:
        assert clean_counterparty("Acme Private Limited") == "acme"

    def test_strips_llp(self) -> None:
        assert clean_counterparty("Sharma & Co LLP") == "sharma & co"

    def test_strips_trailing_inv(self) -> None:
        assert clean_counterparty("Mehta Enterprises INV-2024-001") == "mehta"

    def test_strips_bank_ref_sbin(self) -> None:
        result = clean_counterparty("NEFT/SBIN0000001/ACME PVT LTD/INV-447")
        assert result == "acme"

    def test_normalises_whitespace(self) -> None:
        assert clean_counterparty("  Acme   Ltd  ") == "acme"

    def test_lowercase(self) -> None:
        assert clean_counterparty("ACME") == "acme"

    def test_empty_string(self) -> None:
        assert clean_counterparty("") == ""

    def test_idempotent_on_already_clean(self) -> None:
        assert clean_counterparty("acme") == "acme"


# ─────────────────────────────────────────────────────────────────────────────
# _customer_id
# ─────────────────────────────────────────────────────────────────────────────

class TestCustomerId:
    def test_format(self) -> None:
        cid = _customer_id("acme")
        assert cid.startswith("cust_")
        assert len(cid) == len("cust_") + 10

    def test_deterministic(self) -> None:
        assert _customer_id("acme") == _customer_id("acme")

    def test_different_names_differ(self) -> None:
        assert _customer_id("acme") != _customer_id("apex")


# ─────────────────────────────────────────────────────────────────────────────
# _cosine_sim
# ─────────────────────────────────────────────────────────────────────────────

class TestCosineSim:
    def test_identical_vectors(self) -> None:
        v = [1.0, 0.0, 0.0]
        assert abs(_cosine_sim(v, v) - 1.0) < 1e-9

    def test_orthogonal_vectors(self) -> None:
        assert abs(_cosine_sim([1.0, 0.0], [0.0, 1.0])) < 1e-9

    def test_opposite_vectors(self) -> None:
        sim = _cosine_sim([1.0, 0.0], [-1.0, 0.0])
        assert abs(sim - (-1.0)) < 1e-9

    def test_zero_vector_returns_zero(self) -> None:
        assert _cosine_sim([0.0, 0.0], [1.0, 1.0]) == 0.0

    def test_high_similarity(self) -> None:
        sim = _cosine_sim([1.0, 0.1], [1.0, 0.05])
        assert sim > 0.98


# ─────────────────────────────────────────────────────────────────────────────
# _UnionFind
# ─────────────────────────────────────────────────────────────────────────────

class TestUnionFind:
    def test_find_self(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        assert uf.find("a") == "a"

    def test_union_merges_components(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        assert uf.find("a") == uf.find("b")

    def test_union_is_transitive(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        uf.union("b", "c")
        assert uf.find("a") == uf.find("c")

    def test_disjoint_sets_remain_separate(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        assert uf.find("a") != uf.find("c")

    def test_components(self) -> None:
        uf = _UnionFind(["a", "b", "c"])
        uf.union("a", "b")
        comps = uf.components()
        members = {frozenset(v) for v in comps.values()}
        assert frozenset({"a", "b"}) in members
        assert frozenset({"c"}) in members


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — exact clustering
# ─────────────────────────────────────────────────────────────────────────────

class TestStage2ExactClustering:
    def test_single_revenue_transaction_gets_customer_id(self) -> None:
        doc = _doc(_txn("Acme Pvt Ltd", txn_id="t1"))
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id is not None
        assert result.transactions[0].customer_id.startswith("cust_")

    def test_same_name_variations_share_customer_id(self) -> None:
        """'Acme Pvt Ltd' and 'Acme Private Limited' both clean to 'acme'."""
        t1 = _txn("Acme Pvt Ltd", txn_id="t1")
        t2 = _txn("Acme Private Limited", txn_id="t2")
        doc = _doc(t1, t2)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id == result.transactions[1].customer_id

    def test_neft_with_bank_ref_and_invoice_resolves_to_same_id(self) -> None:
        """'NEFT/SBIN0000001/ACME PVT LTD/INV-447' → same customer_id as 'Acme Pvt Ltd'."""
        t1 = _txn("NEFT/SBIN0000001/ACME PVT LTD/INV-447", txn_id="t1")
        t2 = _txn("Acme Pvt Ltd", txn_id="t2")
        doc = _doc(t1, t2)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id == result.transactions[1].customer_id

    def test_rtgs_prefix_stripped(self) -> None:
        t1 = _txn("RTGS/Sunrise Technologies Ltd", txn_id="t1")
        t2 = _txn("Sunrise Technologies", txn_id="t2")
        doc = _doc(t1, t2)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id == result.transactions[1].customer_id

    def test_different_customers_get_different_ids(self) -> None:
        t1 = _txn("Acme Pvt Ltd", txn_id="t1")
        t2 = _txn("Apex Corp", txn_id="t2")
        doc = _doc(t1, t2)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id != result.transactions[1].customer_id

    def test_non_revenue_transactions_not_touched(self) -> None:
        t_rev = _txn("Acme Pvt Ltd", txn_id="t1", category=TransactionCategory.REVENUE)
        t_ven = _txn(
            "Vendor Payment",
            txn_id="t2",
            category=TransactionCategory.VENDOR_PAYMENT,
            debit=Decimal("5000"),
            credit=None,
        )
        doc = _doc(t_rev, t_ven)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id is not None
        assert result.transactions[1].customer_id is None

    def test_only_vendor_payment_transactions(self) -> None:
        """No REVENUE transactions → doc returned with no customer_ids set."""
        t = _txn(
            "AWS Cloud",
            txn_id="t1",
            category=TransactionCategory.VENDOR_PAYMENT,
            debit=Decimal("3000"),
            credit=None,
        )
        doc = _doc(t)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id is None

    def test_empty_document(self) -> None:
        doc = _doc()
        result = _resolver().resolve(doc)
        assert result.transactions == []

    def test_counterparty_field_used_when_set(self) -> None:
        """If counterparty is already set (by related-party tagger), use it."""
        t1 = _txn("NEFT/ACME PVT LTD/REF001", txn_id="t1", counterparty="acme")
        t2 = _txn("Acme Pvt Ltd", txn_id="t2")  # counterparty=None → cleans description
        doc = _doc(t1, t2)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id == result.transactions[1].customer_id

    def test_multiple_transactions_same_customer(self) -> None:
        """Monthly payments from same customer all share one customer_id."""
        txns = [_txn("Acme Pvt Ltd", txn_id=f"t{i}") for i in range(4)]
        doc = _doc(*txns)
        result = _resolver().resolve(doc)
        ids = {t.customer_id for t in result.transactions}
        assert len(ids) == 1

    def test_customer_id_is_stable_across_calls(self) -> None:
        doc1 = _doc(_txn("Acme Pvt Ltd", txn_id="t1"))
        doc2 = _doc(_txn("Acme Pvt Ltd", txn_id="t2"))
        r1 = _resolver().resolve(doc1)
        r2 = _resolver().resolve(doc2)
        assert r1.transactions[0].customer_id == r2.transactions[0].customer_id

    def test_customer_id_prefix(self) -> None:
        doc = _doc(_txn("Acme Pvt Ltd", txn_id="t1"))
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id.startswith("cust_")  # type: ignore[union-attr]

    def test_customer_id_length(self) -> None:
        doc = _doc(_txn("Acme Pvt Ltd", txn_id="t1"))
        result = _resolver().resolve(doc)
        cid = result.transactions[0].customer_id
        assert cid is not None and len(cid) == 15  # "cust_" (5) + 10 hex chars


# ─────────────────────────────────────────────────────────────────────────────
# Stage 3 — embedding merge
# ─────────────────────────────────────────────────────────────────────────────

class _MockModel:
    """Sentence-transformer stand-in that returns preset embeddings."""

    def __init__(self, embed_map: dict[str, list[float]]) -> None:
        self._map = embed_map

    def encode(self, names: list[str], **_: object) -> list[list[float]]:
        return [self._map.get(n, [0.0]) for n in names]


def _unit_vec(components: list[float]) -> list[float]:
    mag = math.sqrt(sum(x * x for x in components))
    return [x / mag for x in components]


class TestStage3EmbeddingMerge:
    def test_high_similarity_merges_clusters(self) -> None:
        """Names 'acme pl' and 'acme' have cosine sim > 0.85 → same customer_id."""
        # Vectors that are close together
        embed_map = {
            "acme": _unit_vec([1.0, 0.05]),
            "acme pl": _unit_vec([1.0, 0.10]),
        }
        model = _MockModel(embed_map)
        resolver = CustomerIdentityResolver(_model=model)

        t1 = _txn("Acme Pvt Ltd", txn_id="t1")         # cleans to "acme"
        t2 = _txn("Acme PL", txn_id="t2")              # cleans to "acme pl"
        doc = _doc(t1, t2)
        result = resolver.resolve(doc)
        assert result.transactions[0].customer_id == result.transactions[1].customer_id

    def test_low_similarity_keeps_clusters_separate(self) -> None:
        """Names 'acme' and 'apex' are different → different customer_ids."""
        embed_map = {
            "acme": _unit_vec([1.0, 0.0]),
            "apex": _unit_vec([0.0, 1.0]),  # orthogonal → sim = 0
        }
        model = _MockModel(embed_map)
        resolver = CustomerIdentityResolver(_model=model)

        t1 = _txn("Acme Pvt Ltd", txn_id="t1")
        t2 = _txn("Apex Corp", txn_id="t2")
        doc = _doc(t1, t2)
        result = resolver.resolve(doc)
        assert result.transactions[0].customer_id != result.transactions[1].customer_id

    def test_three_way_merge_via_transitivity(self) -> None:
        """a≈b and b≈c but a and c might not be explicitly close — union-find handles it."""
        embed_map = {
            "acme": _unit_vec([1.0, 0.0]),
            "acme pl": _unit_vec([0.98, 0.1]),
            "acme pvt": _unit_vec([0.97, 0.15]),
        }
        model = _MockModel(embed_map)
        resolver = CustomerIdentityResolver(_model=model)

        txns = [
            _txn("Acme Pvt Ltd", txn_id="t1"),   # → "acme"
            _txn("ACME PL", txn_id="t2"),         # → "acme pl"
            _txn("Acme Pvt", txn_id="t3"),        # → "acme pvt"
        ]
        doc = _doc(*txns)
        result = resolver.resolve(doc)
        ids = {t.customer_id for t in result.transactions}
        assert len(ids) == 1

    def test_stage3_skipped_when_disabled(self) -> None:
        """With embedding_enabled=False, two slightly different names stay separate."""
        t1 = _txn("Acme PL", txn_id="t1")
        t2 = _txn("Acme Pvt Ltd", txn_id="t2")   # cleans to "acme" ≠ "acme pl"
        doc = _doc(t1, t2)
        result = _resolver(embedding_enabled=False).resolve(doc)
        # "acme pl" ≠ "acme" → different ids (no embeddings to merge them)
        assert result.transactions[0].customer_id != result.transactions[1].customer_id

    def test_get_embeddings_uses_mock_model(self) -> None:
        import pipeline.customer_identity as mod

        embed_map = {"acme": [1.0, 0.0]}
        model = _MockModel(embed_map)
        resolver = CustomerIdentityResolver(_model=model)

        # Patch the cache so a previous test's disk-write doesn't shadow the mock
        with patch.object(mod, "_load_embedding_cache", return_value={}):
            with patch.object(mod, "_save_embedding_cache"):
                result = resolver._get_embeddings(["acme"])

        assert result == [[1.0, 0.0]]

    def test_model_not_called_for_cached_names(self) -> None:
        """If name is in cache, model.encode is not called."""
        import pipeline.customer_identity as mod

        embed_map = {"acme": [1.0, 0.0]}
        model = _MockModel(embed_map)
        resolver = CustomerIdentityResolver(_model=model)

        # Pre-populate cache
        with patch.object(mod, "_load_embedding_cache", return_value={"acme": [1.0, 0.0]}):
            with patch.object(mod, "_save_embedding_cache"):
                with patch.object(model, "encode") as mock_encode:
                    resolver._get_embeddings(["acme"])

        mock_encode.assert_not_called()

    def test_import_error_falls_back_to_exact_only(self) -> None:
        """If sentence-transformers isn't installed, Stage 3 is silently disabled."""
        with patch.dict("sys.modules", {"sentence_transformers": None}):
            resolver = CustomerIdentityResolver(embedding_enabled=True)
        assert resolver._embedding_enabled is False


# ─────────────────────────────────────────────────────────────────────────────
# Embedding cache
# ─────────────────────────────────────────────────────────────────────────────

class TestEmbeddingCache:
    def test_save_and_load_roundtrip(self, tmp_path: pytest.TempdirFactory) -> None:
        import pipeline.customer_identity as mod
        cache_path = tmp_path / "embeddings.json"

        with patch.object(mod, "_CACHE_PATH", cache_path):
            mod._save_embedding_cache({"acme": [0.1, 0.2, 0.3]})
            loaded = mod._load_embedding_cache()

        assert loaded == {"acme": [0.1, 0.2, 0.3]}

    def test_load_missing_file_returns_empty(self, tmp_path: pytest.TempdirFactory) -> None:
        import pipeline.customer_identity as mod

        missing = tmp_path / "not_here.json"
        with patch.object(mod, "_CACHE_PATH", missing):
            result = mod._load_embedding_cache()
        assert result == {}

    def test_load_corrupt_file_returns_empty(self, tmp_path: pytest.TempdirFactory) -> None:
        import pipeline.customer_identity as mod
        bad = tmp_path / "bad.json"
        bad.write_text("this is not json", encoding="utf-8")

        with patch.object(mod, "_CACHE_PATH", bad):
            result = mod._load_embedding_cache()
        assert result == {}


# ─────────────────────────────────────────────────────────────────────────────
# Integration — multiple categories in one document
# ─────────────────────────────────────────────────────────────────────────────

class TestIntegration:
    def test_mixed_category_document(self) -> None:
        """Only REVENUE transactions get customer_ids."""
        rev1 = _txn("Acme Pvt Ltd", txn_id="r1", category=TransactionCategory.REVENUE)
        rev2 = _txn("Apex Corp", txn_id="r2", category=TransactionCategory.REVENUE)
        sal = _txn(
            "Salary",
            txn_id="s1",
            category=TransactionCategory.SALARY,
            debit=Decimal("80000"),
            credit=None,
        )
        tax = _txn(
            "GST Payment",
            txn_id="x1",
            category=TransactionCategory.TAX,
            debit=Decimal("12000"),
            credit=None,
        )
        doc = _doc(rev1, rev2, sal, tax)
        result = _resolver().resolve(doc)

        rev_txns = [t for t in result.transactions if t.category == TransactionCategory.REVENUE]
        non_rev = [t for t in result.transactions if t.category != TransactionCategory.REVENUE]

        assert all(t.customer_id is not None for t in rev_txns)
        assert all(t.customer_id is None for t in non_rev)

    def test_revenue_and_related_party_tagger_compose(self) -> None:
        """RelatedPartyTagger sets counterparty; CustomerIdentityResolver uses it."""
        # Simulate tagger already ran: counterparty is set to cleaned name
        t1 = _txn(
            "NEFT/ACME PVT LTD/REF0001",
            txn_id="t1",
            counterparty="acme",
        )
        t2 = _txn("Acme Pvt Ltd", txn_id="t2")
        doc = _doc(t1, t2)
        result = _resolver().resolve(doc)
        assert result.transactions[0].customer_id == result.transactions[1].customer_id

    def test_repeated_monthly_payments_same_customer(self) -> None:
        """Six payments from the same company → one customer_id."""
        txns = [
            _txn(f"NEFT/Acme Pvt Ltd/INV-00{i}", txn_id=f"t{i}")
            for i in range(6)
        ]
        doc = _doc(*txns)
        result = _resolver().resolve(doc)
        ids = {t.customer_id for t in result.transactions}
        assert len(ids) == 1

    def test_five_distinct_customers(self) -> None:
        customers = ["Acme Ltd", "Bright Corp", "Clarity Inc", "Delta Solutions", "Echo"]
        txns = [_txn(name, txn_id=f"t{i}") for i, name in enumerate(customers)]
        doc = _doc(*txns)
        result = _resolver().resolve(doc)
        ids = {t.customer_id for t in result.transactions}
        assert len(ids) == 5
