"""Customer identity resolver — groups REVENUE transactions by customer.

Three-stage pipeline:
  Stage 1 (normalise) — strip noise from description/counterparty to a
    canonical entity name (reuses the same cleaning logic as the related-party
    tagger so the two stages compose cleanly).
  Stage 2 (exact cluster) — group REVENUE transactions by exact canonical name.
    Generate a deterministic customer_id: "cust_" + SHA-1(name)[:10].
  Stage 3 (embedding merge) — compute sentence-transformers embeddings for
    each cluster representative; merge clusters whose cosine similarity exceeds
    0.85.  Requires the ``sentence-transformers`` package (optional).
    Embeddings are cached to .cache/embeddings.json so the model only runs
    on novel names.

Usage:
    from pipeline.customer_identity import CustomerIdentityResolver

    doc = CustomerIdentityResolver().resolve(doc)
    # All REVENUE transactions now have customer_id set.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
from pathlib import Path

from pipeline.related_party import _clean_name as _strip_noise
from schema.canonical import StatementDocument, TransactionCategory

log = logging.getLogger(__name__)

_EMBEDDING_THRESHOLD = 0.85
_CACHE_PATH = Path(".cache/embeddings.json")

# Payment-gateway / aggregator settlement narrations are NOT individual
# customers — a single settlement line is a lump-sum payout covering however
# many end-customers paid through that gateway that day, and bank data alone
# can never reveal who they were. Clustering these under one "customer" (as
# exact-name matching would otherwise do) silently fabricates a fake customer
# with implausibly high, steady revenue and zero churn. These are excluded
# from customer_id assignment entirely rather than clustered.
ANOMALY_FLAG_AGGREGATOR_SETTLEMENT = "aggregator_settlement"

_KNOWN_AGGREGATORS = [
    "RAZORPAY", "CASHFREE", "PAYU", "PAYTM", "PHONEPE", "INSTAMOJO",
    "CCAVENUE", "CC AVENUE", "BILLDESK", "PINE\\s*LABS", "JUSPAY", "EBS",
    "ATOM\\s*TECHNOLOGIES", "WORLDLINE",
]
_AGGREGATOR_RE = re.compile(
    r"\b(" + "|".join(_KNOWN_AGGREGATORS) + r")\b", re.IGNORECASE
)


# ─────────────────────────────────────────────────────────────────────────────
# Public helpers
# ─────────────────────────────────────────────────────────────────────────────

def clean_counterparty(name: str) -> str:
    """Normalise a bank narration or counterparty to a canonical entity name.

    Strips payment prefixes, reference numbers, legal-form suffixes, and noise.
    Returns a lowercase, whitespace-normalised string, or "" if nothing remains.
    """
    return _strip_noise(name)


def matched_aggregator(description: str) -> str | None:
    """Return the matched aggregator name if *description* looks like a
    payment-gateway settlement narration, else None."""
    m = _AGGREGATOR_RE.search(description)
    return m.group(1).upper() if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Internal utilities
# ─────────────────────────────────────────────────────────────────────────────

def _customer_id(canonical_name: str) -> str:
    return "cust_" + hashlib.sha1(canonical_name.encode()).hexdigest()[:10]


def _cosine_sim(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(x * x for x in b))
    return dot / mag if mag > 0 else 0.0


class _UnionFind:
    def __init__(self, items: list[str]) -> None:
        self._parent: dict[str, str] = {x: x for x in items}

    def find(self, x: str) -> str:
        while self._parent[x] != x:
            self._parent[x] = self._parent[self._parent[x]]  # path compression
            x = self._parent[x]
        return x

    def union(self, x: str, y: str) -> None:
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self._parent[rx] = ry

    def components(self) -> dict[str, list[str]]:
        """Returns {root: [members]} for every connected component."""
        out: dict[str, list[str]] = {}
        for item in self._parent:
            out.setdefault(self.find(item), []).append(item)
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Resolver
# ─────────────────────────────────────────────────────────────────────────────

class CustomerIdentityResolver:
    """Resolves customer identities for all REVENUE transactions in a statement."""

    def __init__(
        self,
        embedding_enabled: bool = True,
        _model: object | None = None,
    ) -> None:
        """
        Args:
            embedding_enabled: If False, skip Stage 3 even if the package is
                installed.  Useful in tests and for cost-sensitive deployments.
            _model: Inject a pre-loaded SentenceTransformer (or mock) for
                testing without needing to hit disk.
        """
        if _model is not None:
            self._model: object | None = _model
            self._embedding_enabled = True
        elif embedding_enabled:
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
                self._model = SentenceTransformer("all-MiniLM-L6-v2")
                self._embedding_enabled = True
                log.debug("CustomerIdentityResolver: embedding model loaded")
            except ImportError:
                log.warning(
                    "sentence-transformers not installed — "
                    "CustomerIdentityResolver using exact-match only (Stage 2)"
                )
                self._model = None
                self._embedding_enabled = False
        else:
            self._model = None
            self._embedding_enabled = False

    # ── Public API ────────────────────────────────────────────────────────────

    def resolve(self, doc: StatementDocument) -> StatementDocument:
        """Populate customer_id on all REVENUE transactions.

        Non-REVENUE transactions are left untouched (customer_id stays None).
        Payment-gateway/aggregator settlement transactions (Razorpay, Cashfree,
        PayU, ...) are excluded from clustering — see matched_aggregator() —
        and tagged with the aggregator_settlement anomaly flag instead of a
        customer_id, since a settlement line is not an individual customer.
        Returns the same doc object with transactions updated in place.
        """
        revenue_indices = [
            i for i, t in enumerate(doc.transactions)
            if t.category == TransactionCategory.REVENUE
        ]
        if not revenue_indices:
            return doc

        aggregator_indices = [
            i for i in revenue_indices
            if matched_aggregator(doc.transactions[i].description)
        ]
        clusterable_indices = [i for i in revenue_indices if i not in set(aggregator_indices)]

        for i in aggregator_indices:
            txn = doc.transactions[i]
            if ANOMALY_FLAG_AGGREGATOR_SETTLEMENT not in txn.anomaly_flags:
                doc.transactions[i] = txn.model_copy(
                    update={"anomaly_flags": [*txn.anomaly_flags, ANOMALY_FLAG_AGGREGATOR_SETTLEMENT]}
                )

        if not clusterable_indices:
            return doc

        # Stage 1: canonical name per transaction
        canonical: dict[int, str] = {}
        for i in clusterable_indices:
            txn = doc.transactions[i]
            source = txn.counterparty if txn.counterparty is not None else txn.description
            canonical[i] = clean_counterparty(source) or txn.description.lower().strip()

        # Stage 2: exact clustering — one customer_id per unique canonical name
        unique_names: list[str] = list(dict.fromkeys(canonical.values()))
        name_to_id: dict[str, str] = {n: _customer_id(n) for n in unique_names}

        # Stage 3: embedding merge (optional)
        if self._embedding_enabled and len(unique_names) > 1:
            name_to_id = self._merge_via_embeddings(unique_names, name_to_id)

        # Apply customer_ids
        for i in clusterable_indices:
            cid = name_to_id[canonical[i]]
            doc.transactions[i] = doc.transactions[i].model_copy(
                update={"customer_id": cid}
            )

        n_unique = len({name_to_id[n] for n in unique_names})
        log.info(
            "CustomerIdentityResolver: %d REVENUE transactions → %d distinct customers "
            "(%d aggregator-settlement transactions excluded)",
            len(clusterable_indices),
            n_unique,
            len(aggregator_indices),
        )
        return doc

    # ── Internal ──────────────────────────────────────────────────────────────

    def _merge_via_embeddings(
        self,
        names: list[str],
        name_to_id: dict[str, str],
    ) -> dict[str, str]:
        embeddings = self._get_embeddings(names)
        uf = _UnionFind(names)

        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                if _cosine_sim(embeddings[i], embeddings[j]) > _EMBEDDING_THRESHOLD:
                    uf.union(names[i], names[j])

        # Every name in a component shares the customer_id of the component root
        result: dict[str, str] = {}
        for name in names:
            root = uf.find(name)
            result[name] = name_to_id[root]

        return result

    def _get_embeddings(self, names: list[str]) -> list[list[float]]:
        """Return embedding vectors, using cache for already-seen names."""
        cache = _load_embedding_cache()
        missing = [n for n in names if n not in cache]

        if missing and self._model is not None:
            vecs = self._model.encode(missing, show_progress_bar=False)  # type: ignore[union-attr]
            for name, vec in zip(missing, vecs):
                # numpy arrays have .tolist(); plain lists do not
                cache[name] = vec.tolist() if hasattr(vec, "tolist") else list(vec)
            _save_embedding_cache(cache)

        return [cache.get(n, [0.0]) for n in names]


# ─────────────────────────────────────────────────────────────────────────────
# Embedding cache
# ─────────────────────────────────────────────────────────────────────────────

def _load_embedding_cache() -> dict[str, list[float]]:
    if _CACHE_PATH.exists():
        try:
            return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_embedding_cache(cache: dict[str, list[float]]) -> None:
    _CACHE_PATH.parent.mkdir(exist_ok=True)
    _CACHE_PATH.write_text(
        json.dumps(cache, separators=(",", ":")), encoding="utf-8"
    )
