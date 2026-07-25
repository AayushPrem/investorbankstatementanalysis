"""Tests for the docs-generation tools (Sprint 4, Step 4.4)."""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image

from tools.generate_architecture_diagram import generate_diagram
from tools.generate_deck import build_deck


class TestGenerateDeck:
    def test_returns_valid_pdf_bytes(self) -> None:
        pdf_bytes = build_deck()
        assert pdf_bytes.startswith(b"%PDF")

    def test_page_count_within_guide_range(self) -> None:
        pdf_bytes = build_deck()
        # Count page objects directly rather than depending on a PDF parser dependency.
        page_count = len(re.findall(rb"/Type\s*/Page[^s]", pdf_bytes))
        assert 10 <= page_count <= 12

    def test_non_trivial_size(self) -> None:
        pdf_bytes = build_deck()
        assert len(pdf_bytes) > 5_000

    def test_cli_writes_file(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        out_path = tmp_path / "deck.pdf"
        result = subprocess.run(
            [sys.executable, "-m", "tools.generate_deck", "--output", str(out_path)],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
        )
        assert result.returncode == 0, result.stderr
        assert out_path.exists()
        assert out_path.read_bytes().startswith(b"%PDF")


class TestGenerateArchitectureDiagram:
    def test_returns_pil_image(self) -> None:
        img = generate_diagram()
        assert isinstance(img, Image.Image)

    def test_expected_width(self) -> None:
        img = generate_diagram()
        assert img.width == 1000

    def test_height_scales_with_layer_count(self) -> None:
        img = generate_diagram()
        assert img.height > 800  # 5 stacked layer boxes + title + gaps

    def test_cli_writes_png(self, tmp_path: Path) -> None:
        import subprocess
        import sys

        out_path = tmp_path / "diagram.png"
        result = subprocess.run(
            [sys.executable, "-m", "tools.generate_architecture_diagram",
             "--output", str(out_path)],
            capture_output=True, text=True, cwd=Path(__file__).resolve().parent.parent,
        )
        assert result.returncode == 0, result.stderr
        assert out_path.exists()
        img = Image.open(out_path)
        assert img.size[0] > 0 and img.size[1] > 0
