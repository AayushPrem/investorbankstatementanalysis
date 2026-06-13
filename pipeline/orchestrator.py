"""Orchestrator — wires all Sprint 1 pipeline stages into a single callable.

Stage order: adapter → normaliser → validator → categoriser → financial_analyst
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from adapters.digital_pdf import DigitalPDFAdapter
from analysis.categoriser import CategorisationStats, Categoriser
from analysis.financial_analyst import FinancialAnalyst, FinancialMetrics
from pipeline.normaliser import Normaliser
from pipeline.validator import ValidationReport, Validator
from schema.canonical import StatementDocument

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
        self._adapter = DigitalPDFAdapter()
        self._normaliser = Normaliser()
        self._validator = Validator()
        self._categoriser = Categoriser(llm_enabled=llm_enabled)
        self._analyst = FinancialAnalyst()

    def run(self, pdf_path: str) -> OrchestratorResult:
        path = Path(pdf_path)
        log.info("pipeline start: %s", path.name)

        raw = self._adapter.extract(path)
        doc = self._normaliser.normalise(raw)
        validation_report = self._validator.validate(doc)
        doc, cat_stats = self._categoriser.categorise(doc)
        metrics = self._analyst.analyse(doc)

        log.info(
            "pipeline done: %s | txns=%d validation=%s rule_match=%.0f%% "
            "revenue=%.0f burn=%.0f",
            path.stem, len(doc.transactions), doc.validation_status,
            cat_stats.rule_match_rate * 100, metrics.total_revenue, metrics.total_burn,
        )
        return OrchestratorResult(
            doc=doc,
            validation_report=validation_report,
            metrics=metrics,
            categorisation_stats=cat_stats,
        )
