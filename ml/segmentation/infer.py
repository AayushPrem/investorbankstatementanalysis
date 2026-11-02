"""Serving side: segment, nearest cohort peers and PCA position for Network Lens companies.

``resolve_model()`` returns ``None`` when segmentation is disabled
(``SEGMENTATION_ML_ENABLED=false``), unset, missing or fails its sha256
check. The Network Lens is then generated exactly as before.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np
import pandas as pd

from ml.features.company_features import FEATURES
from ml.segmentation import MODEL_NAME
from ml.segmentation.model import SegmentationModel

log = logging.getLogger(__name__)


@dataclass
class SegmentAssignment:
    segment: int
    name: str
    stable: bool
    pc1: float
    pc2: float
    peers: list[tuple[int, float]]      # (index into the cohort, scaled distance), nearest first


def assign(model: SegmentationModel, rows: list[dict[str, float]], n_peers: int) -> list[SegmentAssignment]:
    """Segments for a cohort; peers are the nearest other cohort companies in scaled space."""
    frame = pd.DataFrame(rows)[list(FEATURES)]
    x = model.scale(frame)
    seg = model.predict_scaled(x)
    pcs = np.asarray(model.pca.transform(x), dtype=float)
    d = np.sqrt(((x[:, None, :] - x[None, :, :]) ** 2).sum(axis=2))
    out = []
    for i in range(len(rows)):
        order = [j for j in np.argsort(d[i], kind="mergesort") if j != i][:n_peers]
        s = int(seg[i])
        out.append(SegmentAssignment(segment=s, name=model.names.get(s, f"Segment {s}"),
                                     stable=bool(model.stable.get(s, False)),
                                     pc1=float(pcs[i, 0]), pc2=float(pcs[i, 1]),
                                     peers=[(int(j), round(float(d[i, j]), 3)) for j in order]))
    return out


@lru_cache(maxsize=4)
def _load_cached(version: str, root: str) -> SegmentationModel:
    from ml import registry

    model: SegmentationModel = registry.load(MODEL_NAME, version, root=Path(root)).model
    return model


def resolve_model(version: str | None = None, root: Path | None = None) -> tuple[SegmentationModel, str] | None:
    """The configured segmentation model and its version, or None. Never raises."""
    from config.settings import get_settings
    from config.thresholds import get_thresholds
    from ml import registry

    settings = get_settings()
    if not settings.segmentation_ml_enabled:
        return None
    version = version or settings.segmentation_model_version or get_thresholds().segmentation.model_version
    if not version or version == "unset":
        return None
    if root is None:
        md = Path(settings.models_dir)
        root = md if md.is_absolute() else registry.REPO_ROOT / md
    if not registry.exists(MODEL_NAME, version, root):
        log.warning("segmentation model %s not found; no segments", version)
        return None
    try:
        return _load_cached(version, str(root)), version
    except Exception as exc:
        log.warning("segmentation model %s failed to load (%s); no segments", version, type(exc).__name__)
        return None
