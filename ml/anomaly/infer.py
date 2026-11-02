"""Serving side: score a StatementDocument and produce ``kind=ml`` flags.

``resolve_model()`` returns ``None`` when the model is disabled
(``ANOMALY_ML_ENABLED=false``), no version is configured, the artifact is
missing, or the sha256 check fails. The ``ml_anomaly`` stage is then
skipped: no ML flags, nothing else changes.

ML flags are a separate stream from the rule detectors' ``RiskReport.flags``
(docs/adr/0005). They don't feed the composite risk score, the narrative or the
gold regression. They read "investigate"; they are never a fraud finding.
"""
from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

from analysis.risk import Severity
from ml.anomaly import MODEL_NAME
from ml.anomaly.model import AnomalyModel, flag_mask, severity_of
from ml.features.transaction_features import build_from_document

if TYPE_CHECKING:
    from schema.canonical import StatementDocument

log = logging.getLogger(__name__)

DETECTOR_NAME = "ml_anomaly"
KIND_ML = "ml"

_FEATURE_PHRASES = {
    "log_amount": "amount size",
    "acct_amount_z": "amount vs this account's history",
    "is_credit": "direction",
    "cp_robust_z": "amount vs this counterparty's history",
    "cat_robust_z": "amount vs this category's history",
    "cp_first_seen": "first transaction with this counterparty",
    "cp_share_mtd_out": "counterparty's share of this month's outflows",
    "velocity_1d": "transactions that day",
    "velocity_7d": "transactions that week",
    "velocity_1d_rel": "transactions that day vs usual",
    "velocity_7d_rel": "transactions that week vs usual",
    "dow": "day of week",
    "month_end": "month-end timing",
    "days_since_cp": "time since last transaction with this counterparty",
}


@dataclass
class AnomalyFlag:
    """Duck-compatible with ``analysis.risk.Flag`` (detector_name, severity,
    triggering_transaction_ids, description, evidence), plus ``kind`` and ``score``."""

    detector_name: str
    severity: Severity
    triggering_transaction_ids: list[str]
    description: str
    score: float
    evidence: dict[str, Any] = field(default_factory=dict)
    kind: str = KIND_ML


@dataclass
class AnomalyReport:
    flags: list[AnomalyFlag]
    model_version: str
    n_scored: int
    scorer_counts: dict[str, int]


def describe(top: list[dict[str, Any]]) -> str:
    parts = [_FEATURE_PHRASES.get(t["feature"], t["feature"]) for t in top if t["deviation"] > 0]
    reason = "; ".join(parts) if parts else "unusual combination of features"
    return f"Unusual for this account — investigate. Largest deviations: {reason}."


class AnomalyDetector:
    def __init__(self, model: AnomalyModel, version: str, *, contamination: float,
                 severity_high_pct: float, severity_medium_pct: float,
                 max_flags_per_statement: int, min_history: int | None = None) -> None:
        self.model = model
        self.version = version
        self.contamination = contamination
        self.high = severity_high_pct
        self.medium = severity_medium_pct
        self.max_flags = max_flags_per_statement
        if min_history is not None:
            self.model.min_history = min_history

    @classmethod
    def from_config(cls, model: AnomalyModel, version: str) -> AnomalyDetector:
        from config.thresholds import get_thresholds

        t = get_thresholds().anomaly
        return cls(model, version, contamination=t.contamination,
                   severity_high_pct=t.severity_high_pct, severity_medium_pct=t.severity_medium_pct,
                   max_flags_per_statement=t.max_flags_per_statement, min_history=t.min_history)

    def detect(self, doc: StatementDocument) -> AnomalyReport:
        feats = build_from_document(doc)
        if feats.empty:
            return AnomalyReport([], self.version, 0, {})
        scored = self.model.score_account(feats)
        scored["account_id"] = doc.account_id
        flagged = flag_mask(scored, self.contamination, self.max_flags)
        sub = scored[flagged].sort_values(["percentile", "score", "txn_id"],
                                          ascending=[False, False, True], kind="mergesort")
        flags = []
        for r in sub.itertuples(index=False):
            sev = Severity(severity_of(float(r.percentile), self.high, self.medium))
            flags.append(AnomalyFlag(
                detector_name=DETECTOR_NAME, severity=sev,
                triggering_transaction_ids=[str(r.txn_id)],
                description=describe(r.top_features), score=round(float(r.score), 6),
                evidence={"score": round(float(r.score), 6),
                          "percentile": round(float(r.percentile), 6),
                          "scorer": r.scorer, "model_version": self.version,
                          "top_features": r.top_features,
                          "note": "Unsupervised outlier score; a prompt to investigate, not a finding."},
            ))
        counts = Counter(scored["scorer"])
        return AnomalyReport(flags, self.version, int(len(scored)),
                             {str(k): int(v) for k, v in counts.items()})


@lru_cache(maxsize=4)
def _load_cached(version: str, root: str) -> AnomalyModel:
    from ml import registry

    loaded = registry.load(MODEL_NAME, version, root=Path(root))
    model: AnomalyModel = loaded.model
    return model


def resolve_model(version: str | None = None, root: Path | None = None) -> tuple[AnomalyModel, str] | None:
    """The configured anomaly model and its version, or None (stage skipped). Never raises."""
    from config.settings import get_settings
    from config.thresholds import get_thresholds
    from ml import registry

    settings = get_settings()
    if not settings.anomaly_ml_enabled:
        return None
    version = version or settings.anomaly_model_version or get_thresholds().anomaly.model_version
    if not version or version == "unset":
        return None
    if root is None:
        md = Path(settings.models_dir)
        root = md if md.is_absolute() else registry.REPO_ROOT / md
    if not registry.exists(MODEL_NAME, version, root):
        log.warning("anomaly model %s not found; ml_anomaly stage skipped", version)
        return None
    try:
        return _load_cached(version, str(root)), version
    except Exception as exc:  # corrupted / sha mismatch / incompatible sklearn
        log.warning("anomaly model %s failed to load (%s); ml_anomaly stage skipped",
                    version, type(exc).__name__)
        return None


def resolve_detector() -> AnomalyDetector | None:
    res = resolve_model()
    if res is None:
        return None
    model, version = res
    # copy so a thresholds min_history override doesn't mutate the cached model
    import copy

    return AnomalyDetector.from_config(copy.copy(model), version)

