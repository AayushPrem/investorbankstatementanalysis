"""Tests for pipeline/versioning.py (Step 3.5 — versioning, persistence, replay)."""
from __future__ import annotations

from pathlib import Path

import pytest

from pipeline.versioning import (
    AGENT_VERSIONS,
    FieldDiff,
    ResultStore,
    StoredRun,
    VersionTag,
    diff_results,
    document_hash,
    replay,
    schema_version,
    summarise_result,
)
from tools.synthetic_gen import generate_statement


class TestSchemaVersion:
    def test_reads_v1_from_changelog(self) -> None:
        assert schema_version() == "v1"


class TestDocumentHash:
    def test_stable_for_same_bytes(self) -> None:
        assert document_hash(b"hello") == document_hash(b"hello")

    def test_differs_for_different_bytes(self) -> None:
        assert document_hash(b"hello") != document_hash(b"world")


class TestVersionTag:
    def test_current_populates_all_fields(self) -> None:
        tag = VersionTag.current()
        assert tag.run_id
        assert tag.timestamp
        assert tag.schema_version == "v1"
        assert tag.agent_versions == AGENT_VERSIONS
        assert tag.jurisdiction_module_version == "india-1.0.0"

    def test_two_tags_have_different_run_ids(self) -> None:
        assert VersionTag.current().run_id != VersionTag.current().run_id

    def test_version_tuple_stable_for_same_versions(self) -> None:
        t1, t2 = VersionTag.current(), VersionTag.current()
        assert t1.as_version_tuple() == t2.as_version_tuple()

    def test_version_tuple_changes_with_agent_version(self) -> None:
        t1 = VersionTag.current()
        bumped = dict(t1.agent_versions)
        bumped["categoriser"] = "99.0.0"
        t2 = VersionTag(
            run_id=t1.run_id, timestamp=t1.timestamp, schema_version=t1.schema_version,
            agent_versions=bumped, jurisdiction_module_version=t1.jurisdiction_module_version,
        )
        assert t1.as_version_tuple() != t2.as_version_tuple()

    def test_roundtrip_dict(self) -> None:
        tag = VersionTag.current()
        assert VersionTag.from_dict(tag.to_dict()) == tag


class TestResultStore:
    def test_save_then_load_roundtrip(self, tmp_path: Path) -> None:
        store = ResultStore(root=tmp_path / "state")
        stored = store.save("some/input.pdf", b"fake bytes", {"total_revenue": "1000"})
        loaded = store.load(stored.version_tag.run_id)
        assert loaded == stored

    def test_load_unknown_run_id_raises(self, tmp_path: Path) -> None:
        store = ResultStore(root=tmp_path / "state")
        with pytest.raises(FileNotFoundError):
            store.load("does-not-exist")

    def test_list_runs_for_document_filters_by_hash(self, tmp_path: Path) -> None:
        store = ResultStore(root=tmp_path / "state")
        run_a1 = store.save("a.pdf", b"AAA", {"x": 1})
        store.save("a.pdf", b"AAA", {"x": 2})
        store.save("b.pdf", b"BBB", {"x": 3})

        runs = store.list_runs_for_document(run_a1.input_hash)
        assert len(runs) == 2
        assert all(r.input_hash == run_a1.input_hash for r in runs)

    def test_reruns_do_not_overwrite_historical_records(self, tmp_path: Path) -> None:
        store = ResultStore(root=tmp_path / "state")
        first = store.save("a.pdf", b"AAA", {"categoriser_version": "1.0.0"})
        second = store.save("a.pdf", b"AAA", {"categoriser_version": "2.0.0"})
        assert first.version_tag.run_id != second.version_tag.run_id
        assert store.load(first.version_tag.run_id).result_summary["categoriser_version"] == "1.0.0"
        assert store.load(second.version_tag.run_id).result_summary["categoriser_version"] == "2.0.0"


class TestDiffResults:
    def test_no_changes_when_summaries_match(self) -> None:
        tag1, tag2 = VersionTag.current(), VersionTag.current()
        old = StoredRun(version_tag=tag1, input_path="a.pdf", input_hash="h", result_summary={"x": 1})
        new = StoredRun(version_tag=tag2, input_path="a.pdf", input_hash="h", result_summary={"x": 1})
        result = diff_results(old, new)
        assert result.has_changes is False
        assert result.changed == []

    def test_detects_changed_field(self) -> None:
        tag1, tag2 = VersionTag.current(), VersionTag.current()
        old = StoredRun(version_tag=tag1, input_path="a.pdf", input_hash="h",
                         result_summary={"risk_score": 10.0})
        new = StoredRun(version_tag=tag2, input_path="a.pdf", input_hash="h",
                         result_summary={"risk_score": 25.0})
        result = diff_results(old, new)
        assert result.has_changes is True
        assert result.changed == [FieldDiff(field="risk_score", old_value=10.0, new_value=25.0)]

    def test_detects_field_present_only_in_new(self) -> None:
        tag1, tag2 = VersionTag.current(), VersionTag.current()
        old = StoredRun(version_tag=tag1, input_path="a.pdf", input_hash="h", result_summary={})
        new = StoredRun(version_tag=tag2, input_path="a.pdf", input_hash="h",
                         result_summary={"new_metric": 5})
        result = diff_results(old, new)
        assert result.changed == [FieldDiff(field="new_metric", old_value=None, new_value=5)]


class TestReplay:
    @pytest.fixture(scope="class")
    def statement_pdf(self, tmp_path_factory: pytest.TempPathFactory) -> Path:
        out_dir = tmp_path_factory.mktemp("versioning_replay")
        pdf_path, _ = generate_statement(
            bank="hdfc", profile="healthy_saas", output_dir=out_dir,
            statement_id="replay_test", flags=[], seed=99,
        )
        return pdf_path

    def test_replay_reproduces_summary_when_no_code_has_changed(
        self, tmp_path: Path, statement_pdf: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # deterministic rule-only run
        store = ResultStore(root=tmp_path / "state")

        from demo.pipeline_runner import run_pipeline_on_bytes

        pdf_bytes = statement_pdf.read_bytes()
        first_result = run_pipeline_on_bytes(pdf_bytes, statement_pdf.name)
        first = store.save(str(statement_pdf), pdf_bytes, summarise_result(first_result))

        second = replay(store, first.version_tag.run_id)

        assert second.result_summary == first.result_summary
        diff = diff_results(first, second)
        assert diff.has_changes is False

    def test_replay_missing_input_file_raises(self, tmp_path: Path) -> None:
        store = ResultStore(root=tmp_path / "state")
        stored = store.save(
            str(tmp_path / "gone.pdf"), b"fake", {"x": 1},
        )
        with pytest.raises(FileNotFoundError):
            replay(store, stored.version_tag.run_id)
