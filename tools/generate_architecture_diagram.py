"""Generates docs/architecture_diagram.png — the 5-layer pipeline diagram
(Sprint 4, Step 4.4).

Uses PIL directly (no graphviz / mermaid CLI available in this
environment) to draw a simple box-and-arrow diagram matching the ASCII
version in README.md.

CLI:
    python -m tools.generate_architecture_diagram --output docs/architecture_diagram.png
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

_WIDTH = 1000
_MARGIN = 60
_BOX_HEIGHT = 130
_GAP = 50
_TITLE_HEIGHT = 90

_NAVY = "#1F3864"
_SLATE = "#44546A"
_WHITE = "#FFFFFF"
_BG = "#FFFFFF"
_TEXT_DARK = "#1F2937"

_LAYERS: list[tuple[str, str]] = [
    ("Layer 1 — Ingestion", "Format adapters (PDF, CSV, Excel, scanned PDF/OCR, image) — extract only, never interpret"),
    ("Layer 2 — Validation + Categorisation", "Balance continuity · duplicate detection · rule-based + LLM categorisation"),
    ("Layer 3 — Enrichment", "Related-party tagging · customer identity resolution (exact + embedding clustering)"),
    ("Layer 4 — Analysis", "Risk flags · customer analytics · financial health alerts · compliance · reconciliation"),
    ("Layer 5 — Reports", "Angel · VC · Workbench · Network lenses — pure presentation, no analysis logic"),
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    return ImageFont.load_default(size=size)


def generate_diagram() -> Image.Image:
    height = _TITLE_HEIGHT + len(_LAYERS) * (_BOX_HEIGHT + _GAP) - _GAP + _MARGIN
    img = Image.new("RGB", (_WIDTH, height), _BG)
    draw = ImageDraw.Draw(img)

    title_font = _font(30, bold=True)
    layer_font = _font(20, bold=True)
    desc_font = _font(14)

    title = "BSAA — 5-Layer Pipeline"
    tw = draw.textlength(title, font=title_font)
    draw.text(((_WIDTH - tw) / 2, 20), title, fill=_NAVY, font=title_font)

    box_w = _WIDTH - 2 * _MARGIN
    y = _TITLE_HEIGHT

    for i, (name, desc) in enumerate(_LAYERS):
        box = (_MARGIN, y, _MARGIN + box_w, y + _BOX_HEIGHT)
        fill = _NAVY if i % 2 == 0 else _SLATE
        draw.rounded_rectangle(box, radius=14, fill=fill)

        name_w = draw.textlength(name, font=layer_font)
        draw.text((_MARGIN + (box_w - name_w) / 2, y + 22), name, fill=_WHITE, font=layer_font)

        # Wrap description text manually to fit the box width.
        words = desc.split()
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if draw.textlength(trial, font=desc_font) > box_w - 60:
                lines.append(current)
                current = word
            else:
                current = trial
        if current:
            lines.append(current)

        line_y = y + 60
        for line in lines:
            line_w = draw.textlength(line, font=desc_font)
            draw.text((_MARGIN + (box_w - line_w) / 2, line_y), line, fill=_WHITE, font=desc_font)
            line_y += 20

        if i < len(_LAYERS) - 1:
            arrow_x = _WIDTH / 2
            arrow_top = y + _BOX_HEIGHT + 8
            arrow_bottom = y + _BOX_HEIGHT + _GAP - 8
            draw.line((arrow_x, arrow_top, arrow_x, arrow_bottom), fill=_TEXT_DARK, width=3)
            draw.polygon(
                [
                    (arrow_x - 8, arrow_bottom - 10),
                    (arrow_x + 8, arrow_bottom - 10),
                    (arrow_x, arrow_bottom),
                ],
                fill=_TEXT_DARK,
            )

        y += _BOX_HEIGHT + _GAP

    return img


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the BSAA architecture diagram")
    parser.add_argument("--output", type=Path, default=Path("docs/architecture_diagram.png"))
    args = parser.parse_args()

    img = generate_diagram()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    img.save(args.output)
    print(f"Wrote {args.output} ({img.width}x{img.height})")


if __name__ == "__main__":
    main()
