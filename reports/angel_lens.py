"""Angel Lens report — 1-page A4 PDF for angel investors.

Layout (top to bottom):
  Header bar   — title + generation date
  Company row  — account name, bank, period
  Verdict      — INVESTABLE / MONITOR / CAUTION (colour-coded)
  KPI row      — Revenue | Avg Burn | Runway | MoM Growth
  Monthly table— up to 6 months of Revenue / Burn / Net
  Signals      — 4-5 plain-English health observations
  Footer       — disclaimer

Usage:
    from reports.angel_lens import AngelLensReport
    path = AngelLensReport().generate(doc, metrics, Path("out/report.pdf"))
"""
from __future__ import annotations

import logging
from decimal import Decimal
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import (
    HRFlowable,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from analysis.financial_analyst import FinancialMetrics, MonthlyStats
from schema.canonical import StatementDocument

log = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Colours
# ─────────────────────────────────────────────────────────────────────────────
_DARK_BLUE  = colors.HexColor("#1A2B4A")
_ACCENT     = colors.HexColor("#2563EB")
_GREEN      = colors.HexColor("#166534")
_GREEN_BG   = colors.HexColor("#DCFCE7")
_AMBER      = colors.HexColor("#92400E")
_AMBER_BG   = colors.HexColor("#FEF3C7")
_RED        = colors.HexColor("#991B1B")
_RED_BG     = colors.HexColor("#FEE2E2")
_LIGHT_GREY = colors.HexColor("#F3F4F6")
_MID_GREY   = colors.HexColor("#6B7280")
_TABLE_HEAD = colors.HexColor("#E5E7EB")
_WHITE      = colors.white

_ZERO = Decimal("0")

# ─────────────────────────────────────────────────────────────────────────────
# Paragraph styles (all use built-in Helvetica family — no TTF needed)
# ─────────────────────────────────────────────────────────────────────────────

def _style(
    name: str,
    font: str = "Helvetica",
    size: float = 9,
    leading: float = 12,
    color: object = colors.black,
    align: int = TA_LEFT,
    space_before: float = 0,
    space_after: float = 0,
) -> ParagraphStyle:
    return ParagraphStyle(
        name,
        fontName=font,
        fontSize=size,
        leading=leading,
        textColor=color,
        alignment=align,
        spaceBefore=space_before,
        spaceAfter=space_after,
    )


_S_HEADER_TITLE  = _style("HdrTitle",  "Helvetica-Bold", 13, 16, _WHITE, TA_LEFT)
_S_HEADER_DATE   = _style("HdrDate",   "Helvetica",       8, 10, _WHITE, TA_RIGHT)
_S_LABEL         = _style("Label",     "Helvetica",       7, 10, _MID_GREY, TA_LEFT)
_S_BODY          = _style("Body",      "Helvetica",       8, 11, colors.black, TA_LEFT)
_S_COMPANY       = _style("Company",   "Helvetica-Bold",  10, 13, _DARK_BLUE, TA_LEFT)
_S_SUBLINE       = _style("Subline",   "Helvetica",        8, 10, _MID_GREY, TA_LEFT)
_S_VERDICT_LABEL = _style("VrdLbl",   "Helvetica-Bold",  13, 17, colors.black, TA_CENTER)
_S_KPI_VALUE     = _style("KpiVal",   "Helvetica-Bold",  14, 17, _DARK_BLUE, TA_CENTER)
_S_KPI_LABEL     = _style("KpiLbl",   "Helvetica",        7, 10, _MID_GREY, TA_CENTER)
_S_TBL_HEAD      = _style("TblHead",  "Helvetica-Bold",   8, 10, _DARK_BLUE, TA_LEFT)
_S_TBL_CELL      = _style("TblCell",  "Helvetica",         8, 10, colors.black, TA_RIGHT)
_S_TBL_CELL_L    = _style("TblCellL", "Helvetica",         8, 10, colors.black, TA_LEFT)
_S_SIGNAL        = _style("Signal",   "Helvetica",         8, 11, colors.black, TA_LEFT)
_S_SECTION       = _style("Section",  "Helvetica-Bold",    9, 12, _DARK_BLUE, TA_LEFT,
                           space_before=4)
_S_FOOTER        = _style("Footer",   "Helvetica",          7,  9, _MID_GREY, TA_CENTER)


# ─────────────────────────────────────────────────────────────────────────────
# Verdict logic
# ─────────────────────────────────────────────────────────────────────────────

def _verdict(metrics: FinancialMetrics) -> tuple[str, object, object]:
    """Return (label, text_colour, bg_colour) for the verdict banner."""
    runway = metrics.runway_months or _ZERO
    growth = metrics.avg_mom_growth or _ZERO

    if metrics.is_profitable and growth >= _ZERO:
        return "INVESTABLE", _GREEN, _GREEN_BG
    if runway >= Decimal("6") and growth >= _ZERO:
        return "INVESTABLE", _GREEN, _GREEN_BG
    if metrics.is_profitable or runway >= Decimal("3"):
        return "MONITOR", _AMBER, _AMBER_BG
    return "CAUTION", _RED, _RED_BG


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_amount(amount: Decimal) -> str:
    """Format an INR amount using Lakh/Crore shorthands."""
    abs_a = abs(amount)
    sign = "-" if amount < _ZERO else ""
    if abs_a >= Decimal("10000000"):
        return f"{sign}Rs.{abs_a / Decimal('10000000'):.1f}Cr"
    if abs_a >= Decimal("100000"):
        return f"{sign}Rs.{abs_a / Decimal('100000'):.1f}L"
    if abs_a >= Decimal("1000"):
        return f"{sign}Rs.{abs_a / Decimal('1000'):.0f}K"
    return f"{sign}Rs.{abs_a:.0f}"


def _fmt_growth(rate: Decimal | None) -> str:
    if rate is None:
        return "N/A"
    sign = "+" if rate >= _ZERO else ""
    return f"{sign}{float(rate) * 100:.1f}%"


def _fmt_runway(months: Decimal | None) -> str:
    if months is None:
        return "Profitable"
    return f"{float(months):.1f} mo"


def _month_label(ym: str) -> str:
    """'2024-04' → 'Apr 2024'"""
    from datetime import datetime
    return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")


def _period_str(doc: StatementDocument) -> str:
    s = doc.statement_period_start.strftime("%b %Y")
    e = doc.statement_period_end.strftime("%b %Y")
    return s if s == e else f"{s} – {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Health signals
# ─────────────────────────────────────────────────────────────────────────────

def _signals(metrics: FinancialMetrics) -> list[str]:
    lines: list[str] = []
    growth = metrics.avg_mom_growth

    # Revenue trend
    if growth is not None:
        pct = f"{abs(float(growth)) * 100:.1f}%"
        if growth > Decimal("0.05"):
            lines.append(f"OK  Revenue growing at +{pct}/month — strong top-line momentum.")
        elif growth >= _ZERO:
            lines.append(f"OK  Revenue stable (MoM growth: +{pct}).")
        else:
            lines.append(f"!   Revenue declining at -{pct}/month — investigate churn.")
    else:
        lines.append("    Only one month of data; trend unavailable.")

    # Profitability
    if metrics.is_profitable:
        lines.append("OK  Business is cash-flow positive — revenue exceeds operating costs.")
    else:
        excess = _fmt_amount(metrics.avg_monthly_burn - metrics.avg_monthly_revenue)
        lines.append(f"!   Burning {excess}/month more than it earns — pre-revenue or loss-making.")

    # Runway
    runway = metrics.runway_months
    if runway is None:
        lines.append("OK  No runway risk — business is self-sustaining.")
    elif runway >= Decimal("12"):
        lines.append(f"OK  {_fmt_runway(runway)} runway — well-funded.")
    elif runway >= Decimal("6"):
        lines.append(f"    {_fmt_runway(runway)} runway — plan fundraise in 3-4 months.")
    elif runway >= Decimal("3"):
        lines.append(f"!   {_fmt_runway(runway)} runway — fundraise urgently required.")
    else:
        lines.append(f"!!  {_fmt_runway(runway)} runway — critically low. Immediate action needed.")

    # Customer concentration
    if metrics.top_customer_revenue_pct is not None:
        pct = float(metrics.top_customer_revenue_pct) * 100
        if pct >= 50:
            lines.append(f"!   Top customer accounts for {pct:.0f}% of revenue — high concentration risk.")
        else:
            lines.append(f"OK  Revenue diversified (top customer: {pct:.0f}%).")
    else:
        lines.append("    Customer segmentation data not available.")

    return lines[:5]  # cap at 5 to keep layout tight


# ─────────────────────────────────────────────────────────────────────────────
# Report builder
# ─────────────────────────────────────────────────────────────────────────────

class AngelLensReport:
    """Generates a 1-page A4 Angel investor PDF report."""

    MARGIN = 18 * mm
    COL_WIDTH = A4[0] - 2 * MARGIN  # usable width

    def generate(
        self,
        doc: StatementDocument,
        metrics: FinancialMetrics,
        output_path: Path | str,
        company_name: str | None = None,
    ) -> Path:
        """Write the PDF and return the output path."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        display_name = company_name or f"Account {doc.account_id}"
        verdict_label, verdict_fg, verdict_bg = _verdict(metrics)

        pdf = SimpleDocTemplate(
            str(out),
            pagesize=A4,
            leftMargin=self.MARGIN,
            rightMargin=self.MARGIN,
            topMargin=self.MARGIN,
            bottomMargin=self.MARGIN,
        )

        story: list = []
        story += self._build_header(display_name, doc)
        story += self._build_verdict(verdict_label, verdict_fg, verdict_bg)
        story += self._build_kpi_row(metrics)
        story += self._build_monthly_table(metrics)
        story += self._build_signals(metrics)
        story += self._build_footer()

        pdf.build(story)
        log.info("Angel lens report written: %s", out)
        return out

    # ── Sections ──────────────────────────────────────────────────────────────

    def _build_header(
        self, display_name: str, doc: StatementDocument
    ) -> list:
        from datetime import date as _date
        today = _date.today().strftime("%d %b %Y")

        # Dark-blue header bar: title left, date right
        data = [[
            Paragraph("BSAA &bull; Angel Lens Report", _S_HEADER_TITLE),
            Paragraph(f"Generated: {today}", _S_HEADER_DATE),
        ]]
        tbl = Table(data, colWidths=[self.COL_WIDTH * 0.65, self.COL_WIDTH * 0.35])
        tbl.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), _DARK_BLUE),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING",   (0, 0), (0, 0),   8),
            ("RIGHTPADDING",  (-1, 0), (-1, -1), 8),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))

        bank_label = (doc.bank_name or "").upper()
        period = _period_str(doc)
        sub = f"{bank_label}  |  Account {doc.account_id}  |  {period}"

        return [
            tbl,
            Spacer(1, 3),
            Paragraph(display_name, _S_COMPANY),
            Paragraph(sub, _S_SUBLINE),
            Spacer(1, 4),
            HRFlowable(width=self.COL_WIDTH, thickness=0.5, color=_TABLE_HEAD),
            Spacer(1, 4),
        ]

    def _build_verdict(
        self, label: str, fg: object, bg: object
    ) -> list:
        data = [[Paragraph(f"&bull; &nbsp; {label} &nbsp; &bull;", _S_VERDICT_LABEL)]]
        tbl = Table(data, colWidths=[self.COL_WIDTH])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), bg),
            ("TEXTCOLOR",     (0, 0), (-1, -1), fg),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
            ("BOX",           (0, 0), (-1, -1), 0.5, fg),
            ("ROUNDEDCORNERS", [3]),
        ]))
        return [tbl, Spacer(1, 6)]

    def _build_kpi_row(self, metrics: FinancialMetrics) -> list:
        cw = self.COL_WIDTH / 4

        kpis = [
            ("Total Revenue", _fmt_amount(metrics.total_revenue)),
            ("Avg Monthly Burn", _fmt_amount(metrics.avg_monthly_burn) + "/mo"),
            ("Runway", _fmt_runway(metrics.runway_months)),
            ("Avg MoM Growth", _fmt_growth(metrics.avg_mom_growth)),
        ]

        labels_row = [Paragraph(k, _S_KPI_LABEL) for k, _ in kpis]
        values_row = [Paragraph(v, _S_KPI_VALUE) for _, v in kpis]

        tbl = Table(
            [values_row, labels_row],
            colWidths=[cw] * 4,
        )
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _LIGHT_GREY),
            ("TOPPADDING",    (0, 0), (-1, -1), 8),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ("LINEAFTER",     (0, 0), (2, -1),  0.5, _TABLE_HEAD),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return [tbl, Spacer(1, 6)]

    def _build_monthly_table(self, metrics: FinancialMetrics) -> list:
        months = metrics.monthly_stats[-6:]  # last 6 months max
        if not months:
            return []

        cw = self.COL_WIDTH
        col_w = [cw * 0.28, cw * 0.24, cw * 0.24, cw * 0.24]

        header = [
            Paragraph("Month",   _S_TBL_HEAD),
            Paragraph("Revenue", _S_TBL_HEAD),
            Paragraph("Burn",    _S_TBL_HEAD),
            Paragraph("Net",     _S_TBL_HEAD),
        ]
        rows = [header]
        for m in months:
            net_style = _S_TBL_CELL
            rows.append([
                Paragraph(_month_label(m.year_month), _S_TBL_CELL_L),
                Paragraph(_fmt_amount(m.revenue), _S_TBL_CELL),
                Paragraph(_fmt_amount(m.burn),    _S_TBL_CELL),
                Paragraph(_fmt_amount(m.net),     net_style),
            ])

        tbl = Table(rows, colWidths=col_w)
        style = [
            ("BACKGROUND",    (0, 0), (-1, 0),  _TABLE_HEAD),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (0, -1),  4),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.25, _TABLE_HEAD),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LIGHT_GREY]),
        ]
        tbl.setStyle(TableStyle(style))

        return [
            Paragraph("Monthly Breakdown", _S_SECTION),
            Spacer(1, 3),
            tbl,
            Spacer(1, 6),
        ]

    def _build_signals(self, metrics: FinancialMetrics) -> list:
        lines = _signals(metrics)
        elems: list = [Paragraph("Health Signals", _S_SECTION), Spacer(1, 3)]
        for line in lines:
            # Replace prefix tokens with styled markers
            if line.startswith("OK "):
                text = "<font color='#166534'><b>OK</b></font>  " + line[3:].lstrip()
            elif line.startswith("!! "):
                text = "<font color='#991B1B'><b>!!</b></font>  " + line[3:].lstrip()
            elif line.startswith("!  "):
                text = "<font color='#92400E'><b>!</b></font>   " + line[3:].lstrip()
            else:
                text = "      " + line.lstrip()
            elems.append(Paragraph(text, _S_SIGNAL))
            elems.append(Spacer(1, 2))
        return elems

    def _build_footer(self) -> list:
        text = (
            "This report is generated by BSAA (Bank Statement Analysis Agent) "
            "for informational purposes only. It is not financial advice. "
            "Figures are derived from the bank statement provided and may not "
            "reflect the complete financial position of the entity."
        )
        return [
            Spacer(1, 6),
            HRFlowable(width=self.COL_WIDTH, thickness=0.5, color=_TABLE_HEAD),
            Spacer(1, 3),
            Paragraph(text, _S_FOOTER),
        ]
