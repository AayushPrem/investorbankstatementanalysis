"""Serving side: churn risk per active customer of one statement.

``resolve_scorer()`` returns ``None`` when the model is disabled
(``CHURN_ML_ENABLED=false``), no version is configured, the artifact is
missing, or the sha256 check fails. ``CustomerAnalyticsAnalyst`` then
produces no ``churn_risk`` at all, and nothing else changes.

Identity: the model's units are narration counterparty keys (docs/ml/churn.md
§Identity). Each active resolver ``customer_id`` takes the risk of the key
that covers most of its rows. Rule churn events are computed separately by
``analysis/customer_analytics.py`` and are never touched here.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from analysis.customer_analytics import ChurnRisk
from ml.churn import MODEL_NAME
from ml.churn.model import ChurnModel
from ml.features.churn_features import features_at, payment_key, payments_from_document

if TYPE_CHECKING:
    from schema.canonical import StatementDocument

log = logging.getLogger(__name__)

SOURCE_MODEL = "model"
SOURCE_RULE = "rule"


class ChurnScorer:
    def __init__(self, model: ChurnModel, version: str, *, decision_threshold: float,
                 band_medium: float, min_history_payments: int, active_window_days: int,
                 rule_silence_days: int) -> None:
        self.model = model
        self.version = version
        self.threshold = decision_threshold
        self.band_medium = band_medium
        self.min_history = min_history_payments
        self.active_window = active_window_days
        self.rule_silence = rule_silence_days

    @classmethod
    def from_config(cls, model: ChurnModel, version: str) -> ChurnScorer:
        from config.thresholds import get_thresholds

        t = get_thresholds().churn
        return cls(model, version, decision_threshold=t.decision_threshold,
                   band_medium=t.band_medium, min_history_payments=t.min_history_payments,
                   active_window_days=t.active_window_days, rule_silence_days=t.rule_silence_days)

    def band(self, p: float) -> str:
        if p >= self.threshold:
            return "high"
        if p >= self.band_medium:
            return "medium"
        return "low"

    def score(self, doc: StatementDocument) -> dict[str, ChurnRisk]:
        """ChurnRisk per active resolver customer_id (paid in the last active_window days)."""
        if not doc.transactions:
            return {}
        cutoff: date = max(t.date for t in doc.transactions)
        c = cutoff.toordinal()

        # Resolver customers: their rows' dates and narration keys.
        cust_dates: dict[str, list[date]] = {}
        cust_keys: dict[str, Counter[str]] = {}
        for t in doc.transactions:
            if not t.customer_id or t.credit is None:
                continue
            cust_dates.setdefault(t.customer_id, []).append(t.date)
            k = payment_key(t.description)
            if k is not None:
                cust_keys.setdefault(t.customer_id, Counter())[k] += 1
        active = sorted(cid for cid, ds in cust_dates.items()
                        if c - max(ds).toordinal() <= self.active_window)
        if not active:
            return {}

        feats = features_at(payments_from_document(doc), cutoff)
        probs: dict[str, tuple[float, int]] = {}
        if not feats.empty:
            p = self.model.predict_proba(feats)
            probs = {str(k): (float(v), int(n)) for k, v, n in
                     zip(feats["key"], p, feats["n_payments"], strict=True)}

        out: dict[str, ChurnRisk] = {}
        for cid in active:
            keys = cust_keys.get(cid)
            key = min(keys.items(), key=lambda kv: (-kv[1], kv[0]))[0] if keys else None
            hit = probs.get(key) if key is not None else None
            if hit is not None and hit[1] >= self.min_history:
                prob, n = hit
                out[cid] = ChurnRisk(customer_id=cid, band=self.band(prob), source=SOURCE_MODEL,
                                     score=round(prob, 4), n_payments=n, model_version=self.version)
            else:
                since = c - max(cust_dates[cid]).toordinal()
                out[cid] = ChurnRisk(customer_id=cid,
                                     band="elevated" if since > self.rule_silence else "low",
                                     source=SOURCE_RULE, score=None,
                                     n_payments=len(cust_dates[cid]), model_version=None)
        return out


@lru_cache(maxsize=4)
def _load_cached(version: str, root: str) -> ChurnModel:
    from ml import registry

    loaded = registry.load(MODEL_NAME, version, root=Path(root))
    model: ChurnModel = loaded.model
    return model


def resolve_model(version: str | None = None, root: Path | None = None) -> tuple[ChurnModel, str] | None:
    """The configured churn model and its version, or None. Never raises."""
    from config.settings import get_settings
    from config.thresholds import get_thresholds
    from ml import registry

    settings = get_settings()
    if not settings.churn_ml_enabled:
        return None
    version = version or settings.churn_model_version or get_thresholds().churn.model_version
    if not version or version == "unset":
        return None
    if root is None:
        md = Path(settings.models_dir)
        root = md if md.is_absolute() else registry.REPO_ROOT / md
    if not registry.exists(MODEL_NAME, version, root):
        log.warning("churn model %s not found; no churn_risk", version)
        return None
    try:
        return _load_cached(version, str(root)), version
    except Exception as exc:  # corrupted / sha mismatch / incompatible sklearn
        log.warning("churn model %s failed to load (%s); no churn_risk", version, type(exc).__name__)
        return None


def resolve_scorer() -> ChurnScorer | None:
    res = resolve_model()
    if res is None:
        return None
    model, version = res
    return ChurnScorer.from_config(model, version)
