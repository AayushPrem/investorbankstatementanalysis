"""Tests for Wave 1.1 — validation must gate the pipeline.

Before this fix, pipeline/validator.py set StatementDocument.validation_status
but nothing downstream ever checked it: demo/pipeline_runner.py and
pipeline/orchestrator.py both called Validator().validate(doc) and discarded
the result, so a statement with a balance-continuity failure (or any other
ERROR-severity issue) still produced a full, normal report.

These tests force a FAILED validation result (via monkeypatch — fabricating a
real corrupted PDF end-to-end is out of scope of exercising the wiring) and
assert that:
  1. A ValidationFailedError is raised.
  2. No downstream stage (categoriser onward) ever runs.
  3. The error message carries the specific failing issue, not just a generic message.
  4. In a batch, one statement failing validation is isolated and does not
     affect the other statements' results.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from demo import pipeline_runner
from pipeline import orchestrator as orchestrator_module
from pipeline.batch import BatchInput, BatchOrchestrator
from pipeline.validator import (
    ValidationFailedError,
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
)
from schema.canonical import StatementDocument, ValidationStatus

_HDFC_STEM = "hdfc_healthy_saas_5d2a8b"


def _gold_pdf_bytes(stem: str) -> bytes:
    return (Path("data/gold") / f"{stem}.pdf").read_bytes()


def _fail_validation(self: object, doc: StatementDocument) -> ValidationReport:
    """Stand-in for Validator.validate() that always fails, mirroring what the
    real BALANCE_BREAK path does: sets doc.validation_status and returns a
    ValidationReport carrying the specific issue."""
    doc.validation_status = ValidationStatus.FAILED
    issue = ValidationIssue(
        severity=ValidationSeverity.ERROR,
        code="BALANCE_BREAK",
        message="Balance break at 2024-02-10: prev=100000, expected=95000, actual=50000, diff=45000",
        transaction_id="txn_5",
    )
    return ValidationReport(
        status=ValidationStatus.FAILED,
        issues=[issue],
        transaction_count=len(doc.transactions),
        balance_break_count=1,
    )


def _boom(*args: object, **kwargs: object) -> None:
    raise AssertionError("downstream stage must not run when validation fails")


class TestPipelineRunnerGating:
    def test_raises_validation_failed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pipeline_runner.Validator, "validate", _fail_validation)
        with pytest.raises(ValidationFailedError):
            pipeline_runner.run_pipeline_on_bytes(
                _gold_pdf_bytes(_HDFC_STEM), f"{_HDFC_STEM}.pdf"
            )

    def test_categoriser_never_runs_after_failed_validation(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(pipeline_runner.Validator, "validate", _fail_validation)
        monkeypatch.setattr(pipeline_runner.Categoriser, "categorise", _boom)
        with pytest.raises(ValidationFailedError):
            pipeline_runner.run_pipeline_on_bytes(
                _gold_pdf_bytes(_HDFC_STEM), f"{_HDFC_STEM}.pdf"
            )

    def test_error_message_carries_specific_issue(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(pipeline_runner.Validator, "validate", _fail_validation)
        with pytest.raises(ValidationFailedError) as exc_info:
            pipeline_runner.run_pipeline_on_bytes(
                _gold_pdf_bytes(_HDFC_STEM), f"{_HDFC_STEM}.pdf"
            )
        message = str(exc_info.value)
        assert "BALANCE_BREAK" in message
        assert "txn_5" in message

    def test_clean_gold_statement_still_produces_a_report(self) -> None:
        """Sanity check: gating must not break the passing path."""
        result = pipeline_runner.run_pipeline_on_bytes(
            _gold_pdf_bytes(_HDFC_STEM), f"{_HDFC_STEM}.pdf"
        )
        assert result.doc.validation_status == ValidationStatus.PASSED
        assert len(result.report_bytes) > 500


class TestOrchestratorGating:
    def test_raises_validation_failed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(orchestrator_module.Validator, "validate", _fail_validation)
        monkeypatch.setattr(orchestrator_module.Categoriser, "categorise", _boom)
        orch = orchestrator_module.Orchestrator(llm_enabled=False)
        with pytest.raises(ValidationFailedError):
            orch.run(str(Path("data/gold") / f"{_HDFC_STEM}.pdf"))


class TestBatchIsolatesValidationFailure:
    def test_one_failed_validation_does_not_affect_other_items(self, tmp_path: Path) -> None:
        good_path = tmp_path / "good.pdf"
        bad_path = tmp_path / "bad.pdf"
        good_path.write_bytes(b"stub")
        bad_path.write_bytes(b"stub")

        def _stub_pipeline_fn(pdf_bytes: bytes, filename: str, company_name: str | None = None):
            if filename == "bad.pdf":
                raise ValidationFailedError(
                    ValidationReport(
                        status=ValidationStatus.FAILED,
                        issues=[ValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="BALANCE_BREAK", message="fabricated for test",
                        )],
                        transaction_count=10,
                        balance_break_count=1,
                    )
                )
            return "ok"

        inputs = [BatchInput(path=str(good_path)), BatchInput(path=str(bad_path))]
        result = BatchOrchestrator(concurrency=2).run_batch(inputs, pipeline_fn=_stub_pipeline_fn)

        assert result.summary.succeeded == 1
        assert result.summary.failed == 1
        assert isinstance(result.results[str(bad_path)], ValidationFailedError)
        assert result.results[str(good_path)] == "ok"
