"""Batch processing — run the full pipeline over many statements concurrently
(Sprint 4, Step 4.3).

Each input runs through the canonical pipeline, pipeline.run.run_analysis. By
default that goes via demo.pipeline_runner.run_pipeline_on_bytes, which also
renders the report lenses the Network UI downloads, and the outcome is a
DemoResult. With BatchOrchestrator(render_reports=False), run_analysis is
called directly and the outcome is a pipeline.run.AnalysisResult.

Each input's pipeline call is CPU-bound (PDF parsing) and
makes blocking LLM HTTP calls, so real concurrency comes from running each
one in a worker thread (asyncio.to_thread) under a semaphore — not from
making the pipeline itself async. A failure on one statement is captured,
not raised, so it never takes down the rest of the batch.

Usage:
    from pipeline.batch import BatchInput, BatchOrchestrator

    inputs = [BatchInput(path="a.pdf"), BatchInput(path="b.pdf", company_name="Acme")]
    result = BatchOrchestrator(concurrency=4).run_batch(inputs)
    for path, outcome in result.results.items():
        ...  # outcome is a DemoResult, or the Exception that was raised
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.logging import redact, safe_exc

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, Any], None]
# Called as fn(pdf_bytes, filename, company_name=...) -> DemoResult.
# A substituted pipeline_fn (e.g. in tests) must accept a company_name kwarg,
# even if it ignores it — see tests/test_batch.py for the stub pattern.
PipelineFn = Callable[..., Any]


@dataclass
class BatchInput:
    path: str
    company_name: str | None = None

    def resolved_company_name(self) -> str:
        return self.company_name or Path(self.path).stem


@dataclass
class BatchSummary:
    total: int
    succeeded: int
    failed: int
    failed_paths: list[str] = field(default_factory=list)


@dataclass
class BatchResult:
    results: dict[str, Any]  # input path -> DemoResult on success, Exception on failure
    summary: BatchSummary

    def successes(self) -> dict[str, Any]:
        return {path: r for path, r in self.results.items() if not isinstance(r, Exception)}

    def failures(self) -> dict[str, Exception]:
        return {path: r for path, r in self.results.items() if isinstance(r, Exception)}


def _analysis_only(data: bytes, filename: str, company_name: str | None = None) -> Any:
    from pipeline.run import AnalysisOptions, run_analysis  # heavy import, deferred

    return run_analysis(data, filename, options=AnalysisOptions(company_name=company_name))


def _default_pipeline_fn(render_reports: bool = True) -> PipelineFn:
    if not render_reports:
        return _analysis_only
    from demo.pipeline_runner import run_pipeline_on_bytes  # heavy import, deferred

    return run_pipeline_on_bytes


class BatchOrchestrator:
    """Runs the full per-company pipeline over many inputs with bounded concurrency."""

    def __init__(self, concurrency: int = 4, render_reports: bool = True) -> None:
        if concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {concurrency}")
        self.concurrency = concurrency
        self.render_reports = render_reports

    async def run_batch_async(
        self,
        inputs: list[BatchInput],
        progress_callback: ProgressCallback | None = None,
        pipeline_fn: PipelineFn | None = None,
    ) -> BatchResult:
        fn = pipeline_fn or _default_pipeline_fn(self.render_reports)
        semaphore = asyncio.Semaphore(self.concurrency)
        results: dict[str, Any] = {}

        async def _run_one(item: BatchInput) -> None:
            async with semaphore:
                try:
                    pdf_bytes = await asyncio.to_thread(Path(item.path).read_bytes)
                    outcome = await asyncio.to_thread(
                        fn, pdf_bytes, Path(item.path).name,
                        company_name=item.resolved_company_name(),
                    )
                except Exception as exc:  # noqa: BLE001 — isolate failures per-input by design
                    log.warning(
                        "batch item failed: file=%s (%s)", redact(item.path), safe_exc(exc)
                    )
                    outcome = exc
                results[item.path] = outcome
                if progress_callback is not None:
                    progress_callback(item.path, outcome)

        await asyncio.gather(*(_run_one(item) for item in inputs))

        failed_paths = [p for p, r in results.items() if isinstance(r, Exception)]
        summary = BatchSummary(
            total=len(inputs),
            succeeded=len(inputs) - len(failed_paths),
            failed=len(failed_paths),
            failed_paths=failed_paths,
        )
        return BatchResult(results=results, summary=summary)

    def run_batch(
        self,
        inputs: list[BatchInput],
        progress_callback: ProgressCallback | None = None,
        pipeline_fn: PipelineFn | None = None,
    ) -> BatchResult:
        return asyncio.run(self.run_batch_async(inputs, progress_callback, pipeline_fn))
