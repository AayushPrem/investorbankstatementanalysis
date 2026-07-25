"""End-to-end tests for demo/app.py, driven via Streamlit's AppTest harness
(no browser required) (Sprint 4, Step 4.3).

Covers the Network (multi-company) mode added in Sprint 4 and confirms
Single Company mode still works unchanged.
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
        at.sidebar.radio[0].set_value("Network (multi-company)")
        at.run()
        assert not at.exception
        assert any("Upload two or more statements" in i.value for i in at.info)

    def test_batch_upload_and_run_produces_comparison_and_downloads(
        self, synthetic_pdfs: list[tuple[str, bytes, str]]
    ) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        at.sidebar.radio[0].set_value("Network (multi-company)")
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


class TestSingleCompanyModeUnchanged:
    def test_generate_and_analyse_still_renders_all_tabs(self) -> None:
        at = AppTest.from_file(_APP_PATH, default_timeout=_TIMEOUT).run()
        # Default mode is already "Single Company"; switch statement source
        at.sidebar.radio[1].set_value("🏗️ Generate Synthetic")
        at.run()
        assert not at.exception

        gen_btn = next(b for b in at.button if "Generate & Analyse" in b.label)
        gen_btn.click()
        at.run()

        assert not at.exception
        assert len(at.tabs) == 8
