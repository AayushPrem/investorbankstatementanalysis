"""Orchestrator — wires all Sprint 1 pipeline stages into a single callable.

.. deprecated:: Phase 1 (UPGRADE_GUIDE §7)
    Use :func:`pipeline.run.run_analysis`, the single canonical entry point.
    It runs every stage, not only Sprint 1, and auto-selects the adapter.
    This class is kept (with its tests) until Phase 7's pipeline graph
    replaces it. Instantiating it emits a ``DeprecationWarning``.

Stage order: adapter → normaliser → validator → categoriser → financial_analyst
"""
from __future__ import annotations

import logging
import warnings
from dataclasses import dataclass
from pathlib import Path

from adapters.digital_pdf import DigitalPDFAdapter
from analysis.categoriser import CategorisationStats, Categoriser
from analysis.financial_analyst import FinancialAnalyst, FinancialMetrics
from config.logging import redact
from pipeline.normaliser import Normaliser
from pipeline.validator import ValidationFailedError, ValidationReport, Validator
from schema.canonical import StatementDocument, ValidationStatus

log = logging.getLogger(__name__)


@dataclass
class OrchestratorResult:
    doc: StatementDocument
    validation_report: ValidationReport
    metrics: FinancialMetrics
    categorisation_stats: CategorisationStats


class Orchestrator:
    """Runs the full Sprint 1 pipeline on a single bank statement PDF."""

    def __init__(self, llm_enabled: bool = True) -> None:
        warnings.warn(
            "pipeline.orchestrator.Orchestrator is deprecated; use pipeline.run.run_analysis",
            DeprecationWarning,
            stacklevel=2,
        )
        self._adapter = DigitalPDFAdapter()
        self._normaliser = Normaliser()
        self._validator = Validator()
        self._categoriser = Categoriser(llm_enabled=llm_enabled)
        self._analyst = FinancialAnalyst()

    def run(self, pdf_path: str) -> OrchestratorResult:
        path = Path(pdf_path)
        log.info("pipeline start: file=%s", redact(path.name))

        raw = self._adapter.extract(path)
        doc = self._normaliser.normalise(raw)
        validation_report = self._validator.validate(doc)
        if validation_report.status == ValidationStatus.FAILED:
            raise ValidationFailedError(validation_report)
        doc, cat_stats = self._categoriser.categorise(doc)
        metrics = self._analyst.analyse(doc)

        log.info(
            "pipeline done: file=%s | txns=%d validation=%s rule_match=%.0f%%",
            redact(path.name), len(doc.transactions), doc.validation_status,
            cat_stats.rule_match_rate * 100,
        )
        return OrchestratorResult(
            doc=doc,
            validation_report=validation_report,
            metrics=metrics,
            categorisation_stats=cat_stats,
        )
