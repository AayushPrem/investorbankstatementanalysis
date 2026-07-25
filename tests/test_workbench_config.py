"""Tests for workbench_config.py — fund-specific custom detector hook (Step 3.4)."""
from __future__ import annotations

import datetime
from decimal import Decimal

import workbench_config
from analysis.risk import Flag, RiskReport, Severity
from schema.canonical import CanonicalTransaction, SourceReference, StatementDocument

_DATE = datetime.date(2024, 4, 1)


def _doc() -> StatementDocument:
    txn = CanonicalTransaction(
        transaction_id="t1", date=_DATE, description="payment",
        credit=Decimal("10000"), balance=Decimal("100000"),
        source_reference=SourceReference(file_path="test.pdf", page=1, row=0, raw_text=""),
    )
    return StatementDocument(
        account_id="acc1", statement_period_start=_DATE, statement_period_end=_DATE,
        source_format="test", transactions=[txn],
    )


def _flag(name: str, severity: Severity) -> Flag:
    return Flag(detector_name=name, severity=severity, triggering_transaction_ids=["t1"],
                description=f"{name} triggered")


class TestDefaultConfigIsPassthrough:
    def test_all_flags_kept_by_default(self) -> None:
        report = RiskReport(
            flags=[_flag("structuring", Severity.HIGH), _flag("spike_drain", Severity.MEDIUM)],
            composite_score=15.0, narrative="test narrative",
        )
        result = workbench_config.apply(_doc(), report)
        assert len(result.flags) == 2
        assert result.composite_score == 15.0
        assert result.narrative == "test narrative"


class TestBuiltinToggles:
    def test_disabled_detector_flags_are_dropped(self, monkeypatch) -> None:
        monkeypatch.setitem(workbench_config.BUILTIN_DETECTOR_TOGGLES, "structuring", False)
        report = RiskReport(
            flags=[_flag("structuring", Severity.HIGH), _flag("spike_drain", Severity.MEDIUM)],
            composite_score=15.0, narrative="",
        )
        result = workbench_config.apply(_doc(), report)
        assert [f.detector_name for f in result.flags] == ["spike_drain"]

    def test_score_recomputed_after_toggle(self, monkeypatch) -> None:
        monkeypatch.setitem(workbench_config.BUILTIN_DETECTOR_TOGGLES, "structuring", False)
        report = RiskReport(
            flags=[_flag("structuring", Severity.HIGH), _flag("spike_drain", Severity.MEDIUM)],
            composite_score=15.0, narrative="",
        )
        result = workbench_config.apply(_doc(), report)
        assert result.composite_score == 5.0  # only the MEDIUM flag remains


class TestCustomDetectors:
    def test_custom_detector_flags_are_appended(self, monkeypatch) -> None:
        def always_fires(doc: StatementDocument) -> list[Flag]:
            return [_flag("custom_rule", Severity.LOW)]

        monkeypatch.setattr(
            workbench_config, "CUSTOM_DETECTORS",
            [workbench_config.CustomDetector(name="custom_rule", fn=always_fires)],
        )
        report = RiskReport(flags=[], composite_score=0.0, narrative="")
        result = workbench_config.apply(_doc(), report)
        assert [f.detector_name for f in result.flags] == ["custom_rule"]
        assert result.composite_score == 2.0

    def test_disabled_custom_detector_does_not_fire(self, monkeypatch) -> None:
        def always_fires(doc: StatementDocument) -> list[Flag]:
            return [_flag("custom_rule", Severity.LOW)]

        monkeypatch.setattr(
            workbench_config, "CUSTOM_DETECTORS",
            [workbench_config.CustomDetector(name="custom_rule", fn=always_fires, enabled=False)],
        )
        report = RiskReport(flags=[], composite_score=0.0, narrative="")
        result = workbench_config.apply(_doc(), report)
        assert result.flags == []

    def test_custom_detector_receives_the_document(self, monkeypatch) -> None:
        received = {}

        def capture(doc: StatementDocument) -> list[Flag]:
            received["doc"] = doc
            return []

        monkeypatch.setattr(
            workbench_config, "CUSTOM_DETECTORS",
            [workbench_config.CustomDetector(name="capture", fn=capture)],
        )
        doc = _doc()
        workbench_config.apply(doc, RiskReport(flags=[], composite_score=0.0, narrative=""))
        assert received["doc"] is doc
