"""Scanned PDF adapter — OCR extraction for non-digital (image-only) bank
statement PDFs, using Tesseract via pytesseract.

Pipeline per page:
  1. Render the page to an image (pdfplumber -> pypdfium2, already a
     transitive dependency — no extra rendering library needed).
  2. Preprocess: greyscale -> deskew -> denoise -> binarise (Otsu).
  3. OCR with word-level bounding boxes + confidence (`image_to_data`).
  4. Group words into lines (Tesseract's block/par/line numbering already
     gives reading order for a single-column table).
  5. Parse each line with a date + amount regex. Since OCR reading order
     preserves left-to-right column order but a blank debit/credit cell
     drops out entirely, the last two amount tokens on a line are always
     (transaction amount, balance) — whether that amount is a debit or a
     credit is then inferred from the change in running balance, not from
     column position. This is robust to OCR column misalignment.

Unlike the digital PDF adapter, cell-level confidence is available and is
recorded per row (`RawRow.ocr_confidence`, 0-100) so low-confidence rows can
be flagged for manual review downstream.

Requires the Tesseract OCR binary on PATH (not just the `pytesseract` pip
package). Raises AdapterError with install instructions if it's missing.
"""
from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

try:
    import pdfplumber
except ImportError as exc:
    raise ImportError("pdfplumber is required: pip install pdfplumber") from exc

try:
    import pytesseract
except ImportError as exc:
    raise ImportError("pytesseract is required: pip install pytesseract") from exc

from adapters.base import AdapterError, RawRow, RawStatement

_RENDER_DPI = 300
_DESKEW_RANGE_DEG = 5.0
_DESKEW_STEP_DEG = 0.5

_RE_DATE = re.compile(r"(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})")
_RE_AMOUNT = re.compile(r"-?[\d,]+\.\d{2}")
_RE_OPENING = re.compile(r"opening\s+balance.*?([\d,]+\.\d{2})", re.IGNORECASE)
_SKIP_DESCRIPTIONS = frozenset({"opening balance", "closing balance"})


