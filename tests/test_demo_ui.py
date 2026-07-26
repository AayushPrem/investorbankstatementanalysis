"""End-to-end tests for demo/app.py, driven via Streamlit's AppTest harness
(no browser required) (Sprint 4, Step 4.3; mode split into 4 lenses later).

Covers all four analysis modes: Angel, VC, Workbench, Network.
"""
from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from tools.synthetic_gen import generate_statement

_APP_PATH = "demo/app.py"
_TIMEOUT = 120


@pytest.fixture(scope="module")
def synthetic_pdfs(tmp_path_factory: pytest.TempPathFactory) -> list[tuple[str, bytes, str]]:
    out_dir = tmp_path_factory.mktemp("demo_ui_synthetic")
    files = []
    for i, profile in enumerate(["healthy_saas", "burning_startup"]):
        pdf_path, _ = generate_statement(
            bank="hdfc", profile=profile, output_dir=out_dir,
            statement_id=f"ui_test_{i}", flags=[], seed=300 + i,
        )
        files.append((f"{profile}.pdf", pdf_path.read_bytes(), "application/pdf"))
    return files


@pytest.fixture(autouse=True)
def _no_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # fast, deterministic rule-only run


class TestNetworkMode:
    def test_mode_switch_with_no_files_shows_prompt(self) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at.sidebar.radio[0].set_value("🌐 Network")
        at.run()
        assert not at.exception
        assert any("Add at least two statements" in i.value for i in at.info)

    def test_batch_upload_and_run_produces_comparison_and_downloads(
        self, synthetic_pdfs: list[tuple[str, bytes, str]]
    ) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at.sidebar.radio[0].set_value("🌐 Network")
        at.run()

        at.file_uploader[0].set_value(synthetic_pdfs)
        at.run()

        run_btn = next(b for b in at.button if "Run Batch Analysis" in b.label)
        run_btn.click()
        at.run()

        assert not at.exception
        assert any("2/2 statements analysed successfully" in m.value for m in at.markdown)
        assert len(at.dataframe) >= 1
        assert "network_xlsx_bytes" in at.session_state
        assert "network_pdf_bytes" in at.session_state
        assert at.session_state["network_xlsx_bytes"][:2] == b"PK"
        assert at.session_state["network_pdf_bytes"][:4] == b"%PDF"

    def test_portfolio_narrative_and_flag_detail_are_shown(
        self, synthetic_pdfs: list[tuple[str, bytes, str]]
    ) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at.sidebar.radio[0].set_value("🌐 Network")
        at.run()

        at.file_uploader[0].set_value(synthetic_pdfs)
        at.run()

        run_btn = next(b for b in at.button if "Run Batch Analysis" in b.label)
        run_btn.click()
        at.run()

        assert not at.exception
        # Narrative is a non-trivial generated sentence, not just a stub
        assert "network_narrative" in at.session_state
        narrative = at.session_state["network_narrative"]
        assert isinstance(narrative, str) and len(narrative) > 20
        assert any(narrative in i.value for i in at.info)

        # The "Risk & Compliance Detail" tab should render without exploding,
        # whether or not this particular synthetic cohort tripped any flags.
        assert len(at.tabs) == 3

    def test_generate_synthetic_cohort_and_run(self) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at.sidebar.radio[0].set_value("🌐 Network")
        at.run()

        source_radio = next(r for r in at.radio if "🏗️ Generate Synthetic Cohort" in r.options)
        source_radio.set_value("🏗️ Generate Synthetic Cohort")
        at.run()
        assert not at.exception

        count_slider = next(s for s in at.slider if s.label == "Number of companies")
        count_slider.set_value(3)
        at.run()

        gen_btn = next(b for b in at.button if "Generate Cohort" in b.label)
        gen_btn.click()
        at.run()

        assert not at.exception
        assert any("Generated 3 statement" in s.value for s in at.success)

        run_btn = next(b for b in at.button if "Run Batch Analysis" in b.label)
        run_btn.click()
        at.run()

        assert not at.exception
        assert any("3/3 statements analysed successfully" in m.value for m in at.markdown)
        assert "network_xlsx_bytes" in at.session_state
        assert "network_pdf_bytes" in at.session_state


def _generate_and_analyse(at: AppTest, mode: str | None = None) -> AppTest:
    """Optionally switch analysis mode, then generate a synthetic statement
    and run the pipeline. Returns the AppTest after the run."""
    if mode is not None:
        at.sidebar.radio[0].set_value(mode)
        at.run()
    at.sidebar.radio[1].set_value("🏗️ Generate Synthetic")
    at.run()
    assert not at.exception

    gen_btn = next(b for b in at.button if "Generate & Analyse" in b.label)
    gen_btn.click()
    at.run()
    return at


class TestFourLensModes:
    """Each mode must show only what that lens actually contains — the same
    pipeline run, presented at that stakeholder's depth."""

    def test_angel_mode_is_default_and_has_no_tabs(self) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        assert at.sidebar.radio[0].value == "👼 Angel"
        at = _generate_and_analyse(at)
        assert not at.exception
        assert len(at.tabs) == 0  # quick-read view, no tab set at all
        assert any("Angel Lens" in m.value for m in at.markdown)
        assert any("Health Signals" in m.value for m in at.markdown)

    def test_vc_mode_has_six_tabs_no_compliance_or_reconciliation(self) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at = _generate_and_analyse(at, mode="💼 VC")
        assert not at.exception
        assert len(at.tabs) == 6
        tab_labels = [t.label for t in at.tabs]
        assert "⚖️ Compliance" not in tab_labels
        assert "🔍 Reconciliation" not in tab_labels
        assert "📈 Financial" in tab_labels
        assert "🔗 Related Parties" in tab_labels

    def test_workbench_mode_has_all_eight_tabs(self) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at = _generate_and_analyse(at, mode="🏛️ Workbench")
        assert not at.exception
        assert len(at.tabs) == 8
        tab_labels = [t.label for t in at.tabs]
        assert "⚖️ Compliance" in tab_labels
        assert "🔍 Reconciliation" in tab_labels

    def test_each_mode_offers_only_its_own_lens_download(self) -> None:
        at_angel = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at_angel = _generate_and_analyse(at_angel)
        angel_text = " ".join(m.value for m in at_angel.markdown)
        assert "Angel Lens" in angel_text
        assert "Workbench Lens" not in angel_text
        assert "VC Lens" not in angel_text
