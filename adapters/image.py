"""Image adapter — OCR extraction for a single JPG/PNG photo or scan of one
bank statement page.

Thin wrapper around adapters.scanned_pdf's preprocessing + OCR + line-parsing
pipeline: a standalone image is treated as a one-page document.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, UnidentifiedImageError

try:
    import pytesseract
except ImportError as exc:
    raise ImportError("pytesseract is required: pip install pytesseract") from exc

from adapters.base import AdapterError, RawStatement
from adapters.scanned_pdf import extract_rows_from_pages

_SUPPORTED_SUFFIXES = frozenset({".jpg", ".jpeg", ".png"})


class ImageAdapter:
    """Extracts raw transaction rows from a single JPG/PNG bank statement page."""

    def extract(self, image_path: str | Path) -> RawStatement:
        image_path = Path(image_path)
        if not image_path.exists():
            raise AdapterError(f"File not found: {image_path}")
        if image_path.suffix.lower() not in _SUPPORTED_SUFFIXES:
            raise AdapterError(
                f"Unsupported image type {image_path.suffix!r}; "
                f"expected one of {sorted(_SUPPORTED_SUFFIXES)}"
            )

        try:
            image = Image.open(image_path)
            image.load()
        except UnidentifiedImageError as exc:
            raise AdapterError(f"Could not read {image_path.name} as an image: {exc}") from exc

        try:
            rows = extract_rows_from_pages([image])
        except pytesseract.TesseractNotFoundError as exc:
            raise AdapterError(
                "Tesseract OCR binary not found on PATH. Install it "
                "(https://github.com/tesseract-ocr/tesseract) and ensure "
                "`tesseract` is callable from the shell."
            ) from exc

        return RawStatement(
            source_path=str(image_path),
            bank_name="UNKNOWN",
            account_id=None,
            period_start_raw=None,
            period_end_raw=None,
            opening_balance_raw=None,
            rows=rows,
        )