class ScannedPDFAdapter:
    """Extracts raw transaction rows from a scanned (image-only) bank statement PDF."""

    def __init__(self, dpi: int = _RENDER_DPI) -> None:
        self._dpi = dpi

    def extract(self, pdf_path: str | Path) -> RawStatement:
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            raise AdapterError(f"File not found: {pdf_path}")

        try:
            with pdfplumber.open(str(pdf_path)) as pdf:
                images = [p.to_image(resolution=self._dpi).original for p in pdf.pages]
        except Exception as exc:
            raise AdapterError(f"pdfplumber could not read {pdf_path.name}: {exc}") from exc

        try:
            rows = extract_rows_from_pages(images)
        except pytesseract.TesseractNotFoundError as exc:
            raise AdapterError(
                "Tesseract OCR binary not found on PATH. Install it "
                "(https://github.com/tesseract-ocr/tesseract) and ensure "
                "`tesseract` is callable from the shell."
            ) from exc

        return RawStatement(
            source_path=str(pdf_path),
            bank_name="UNKNOWN",
            account_id=None,
            period_start_raw=None,
            period_end_raw=None,
            opening_balance_raw=None,
            rows=rows,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Image preprocessing (pure functions — no OCR dependency, independently testable)
# ─────────────────────────────────────────────────────────────────────────────

def deskew(image: Image.Image) -> Image.Image:
    """Rotate to the angle that maximises horizontal-projection variance —
    text lines are sharpest (highest-variance row sums) when perfectly level.
    """
    grey = image.convert("L")
    best_angle, best_score = 0.0, -1.0
    angle = -_DESKEW_RANGE_DEG
    while angle <= _DESKEW_RANGE_DEG:
        if angle != 0.0:
            rotated = np.asarray(grey.rotate(angle, expand=False, fillcolor=255))
        else:
            rotated = np.asarray(grey)
        score = float(np.var(rotated.sum(axis=1)))
        if score > best_score:
            best_score, best_angle = score, angle
        angle += _DESKEW_STEP_DEG
    if abs(best_angle) < 1e-9:
        return image
    return image.rotate(best_angle, expand=False, fillcolor=255)


def denoise(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.MedianFilter(size=3))


def binarize(image: Image.Image) -> Image.Image:
    """Otsu global threshold — implemented directly on the greyscale
    histogram so no OpenCV dependency is needed for a single global split.
    """
    grey = np.asarray(image.convert("L"))
    threshold = _otsu_threshold(grey)
    bw = np.where(grey > threshold, 255, 0).astype(np.uint8)
    return Image.fromarray(bw)


def _otsu_threshold(grey: np.ndarray) -> int:
    hist, _ = np.histogram(grey, bins=256, range=(0, 256))
    total = grey.size
    sum_total = float(np.dot(np.arange(256), hist))
    sum_b, weight_b, best_var, best_t = 0.0, 0.0, -1.0, 128
    for t in range(256):
        weight_b += hist[t]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += t * hist[t]
        mean_b = sum_b / weight_b
        mean_f = (sum_total - sum_b) / weight_f
        var_between = weight_b * weight_f * (mean_b - mean_f) ** 2
        if var_between > best_var:
            best_var, best_t = var_between, t
    return best_t


def preprocess(image: Image.Image) -> Image.Image:
    """Full preprocessing chain: greyscale -> deskew -> denoise -> binarise."""
    return binarize(denoise(deskew(image.convert("L"))))


# ─────────────────────────────────────────────────────────────────────────────
# OCR -> lines
# ─────────────────────────────────────────────────────────────────────────────

def _ocr_lines(image: Image.Image) -> list[tuple[str, float]]:
    """Run Tesseract and group words into (line_text, avg_confidence) pairs,
    in top-to-bottom reading order.
    """
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    lines: dict[tuple[int, int, int], list[tuple[int, str, float]]] = {}
    for i, text in enumerate(data["text"]):
        word = text.strip()
        if not word:
            continue
        conf = float(data["conf"][i])
        if conf < 0:  # tesseract uses -1 for non-text regions
            continue
        key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
        lines.setdefault(key, []).append((data["left"][i], word, conf))

    out: list[tuple[str, float]] = []
    for key in sorted(lines):
        words = sorted(lines[key], key=lambda w: w[0])
        text = " ".join(w[1] for w in words)
        avg_conf = sum(w[2] for w in words) / len(words)
        out.append((text, avg_conf))
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Line parsing -> RawRow (pure — testable without invoking Tesseract)
# ─────────────────────────────────────────────────────────────────────────────

def parse_ocr_line(
    line_text: str, confidence: float, row_idx: int, page: int, previous_balance: Decimal | None,
) -> RawRow | None:
    """Parse one OCR'd line into a RawRow, or None if it isn't a transaction row.

    The last two amount-like tokens on the line are always
    (transaction_amount, balance); which side (debit/credit) the transaction
    amount belongs to is inferred from *previous_balance*, not from column
    position, since OCR silently drops empty cells.
    """
    date_match = _RE_DATE.search(line_text)
    if date_match is None:
        return None

    amounts = _RE_AMOUNT.findall(line_text)
    if len(amounts) < 2:
        return None  # not enough amount tokens to be a transaction row

    txn_amount_raw, balance_raw = amounts[-2], amounts[-1]
    try:
        txn_amount = Decimal(txn_amount_raw.replace(",", ""))
        balance = Decimal(balance_raw.replace(",", ""))
    except InvalidOperation:
        return None

    description = line_text[date_match.end():]
    first_amount_pos = description.find(txn_amount_raw)
    if first_amount_pos != -1:
        description = description[:first_amount_pos]
    description = description.strip(" |-")
    if description.lower() in _SKIP_DESCRIPTIONS:
        return None

    debit_raw: str | None
    credit_raw: str | None
    if previous_balance is None:
        # No running-balance context yet (first row) — default to credit,
        # the more common opening transaction; downstream validation will
        # flag a balance break if this guess is wrong.
        debit_raw, credit_raw = None, str(txn_amount)
    elif balance < previous_balance:
        debit_raw, credit_raw = str(txn_amount), None
    else:
        debit_raw, credit_raw = None, str(txn_amount)

    return RawRow(
        page=page,
        row_idx=row_idx,
        raw_text=line_text,
        date_raw=date_match.group(1),
        description_raw=description,
        debit_raw=debit_raw,
        credit_raw=credit_raw,
        balance_raw=str(balance),
        ocr_confidence=confidence,
    )


def _seed_opening_balance(images: list[Image.Image]) -> Decimal | None:
    if not images:
        return None
    try:
        text = pytesseract.image_to_string(images[0])
    except pytesseract.TesseractNotFoundError:
        raise
    m = _RE_OPENING.search(text)
    if not m:
        return None
    try:
        return Decimal(m.group(1).replace(",", ""))
    except InvalidOperation:
        return None


def extract_rows_from_pages(images: list[Image.Image]) -> list[RawRow]:
    """Run the full OCR pipeline over already-rendered page images."""
    running_balance = _seed_opening_balance(images)
    rows: list[RawRow] = []
    for page_num, image in enumerate(images):
        processed = preprocess(image)
        for row_idx, (line_text, conf) in enumerate(_ocr_lines(processed)):
            row = parse_ocr_line(line_text, conf, row_idx, page_num, running_balance)
            if row is None:
                continue
            rows.append(row)
            running_balance = Decimal(row.balance_raw) if row.balance_raw else running_balance
    return rows
