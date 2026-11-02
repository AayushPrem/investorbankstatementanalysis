"""Typed loader for config/thresholds.yaml.

Each section maps to a frozen dataclass. Fields are converted to their
annotated type (``Decimal`` from a string, ``int``, ``float``), so modules
that previously held ``_STRUCT_LOWER = Decimal("150000")`` now hold exactly
the same value, loaded from YAML.

Usage::

    from config.thresholds import get_thresholds
    _T = get_thresholds().risk
    _STRUCT_LOWER = _T.structuring_lower

The loaded object is cached per process. Thresholds are read at import time
by the analysis modules, so changing the YAML takes effect on the next
process start, not mid-run.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent / "thresholds.yaml"


class ThresholdConfigError(ValueError):
    """thresholds.yaml is missing a field, has an unknown one, or a bad value."""


@dataclass(frozen=True)
class RiskThresholds:
    structuring_lower: Decimal
    structuring_upper: Decimal
    structuring_window_days: int
    structuring_min_count: int
    round_trip_min_days: int
    round_trip_max_days: int
    round_trip_amount_tol: Decimal
    spike_z: float
    spike_z_high: float
    concentration_high: Decimal
    concentration_medium: Decimal
    round_amount_threshold: float
    round_amount_high_threshold: float
    founder_monthly_limit: Decimal
    founder_salary_multiple: Decimal
    severity_weights: dict[str, float]


@dataclass(frozen=True)
class HealthThresholds:
    runway_critical_months: Decimal
    runway_low_months: Decimal
    runway_healthy_months: Decimal
    burn_extreme_ratio: float
    burn_high_ratio: float
    nrr_severe_pct: float
    nrr_contraction_pct: float
    nrr_par_pct: float
    churn_high_pct: float
    churn_elevated_pct: float
    sustained_decline_months: int
    stagnation_band: Decimal
    burn_acceleration_min_growth: float
    burn_acceleration_margin: float


@dataclass(frozen=True)
class CustomerAnalyticsThresholds:
    churn_silence_months: int
    min_months_for_regularity: int
    regularity_gap_multiple: float
    aggregator_dominance_threshold: float


@dataclass(frozen=True)
class ReconciliationThresholds:
    severity_high_delta_pct: int
    severity_medium_delta_pct: int
    revenue_tolerance_pct: int
    customer_count_tolerance_pct: int
    salary_headcount_tolerance_pct: int
    nrr_percent_input_cutoff: int
    nrr_tolerance: float
    nrr_high_delta: float


@dataclass(frozen=True)
class PortfolioThresholds:
    high_risk_score: float


@dataclass(frozen=True)
class IndiaComplianceThresholds:
    cash_receipt_limit_269st: Decimal
    cash_loan_limit_269ss_t: Decimal
    gst_annual_turnover_gate: Decimal
    gst_monthly_revenue_floor: Decimal
    tds_annual_salary_gate: Decimal
    ctr_cash_window_threshold: Decimal
    rpt_concentration_threshold: float


@dataclass(frozen=True)
class IdentityThresholds:
    embedding_threshold: float


@dataclass(frozen=True)
class RelatedPartyThresholds:
    llm_confidence_threshold: float
    llm_batch_size: int


@dataclass(frozen=True)
class CategoriserThresholds:
    llm_batch_size: int
    # Phase 4 (v3 routing). tau_* are chosen on the company-split VALIDATION
    # precision-coverage curve by ml/categoriser/evaluate.py, for model_version.
    model_version: str
    tau_accept: float
    tau_review: float
    high_precision_rules: tuple[int, ...]   # indices into analysis.categoriser._RULES (rule audit)


@dataclass(frozen=True)
class Thresholds:
    risk: RiskThresholds
    health: HealthThresholds
    customer_analytics: CustomerAnalyticsThresholds
    reconciliation: ReconciliationThresholds
    portfolio: PortfolioThresholds
    india_compliance: IndiaComplianceThresholds
    identity: IdentityThresholds
    related_party: RelatedPartyThresholds
    categoriser: CategoriserThresholds


_T = TypeVar("_T")


def _coerce(name: str, value: Any, typ: Any) -> Any:
    if typ is Decimal:
        if isinstance(value, float):
            # A float literal would silently lose precision (0.1 → 0.1000000000000000055…).
            raise ThresholdConfigError(f"{name}: Decimal fields must be quoted strings, got {value!r}")
        return Decimal(str(value))
    if typ is int:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ThresholdConfigError(f"{name}: expected int, got {value!r}")
        return value
    if typ is float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ThresholdConfigError(f"{name}: expected number, got {value!r}")
        return float(value)
    if typ is str:
        if not isinstance(value, str):
            raise ThresholdConfigError(f"{name}: expected string, got {value!r}")
        return value
    if typ == tuple[int, ...]:
        if not isinstance(value, list) or any(isinstance(v, bool) or not isinstance(v, int) for v in value):
            raise ThresholdConfigError(f"{name}: expected a list of ints, got {value!r}")
        return tuple(value)
    if typ == dict[str, float]:
        if not isinstance(value, dict):
            raise ThresholdConfigError(f"{name}: expected mapping, got {value!r}")
        return {str(k): _coerce(f"{name}.{k}", v, float) for k, v in value.items()}
    raise ThresholdConfigError(f"{name}: unsupported field type {typ!r}")


def _build(cls: type[_T], section: str, raw: Any) -> _T:
    if not isinstance(raw, dict):
        raise ThresholdConfigError(f"section {section!r} missing or not a mapping")
    hints = get_type_hints(cls)
    fields = {f.name for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    missing = fields - raw.keys()
    unknown = raw.keys() - fields
    if missing or unknown:
        raise ThresholdConfigError(
            f"section {section!r}: missing={sorted(missing)} unknown={sorted(unknown)}"
        )
    kwargs = {f: _coerce(f"{section}.{f}", raw[f], hints[f]) for f in fields}
    return cls(**kwargs)


def load_thresholds(path: Path | str | None = None) -> Thresholds:
    """Parse and validate a thresholds YAML file (default: config/thresholds.yaml)."""
    p = Path(path) if path is not None else DEFAULT_PATH
    with p.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if not isinstance(data, dict):
        raise ThresholdConfigError(f"{p}: top level must be a mapping")
    top_hints = get_type_hints(Thresholds)
    sections = {f.name for f in dataclasses.fields(Thresholds)}
    unknown = data.keys() - sections
    if unknown:
        raise ThresholdConfigError(f"{p}: unknown sections {sorted(unknown)}")
    return Thresholds(**{s: _build(top_hints[s], s, data.get(s)) for s in sections})


@lru_cache(maxsize=1)
def get_thresholds() -> Thresholds:
    """Process-wide thresholds from the default YAML (cached)."""
    return load_thresholds()
