"""Tests for pipeline/batch.py (Sprint 4, Step 4.2)."""
from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import pytest

from pipeline.batch import BatchInput, BatchOrchestrator
from tools.synthetic_gen import generate_statement


class TestBatchInput:
    def test_resolved_company_name_uses_explicit_name(self) -> None:
        item = BatchInput(path="statements/acme.pdf", company_name="Acme Pvt Ltd")
        assert item.resolved_company_name() == "Acme Pvt Ltd"

    def test_resolved_company_name_falls_back_to_stem(self) -> None:
        item = BatchInput(path="statements/acme_hdfc_001.pdf")
        assert item.resolved_company_name() == "acme_hdfc_001"


class TestBatchOrchestratorValidation:
    def test_rejects_zero_concurrency(self) -> None:
        with pytest.raises(ValueError):
            BatchOrchestrator(concurrency=0)


class TestBatchOrchestratorRealPipeline:
    @pytest.fixture(scope="class")
    def statement_paths(self, tmp_path_factory: pytest.TempPathFactory) -> list[Path]:
        out_dir = tmp_path_factory.mktemp("batch_real")
        paths = []
        for i, profile in enumerate(["healthy_saas", "burning_startup", "services_firm"]):
            pdf_path, _ = generate_statement(
                bank="hdfc", profile=profile, output_dir=out_dir,
                statement_id=f"batch_real_{i}", flags=[], seed=100 + i,
            )
            paths.append(pdf_path)
        return paths

    def test_batch_of_real_statements_all_succeed(
        self, statement_paths: list[Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # fast, deterministic rule-only run
        inputs = [BatchInput(path=str(p)) for p in statement_paths]
        result = BatchOrchestrator(concurrency=2).run_batch(inputs)

        assert len(result.results) == 3
        assert result.summary.total == 3
        assert result.summary.succeeded == 3
        assert result.summary.failed == 0
        assert result.failures() == {}
        assert len(result.successes()) == 3

    def test_one_corrupted_input_fails_in_isolation(
        self, tmp_path: Path, statement_paths: list[Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        bad_path = tmp_path / "corrupted.pdf"
        bad_path.write_bytes(b"not a real pdf")

        inputs = [BatchInput(path=str(statement_paths[0])), BatchInput(path=str(bad_path))]
        result = BatchOrchestrator(concurrency=2).run_batch(inputs)

        assert result.summary.total == 2
        assert result.summary.succeeded == 1
        assert result.summary.failed == 1
        assert str(bad_path) in result.summary.failed_paths
        assert isinstance(result.results[str(bad_path)], Exception)
        assert str(statement_paths[0]) not in result.failures()


class TestConcurrencyControl:
    def test_never_exceeds_configured_concurrency(self, tmp_path: Path) -> None:
        paths = []
        for i in range(6):
            p = tmp_path / f"stmt_{i}.pdf"
            p.write_bytes(b"stub")
            paths.append(p)

        lock = threading.Lock()
        state = {"current": 0, "max_seen": 0}

        def _stub_pipeline_fn(pdf_bytes: bytes, filename: str, company_name: str | None = None):
            with lock:
                state["current"] += 1
                state["max_seen"] = max(state["max_seen"], state["current"])
            time.sleep(0.05)
            with lock:
                state["current"] -= 1
            return {"filename": filename, "company_name": company_name}

        inputs = [BatchInput(path=str(p)) for p in paths]
        result = BatchOrchestrator(concurrency=2).run_batch(inputs, pipeline_fn=_stub_pipeline_fn)

        assert state["max_seen"] <= 2
        assert result.summary.succeeded == 6

    def test_progress_callback_fires_once_per_input(self, tmp_path: Path) -> None:
        paths = []
        for i in range(3):
            p = tmp_path / f"stmt_{i}.pdf"
            p.write_bytes(b"stub")
            paths.append(p)

        seen: list[str] = []

        def _stub_pipeline_fn(pdf_bytes: bytes, filename: str, company_name: str | None = None):
            return "ok"

        def _progress(path: str, outcome: object) -> None:
            seen.append(path)

        inputs = [BatchInput(path=str(p)) for p in paths]
        BatchOrchestrator(concurrency=3).run_batch(
            inputs, progress_callback=_progress, pipeline_fn=_stub_pipeline_fn,
        )

        assert sorted(seen) == sorted(str(p) for p in paths)

    def test_company_name_is_threaded_through_to_pipeline_fn(self, tmp_path: Path) -> None:
        p = tmp_path / "stmt.pdf"
        p.write_bytes(b"stub")
        received = {}

        def _stub_pipeline_fn(pdf_bytes: bytes, filename: str, company_name: str | None = None):
            received["company_name"] = company_name
            return "ok"

        inputs = [BatchInput(path=str(p), company_name="Acme Pvt Ltd")]
        BatchOrchestrator(concurrency=1).run_batch(inputs, pipeline_fn=_stub_pipeline_fn)

        assert received["company_name"] == "Acme Pvt Ltd"


class TestAsyncEntrypoint:
    def test_run_batch_async_matches_sync_wrapper(self, tmp_path: Path) -> None:
        p = tmp_path / "stmt.pdf"
        p.write_bytes(b"stub")

        def _stub_pipeline_fn(pdf_bytes: bytes, filename: str, company_name: str | None = None):
            return "ok"

        inputs = [BatchInput(path=str(p))]

        async def _run():
            return await BatchOrchestrator(concurrency=1).run_batch_async(
                inputs, pipeline_fn=_stub_pipeline_fn,
            )

        result = asyncio.run(_run())
        assert result.summary.succeeded == 1
