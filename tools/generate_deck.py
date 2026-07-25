"""Generates docs/system_tour.pdf — the 10-12 slide system tour deck
(Sprint 4, Step 4.4).

Landscape A4, one slide per page. Reads validation numbers from
results/sprint1/gold_regression.json when available, falling back to a
placeholder note if the regression suite hasn't been run yet.

CLI:
    python -m tools.generate_deck --output docs/system_tour.pdf
"""
from __future__ import annotations

import argparse
import json
from io import BytesIO
from pathlib import Path
from typing import Any

from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    Image as RLImage,
)

_NAVY = rl_colors.HexColor("#1F3864")
_SLATE = rl_colors.HexColor("#44546A")
_GREEN = rl_colors.HexColor("#166534")
_PAGE_SIZE = landscape(A4)
_RESULTS_PATH = Path("results/sprint1/gold_regression.json")
_DIAGRAM_PATH = Path("docs/architecture_diagram.png")

_STYLES = {
    "title": ParagraphStyle("title", fontName="Helvetica-Bold", fontSize=32, textColor=_NAVY,
                             spaceAfter=10, leading=38),
    "subtitle": ParagraphStyle("subtitle", fontName="Helvetica", fontSize=15,
                                textColor=_SLATE, leading=20),
    "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=24, textColor=_NAVY,
                          spaceAfter=14),
    "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=15, textColor=_SLATE,
                          spaceAfter=6, spaceBefore=10),
    "body": ParagraphStyle("body", fontName="Helvetica", fontSize=13, leading=19, spaceAfter=8),
    "bullet": ParagraphStyle("bullet", fontName="Helvetica", fontSize=13, leading=19,
                              leftIndent=16, spaceAfter=6),
    "small": ParagraphStyle("small", fontName="Helvetica", fontSize=10, textColor=rl_colors.grey),
}


def _load_validation_summary() -> dict[str, Any]:
    if not _RESULTS_PATH.exists():
        return {}
    try:
        data: dict[str, Any] = json.loads(_RESULTS_PATH.read_text(encoding="utf-8"))
        summary: dict[str, Any] = data.get("summary", {})
        return summary
    except (json.JSONDecodeError, OSError):
        return {}


def _slide_header(title: str) -> list[Any]:
    return [
        Paragraph(title, _STYLES["h1"]),
        HRFlowable(width="100%", thickness=1.5, color=_NAVY),
        Spacer(1, 6 * mm),
    ]


def _bullets(items: list[str]) -> list[Any]:
    return [Paragraph(f"•&nbsp;&nbsp;{item}", _STYLES["bullet"]) for item in items]


def _slide_cover() -> list[Any]:
    return [
        Spacer(1, 40 * mm),
        Paragraph("BSAA", _STYLES["title"]),
        Paragraph("Agentic Bank Statement Analysis Platform — Indian Market",
                   _STYLES["subtitle"]),
        Spacer(1, 10 * mm),
        Paragraph("System Tour · Sprint 4", _STYLES["small"]),
    ]


def _slide_problem() -> list[Any]:
    return _slide_header("The Problem") + [
        Paragraph(
            "Manual Indian bank statement review for investor due diligence is slow, "
            "inconsistent, and doesn't scale — and different banks (HDFC, ICICI, SBI, "
            "Axis, Kotak) all use different PDF layouts.",
            _STYLES["body"],
        ),
        Spacer(1, 4 * mm),
        Paragraph("One Engine, Many Lenses", _STYLES["h2"]),
        Paragraph(
            "A single analysis pipeline produces every finding once. Four lightweight "
            "presentation templates (\"lenses\") tailor that same output for angel "
            "investors, angel networks, VCs, and in-house finance teams — adding a new "
            "user type means a new template, not a new engine.",
            _STYLES["body"],
        ),
    ]


def _slide_architecture() -> list[Any]:
    content = _slide_header("Architecture — 5-Layer Pipeline")
    if _DIAGRAM_PATH.exists():
        content.append(RLImage(str(_DIAGRAM_PATH), width=140 * mm, height=140 * mm * 1.4,
                                kind="proportional"))
    else:
        content += _bullets([
            "Layer 1 — Ingestion: format adapters (PDF, CSV, Excel, scanned PDF/OCR, image)",
            "Layer 2 — Validation + Categorisation: balance continuity, rule + LLM categorisation",
            "Layer 3 — Enrichment: related-party tagging, customer identity resolution",
            "Layer 4 — Analysis: risk, customer analytics, compliance, reconciliation",
            "Layer 5 — Reports: Angel, VC, Workbench, Network lenses",
        ])
    return content


def _slide_layer(title: str, points: list[str]) -> list[Any]:
    return _slide_header(title) + _bullets(points)


