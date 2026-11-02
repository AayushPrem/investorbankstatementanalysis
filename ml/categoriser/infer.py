"""Serving-side wrapper around a registered categoriser model.

``resolve_model()`` is what ``analysis.categoriser.Categoriser`` calls. It returns
``None`` (→ v2 rules-only behaviour) when ML is disabled, no version is
configured, or the artifact is missing or fails its sha256 check, so
the pipeline never hard-fails because a model isn't there.
"""
from __future__ import annotations

import logging
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

from ml.categoriser import MODEL_NAME
from ml.features.categoriser import build_frame_from_transactions

if TYPE_CHECKING:
    from schema.canonical import CanonicalTransaction

log = logging.getLogger(__name__)


class CategoriserModel:
    """A fitted sklearn Pipeline (column transformer + classifier) + metadata."""

    def __init__(self, pipeline: Any, metadata: dict[str, Any] | None = None) -> None:
        self.pipeline = pipeline
        self.metadata = metadata or {}
        self.classes: list[str] = [str(c) for c in pipeline.classes_]

    @property
    def version(self) -> str:
        return str(self.metadata.get("model_version", "unregistered"))

    def predict_proba_frame(self, frame: pd.DataFrame) -> np.ndarray:
        proba: np.ndarray = self.pipeline.predict_proba(frame)
        return proba

    def predict_proba_transactions(self, txns: Sequence[CanonicalTransaction]) -> np.ndarray:
        if not txns:
            return np.zeros((0, len(self.classes)))
        return self.predict_proba_frame(build_frame_from_transactions(txns))


def top_k(proba_row: np.ndarray, classes: Sequence[str], k: int = 3) -> list[tuple[str, float]]:
    """Top-k (class, probability), highest first; ties broken by class name."""
    order = sorted(range(len(classes)), key=lambda i: (-float(proba_row[i]), classes[i]))
    return [(classes[i], float(proba_row[i])) for i in order[:k]]


@lru_cache(maxsize=4)
def _load_cached(version: str, root: str) -> CategoriserModel:
    from ml import registry
    loaded = registry.load(MODEL_NAME, version, root=Path(root))
    return CategoriserModel(loaded.model, loaded.metadata)


def load_model(version: str, root: Path | None = None) -> CategoriserModel:
    from ml import registry
    return _load_cached(version, str(root or registry.MODELS_DIR))


def resolve_model(version: str | None = None, root: Path | None = None) -> CategoriserModel | None:
    """The configured production model, or None (→ v2 fallback). Never raises."""
    from config.settings import get_settings
    from config.thresholds import get_thresholds
    from ml import registry

    settings = get_settings()
    if not settings.categoriser_ml_enabled:
        return None
    version = version or settings.categoriser_model_version or get_thresholds().categoriser.model_version
    if not version or version == "unset":
        return None
    if root is None:
        md = Path(settings.models_dir)
        root = md if md.is_absolute() else registry.REPO_ROOT / md
    if not registry.exists(MODEL_NAME, version, root):
        log.warning("categoriser model %s not found; using v2 rules-only path", version)
        return None
    try:
        return load_model(version, root)
    except Exception as exc:  # corrupted / sha mismatch / incompatible sklearn
        log.warning("categoriser model %s failed to load (%s); using v2 rules-only path",
                    version, type(exc).__name__)
        return None
