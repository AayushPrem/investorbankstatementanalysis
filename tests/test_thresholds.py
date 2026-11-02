"""config/thresholds.yaml must reproduce the pre-Phase-1 hardcoded constants.

Every expected value below is the literal that was in the source before the
constants moved to YAML. Types are pinned too: a Decimal that became a float
(or the reverse) would change comparisons and formatting.
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from config.thresholds import ThresholdConfigError, get_thresholds, load_thresholds


def _same(actual: object, expected: object) -> None:
    assert type(actual) is type(expected), (actual, expected)
    assert actual == expected
    if isinstance(expected, Decimal):
        # Same textual form → identical in f-strings / str().
        assert str(actual) == str(expected)


# (module, attribute, pre-Phase-1 literal)
LEGACY_CONSTANTS = [
    ("analysis.risk", "_STRUCT_LOWER", Decimal("150000")),
    ("analysis.risk", "_STRUCT_UPPER", Decimal("200000")),
    ("analysis.risk", "_STRUCT_WINDOW_DAYS", 7),
    ("analysis.risk", "_STRUCT_MIN_COUNT", 4),
    ("analysis.risk", "_RT_MIN_DAYS", 3),
    ("analysis.risk", "_RT_MAX_DAYS", 14),
    ("analysis.risk", "_RT_AMOUNT_TOL", Decimal("0.05")),
    ("analysis.risk", "_SPIKE_Z", 2.5),
    ("analysis.risk", "_SPIKE_Z_HIGH", 3.5),
    ("analysis.risk", "_CONC_HIGH", Decimal("0.80")),
    ("analysis.risk", "_CONC_MEDIUM", Decimal("0.60")),
    ("analysis.risk", "_ROUND_THRESHOLD", 0.30),
    ("analysis.risk", "_ROUND_HIGH_THRESHOLD", 0.60),
    ("analysis.risk", "_FOUNDER_MONTHLY_LIMIT", Decimal("500000")),
    ("analysis.risk", "_FOUNDER_SALARY_MULTIPLE", Decimal("3")),
    ("analysis.financial_health_alerts", "RUNWAY_CRITICAL_MONTHS", Decimal("2")),
    ("analysis.financial_health_alerts", "RUNWAY_LOW_MONTHS", Decimal("4")),
    ("analysis.financial_health_alerts", "RUNWAY_HEALTHY_MONTHS", Decimal("6")),
    ("analysis.financial_health_alerts", "BURN_EXTREME_RATIO", 3.0),
    ("analysis.financial_health_alerts", "BURN_HIGH_RATIO", 1.5),
    ("analysis.financial_health_alerts", "NRR_SEVERE_PCT", 0.70),
    ("analysis.financial_health_alerts", "NRR_CONTRACTION_PCT", 0.85),
    ("analysis.financial_health_alerts", "NRR_PAR_PCT", 1.00),
    ("analysis.financial_health_alerts", "CHURN_HIGH_PCT", 0.30),
    ("analysis.financial_health_alerts", "CHURN_ELEVATED_PCT", 0.15),
    ("analysis.financial_health_alerts", "CONCENTRATION_HIGH_PCT", 0.80),
    ("analysis.financial_health_alerts", "CONCENTRATION_MEDIUM_PCT", 0.60),
    ("analysis.customer_analytics", "_CHURN_SILENCE_MONTHS", 3),
    ("analysis.customer_analytics", "_MIN_MONTHS_FOR_REGULARITY", 6),
    ("analysis.customer_analytics", "_REGULARITY_GAP_MULTIPLE", 2.0),
    ("analysis.customer_analytics", "_AGGREGATOR_DOMINANCE_THRESHOLD", 0.30),
    ("analysis.portfolio_narrative", "_HIGH_RISK_THRESHOLD", 50.0),
    ("jurisdictions.india.rules", "_269ST_CASH_LIMIT", Decimal("200000")),
    ("jurisdictions.india.rules", "_269SS_T_CASH_LOAN_LIMIT", Decimal("20000")),
    ("jurisdictions.india.rules", "_GST_ANNUAL_TURNOVER_GATE", Decimal("4000000")),
    ("jurisdictions.india.rules", "_GST_MONTHLY_REVENUE_FLOOR", Decimal("300000")),
    ("jurisdictions.india.rules", "_TDS_ANNUAL_SALARY_GATE", Decimal("500000")),
    ("jurisdictions.india.rules", "_RPT_CONCENTRATION_THRESHOLD", 0.20),
    ("pipeline.customer_identity", "_EMBEDDING_THRESHOLD", 0.85),
    ("pipeline.related_party", "_LLM_CONFIDENCE_THRESHOLD", 0.8),
    ("pipeline.related_party", "_LLM_BATCH_SIZE", 30),
]


@pytest.mark.parametrize(("module", "attr", "expected"), LEGACY_CONSTANTS)
def test_module_constant_unchanged(module: str, attr: str, expected: object) -> None:
    import importlib

    _same(getattr(importlib.import_module(module), attr), expected)


def test_ctr_threshold_equals_ten_lakh() -> None:
    # Was `_LAKH * 10`, i.e. Decimal("100000") * 10.
    from jurisdictions.india import rules

    assert rules._CTR_CASH_WINDOW_THRESHOLD == Decimal("100000") * 10
    assert isinstance(rules._CTR_CASH_WINDOW_THRESHOLD, Decimal)


def test_severity_weights_unchanged() -> None:
    from analysis.risk import _SEVERITY_WEIGHTS, Severity

    assert _SEVERITY_WEIGHTS == {Severity.HIGH: 10.0, Severity.MEDIUM: 5.0, Severity.LOW: 2.0}
    assert all(isinstance(k, Severity) for k in _SEVERITY_WEIGHTS)


def test_categoriser_batch_size_unchanged() -> None:
    from analysis.categoriser import Categoriser

    _same(Categoriser.BATCH_SIZE, 50)


def test_categoriser_v3_routing_thresholds_pinned() -> None:
    """Phase 4: written by `ml.categoriser.evaluate --write-thresholds` (company-split
    validation τ, raised to the policy floor 0.90 / 0.50). Change only with a retrain."""
    c = get_thresholds().categoriser
    _same(c.model_version, "cat3-svc_cal-company-8a28bbe2-191ad9")
    _same(c.tau_accept, 0.90)
    _same(c.tau_review, 0.50)
    _same(c.high_precision_rules, (0, 1, 4, 5, 8, 9, 10))
    assert 0.0 <= c.tau_review <= c.tau_accept <= 1.0


def test_anomaly_thresholds_pinned() -> None:
    """Phase 5: chosen on company-split validation companies by ml/anomaly/train.py and
    written by `ml.anomaly.evaluate --write-thresholds`. Change only with a retrain."""
    a = get_thresholds().anomaly
    _same(a.model_version, "anom1-pooled-company-8a28bbe2-a77238")
    _same(a.contamination, 0.005)
    _same(a.min_history, 150)
    _same(a.severity_high_pct, 0.999)
    _same(a.severity_medium_pct, 0.9986)
    _same(a.max_flags_per_statement, 20)
    assert 1.0 - a.contamination <= a.severity_medium_pct <= a.severity_high_pct <= 1.0


def test_update_section_is_section_scoped(tmp_path: Path) -> None:
    """categoriser and anomaly both have model_version; each writer touches only its section."""
    from config.thresholds import update_section

    p = tmp_path / "t.yaml"
    p.write_text(Path("config/thresholds.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    update_section(p, "anomaly", {"model_version": '"anom-x"', "contamination": "0.02"})
    update_section(p, "categoriser", {"model_version": '"cat-y"'})
    t = load_thresholds(p)
    assert (t.anomaly.model_version, t.anomaly.contamination) == ("anom-x", 0.02)
    assert t.categoriser.model_version == "cat-y"
    assert "# flagged fraction per account" in p.read_text(encoding="utf-8")
    with pytest.raises(ThresholdConfigError):
        update_section(p, "anomaly", {"no_such_key": "1"})


def test_formerly_inline_literals() -> None:
    t = get_thresholds()
    _same(t.health.sustained_decline_months, 3)
    _same(t.health.stagnation_band, Decimal("0.02"))
    _same(t.health.burn_acceleration_min_growth, 0.20)
    _same(t.health.burn_acceleration_margin, 0.15)
    r = t.reconciliation
    _same(r.severity_high_delta_pct, 30)
    _same(r.severity_medium_delta_pct, 15)
    _same(r.revenue_tolerance_pct, 8)
    _same(r.customer_count_tolerance_pct, 12)
    _same(r.salary_headcount_tolerance_pct, 18)
    _same(r.nrr_percent_input_cutoff, 5)
    _same(r.nrr_tolerance, 0.08)
    _same(r.nrr_high_delta, 0.20)


# ── Loader validation ────────────────────────────────────────────────────────

def _write_variant(tmp_path: Path, old: str, new: str) -> Path:
    from config.thresholds import DEFAULT_PATH

    text = DEFAULT_PATH.read_text(encoding="utf-8")
    assert old in text
    p = tmp_path / "t.yaml"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return p


def test_default_file_loads() -> None:
    assert load_thresholds() == get_thresholds()


def test_unquoted_decimal_rejected(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, 'round_trip_amount_tol: "0.05"', "round_trip_amount_tol: 0.05")
    with pytest.raises(ThresholdConfigError, match="quoted"):
        load_thresholds(p)


def test_missing_field_rejected(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, "  spike_z: 2.5\n", "")
    with pytest.raises(ThresholdConfigError, match="spike_z"):
        load_thresholds(p)


def test_unknown_field_rejected(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, "  spike_z: 2.5\n", "  spike_z: 2.5\n  spike_zz: 1.0\n")
    with pytest.raises(ThresholdConfigError, match="spike_zz"):
        load_thresholds(p)


def test_wrong_int_type_rejected(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, "structuring_min_count: 4", "structuring_min_count: four")
    with pytest.raises(ThresholdConfigError, match="structuring_min_count"):
        load_thresholds(p)


def test_override_changes_value(tmp_path: Path) -> None:
    p = _write_variant(tmp_path, "  spike_z: 2.5\n", "  spike_z: 3.0\n")
    assert load_thresholds(p).risk.spike_z == 3.0
