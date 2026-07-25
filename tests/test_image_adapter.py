"""Tests for the single-image (JPG/PNG) adapter (Sprint 3, Step 3.6).

Reuses adapters.scanned_pdf's OCR pipeline — see test_scanned_pdf_adapter.py
for the underlying parsing tests. These tests cover the image-specific
surface: file-type handling and wiring into that shared pipeline.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from adapters import AdapterError, ImageAdapter


def _fake_image_to_data(image, output_type=None):  # noqa: ARG001
    words = [("01/04/24", 0), ("NEFT", 1), ("CR", 2), ("50,000.00", 3), ("150,000.00", 4)]
    n = len(words)
    return {
        "text": [w[0] for w in words],
        "conf": [88] * n,
        "left": [w[1] * 40 for w in words],
        "block_num": [1] * n,
        "par_num": [1] * n,
        "line_num": [1] * n,
    }


class TestImageAdapter:
    def test_extracts_row_from_png(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import adapters.scanned_pdf as sp

        monkeypatch.setattr(sp.pytesseract, "image_to_data", _fake_image_to_data)
        monkeypatch.setattr(sp.pytesseract, "image_to_string", lambda *a, **k: "")
        monkeypatch.setattr(sp, "preprocess", lambda img: img)

        img_path = tmp_path / "page1.png"
        Image.new("L", (300, 80), color=255).save(img_path)

        stmt = ImageAdapter().extract(img_path)
        assert len(stmt.rows) == 1
        assert stmt.rows[0].credit_raw == "50000.00"
        assert stmt.bank_name == "UNKNOWN"

    def test_jpg_extension_accepted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        import adapters.scanned_pdf as sp

        monkeypatch.setattr(sp.pytesseract, "image_to_data", _fake_image_to_data)
        monkeypatch.setattr(sp.pytesseract, "image_to_string", lambda *a, **k: "")
        monkeypatch.setattr(sp, "preprocess", lambda img: img)

        img_path = tmp_path / "page1.jpg"
        Image.new("RGB", (300, 80), color=(255, 255, 255)).save(img_path)

        stmt = ImageAdapter().extract(img_path)
        assert len(stmt.rows) == 1


class TestErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AdapterError):
            ImageAdapter().extract(tmp_path / "does_not_exist.png")

    def test_unsupported_extension_raises(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "statement.pdf"
        bad_path.write_bytes(b"%PDF-1.4 fake")
        with pytest.raises(AdapterError):
            ImageAdapter().extract(bad_path)

    def test_not_an_image_raises(self, tmp_path: Path) -> None:
        bad_path = tmp_path / "fake.png"
        bad_path.write_text("this is not an image", encoding="utf-8")
        with pytest.raises(AdapterError):
            ImageAdapter().extract(bad_path)
