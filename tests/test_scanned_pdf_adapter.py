"""Tests for the scanned PDF / OCR adapter (Sprint 3, Step 3.6).

Actual Tesseract OCR isn't required to run this suite: the line-parsing logic
and image preprocessing are pure functions tested directly, and the
Tesseract call itself is monkeypatched to return synthetic word data — the
same pattern this repo already uses to test LLM-backed components without a
live API key (see tests/test_categoriser.py).
"""
from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from adapters import AdapterError
from adapters.scanned_pdf import (
    ScannedPDFAdapter,
    binarize,
    denoise,
    deskew,
    extract_rows_from_pages,
    parse_ocr_line,
)

# ─────────────────────────────────────────────────────────────────────────────
# Line parsing (pure — the interesting logic in this adapter)
# ─────────────────────────────────────────────────────────────────────────────

class TestParseOCRLine:
    def test_credit_row_when_balance_increases(self) -> None:
        row = parse_ocr_line(
            "01/04/24 NEFT CR ACME PVT LTD 50,000.00 150,000.00",
            confidence=91.0, row_idx=0, page=0, previous_balance=Decimal("100000.00"),
        )
        assert row is not None
        assert row.date_raw == "01/04/24"
        assert row.credit_raw == "50000.00"
        assert row.debit_raw is None
        assert row.balance_raw == "150000.00"
        assert row.ocr_confidence == 91.0

    def test_debit_row_when_balance_decreases(self) -> None:
        row = parse_ocr_line(
            "03/04/24 UPI VENDOR PAYMENT XYZ 12,000.00 138,000.00",
            confidence=88.0, row_idx=1, page=0, previous_balance=Decimal("150000.00"),
        )
        assert row is not None
        assert row.debit_raw == "12000.00"
        assert row.credit_raw is None

    def test_first_row_with_no_prior_balance_defaults_to_credit(self) -> None:
        row = parse_ocr_line(
            "01/04/24 OPENING TXN 50,000.00 150,000.00",
            confidence=90.0, row_idx=0, page=0, previous_balance=None,
        )
        assert row is not None
        assert row.credit_raw == "50000.00"
        assert row.debit_raw is None

    def test_non_transaction_line_returns_none(self) -> None:
        row = parse_ocr_line(
            "Statement Period: April 2024",
            confidence=85.0, row_idx=0, page=0, previous_balance=None,
        )
        assert row is None

    def test_line_with_only_one_amount_returns_none(self) -> None:
        row = parse_ocr_line(
            "01/04/24 SOME TEXT 50,000.00",
            confidence=85.0, row_idx=0, page=0, previous_balance=Decimal("100000.00"),
        )
        assert row is None

    def test_description_extracted_between_date_and_amount(self) -> None:
        row = parse_ocr_line(
            "01/04/24 NEFT CR ACME PVT LTD 50,000.00 150,000.00",
            confidence=91.0, row_idx=0, page=0, previous_balance=Decimal("100000.00"),
        )
        assert row is not None
        assert "ACME" in row.description_raw

    def test_balance_only_line_skipped(self) -> None:
        row = parse_ocr_line(
            "opening balance 100,000.00",
            confidence=85.0, row_idx=0, page=0, previous_balance=None,
        )
        assert row is None


# ─────────────────────────────────────────────────────────────────────────────
# Preprocessing (pure — no Tesseract needed)
# ─────────────────────────────────────────────────────────────────────────────

class TestPreprocessing:
    def test_binarize_produces_only_two_values(self) -> None:
        rng = np.random.default_rng(42)
        noisy = Image.fromarray(rng.integers(0, 256, size=(50, 50), dtype=np.uint8))
        result = binarize(noisy)
        values = set(np.asarray(result).flatten().tolist())
        assert values <= {0, 255}

    def test_denoise_removes_salt_and_pepper_speckle(self) -> None:
        base = np.full((40, 40), 255, dtype=np.uint8)
        rng = np.random.default_rng(7)
        speckle_idx = rng.choice(40 * 40, size=20, replace=False)
        flat = base.flatten()
        flat[speckle_idx] = 0
        img = Image.fromarray(flat.reshape(40, 40))
        result = np.asarray(denoise(img))
        # A few isolated black speckles surrounded by white should mostly vanish
        assert (result == 0).sum() < (np.asarray(img) == 0).sum()

    def test_deskew_leaves_already_level_image_unchanged_in_size(self) -> None:
        img = Image.new("L", (100, 100), color=255)
        result = deskew(img)
        assert result.size == img.size


# ─────────────────────────────────────────────────────────────────────────────
# Full pipeline with a mocked Tesseract call
# ─────────────────────────────────────────────────────────────────────────────

def _fake_image_to_data(image, output_type=None):  # noqa: ARG001 — matches pytesseract signature
    words = [
        ("01/04/24", 0), ("NEFT", 1), ("CR", 2), ("ACME", 3), ("50,000.00", 4), ("150,000.00", 5),
    ]
    n = len(words)
    return {
        "text": [w[0] for w in words],
        "conf": [90] * n,
        "left": [w[1] * 40 for w in words],
        "block_num": [1] * n,
        "par_num": [1] * n,
        "line_num": [1] * n,
    }


class TestExtractRowsFromPages:
    def test_mocked_ocr_produces_one_row(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import adapters.scanned_pdf as sp

        monkeypatch.setattr(sp.pytesseract, "image_to_data", _fake_image_to_data)
        monkeypatch.setattr(sp.pytesseract, "image_to_string", lambda *a, **k: "")
        monkeypatch.setattr(sp, "preprocess", lambda img: img)

        image = Image.new("L", (200, 50), color=255)
        rows = extract_rows_from_pages([image])
        assert len(rows) == 1
        assert rows[0].credit_raw == "50000.00"
        assert rows[0].ocr_confidence == 90.0


class TestErrors:
    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(AdapterError):
            ScannedPDFAdapter().extract(tmp_path / "does_not_exist.pdf")

    def test_tesseract_not_found_raises_adapter_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import pytesseract as pt

        from tools.synthetic_gen import generate_statement

        pdf_path, _ = generate_statement(
            bank="hdfc", profile="healthy_saas", output_dir=tmp_path,
            statement_id="ocr_missing_binary", flags=[], seed=1,
        )

        def _raise(*a, **k):
            raise pt.TesseractNotFoundError()

        monkeypatch.setattr(pt, "image_to_string", _raise)
        with pytest.raises(AdapterError, match="Tesseract"):
            ScannedPDFAdapter().extract(pdf_path)