def _slide_lenses(title: str, lenses: list[tuple[str, str]]) -> list[Any]:
    content = _slide_header(title)
    for name, desc in lenses:
        content.append(Paragraph(name, _STYLES["h2"]))
        content.append(Paragraph(desc, _STYLES["body"]))
    return content


def _slide_validation() -> list[Any]:
    summary = _load_validation_summary()
    content = _slide_header("Validation Summary")

    if summary:
        rows = [
            ["Metric", "Value"],
            ["Gold-set statements", str(summary.get("total_statements", "—"))],
            ["Transaction extraction accuracy", f"{summary.get('extraction_accuracy', 0)*100:.1f}%"],
            ["Categorisation accuracy", f"{summary.get('categorisation_accuracy', 0)*100:.1f}%"],
            ["Sprint 1 targets met", "Yes" if summary.get("passed") else "No"],
        ]
    else:
        rows = [["Metric", "Value"], ["Gold-set regression", "Run `make regression` to populate"]]

    table = Table(rows, colWidths=[110 * mm, 80 * mm])
    table.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 12),
        ("BACKGROUND", (0, 0), (-1, 0), _SLATE),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("GRID", (0, 0), (-1, -1), 0.5, rl_colors.lightgrey),
        ("PADDING", (0, 0), (-1, -1), 8),
    ]))
    content.append(table)
    content.append(Spacer(1, 6 * mm))
    content.append(Paragraph(
        "759 automated tests across the full pipeline (schema, adapters, normaliser, "
        "validator, categoriser, risk, customer analytics, compliance, reconciliation, "
        "batch processing, and all four report lenses).",
        _STYLES["body"],
    ))
    return content


def _slide_whats_next() -> list[Any]:
    return _slide_header("What's Next") + _bullets([
        "Account Aggregator (Sahamati) sandbox adapter — proof that the pattern extends "
        "to live consent-based data, pending sandbox credentials",
        "Production AA integration beyond the sandbox proof-of-concept",
        "Additional jurisdiction modules for markets beyond India",
        "Fund-specific onboarding: custom detectors and lens branding per workbench_config.py",
    ])


def build_deck() -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=_PAGE_SIZE,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=15 * mm, bottomMargin=15 * mm,
    )

    slides = [
        _slide_cover(),
        _slide_problem(),
        _slide_architecture(),
        _slide_layer("Layer 1 — Ingestion", [
            "Format adapters: digital PDF, CSV, Excel, scanned PDF (OCR), image",
            "Adapters extract only — they never decide what a column means",
            "HDFC + ICICI supported today; profile-based, so new banks are new profiles",
        ]),
        _slide_layer("Layer 2 — Validation + Categorisation", [
            "Balance continuity is the primary defence against silent data corruption",
            "Duplicate detection, date-order and gap checks",
            "Rule-based categorisation covers ~98% of cases; Claude Haiku LLM fills the rest",
        ]),
        _slide_layer("Layer 3 — Enrichment", [
            "Related-party tagger: exact match + LLM fuzzy match against affiliate list",
            "Customer identity resolver: exact-match clustering + sentence-embedding merge",
            "Every REVENUE transaction ends up with a stable customer_id",
        ]),
        _slide_layer("Layer 4 — Analysis", [
            "Six independent risk detectors + composite 0-100 score",
            "Customer analytics: NRR, churn, cohort retention, concentration trajectory",
            "Compliance: India jurisdiction module (§269ST, GST, TDS, PMLA, related-party limits)",
            "Reconciliation: bank-statement reality vs. declared pitch-deck claims",
        ]),
        _slide_lenses("Reports — Angel & VC Lens", [
            ("Angel Lens", "1-page PDF: verdict, KPIs, runway gauge, health signals — "
                            "the quick read for angel investors."),
            ("VC Lens", "6-sheet XLSX + 3-page PDF: financial health, risk, customer "
                        "analytics, transactions, related parties, composite risk score."),
        ]),
        _slide_lenses("Reports — Workbench & Network Lens", [
            ("Workbench Lens", "10-sheet XLSX + 4-page PDF: the deepest view, with "
                                "fund-specific custom detectors via workbench_config.py."),
            ("Network Lens", "Cross-company comparison workbook for angel investor "
                              "networks — sortable table, risk distribution, cohort dashboard."),
        ]),
        _slide_validation(),
        _slide_whats_next(),
    ]

    story = []
    for i, slide in enumerate(slides):
        story.extend(slide)
        if i < len(slides) - 1:
            story.append(PageBreak())

    doc.build(story)
    return buf.getvalue()


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate the BSAA system tour deck")
    parser.add_argument("--output", type=Path, default=Path("docs/system_tour.pdf"))
    args = parser.parse_args()

    pdf_bytes = build_deck()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(pdf_bytes)
    print(f"Wrote {args.output} ({len(pdf_bytes)} bytes)")


if __name__ == "__main__":
    main()
