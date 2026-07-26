"""Angel Lens report — 1-page A4 PDF for angel investors.

Layout (top to bottom):
  Header bar      — title + generation date
  Company row     — account name, bank, period
  Verdict         — INVESTABLE / MONITOR / CAUTION with one-line rationale
  Risk mini-row   — composite score, flag count, severity breakdown
  KPI grid        — Revenue · Burn · Runway · MoM Growth
  Monthly table   — up to 6 months of Revenue / Burn / Net
  Health Signals  — 6-8 explanatory observations (financial + customer + risk)
  Footer          — disclaimer

Usage:
    from reports.angel_lens import AngelLensReport
    path = AngelLensReport().generate(
        doc, metrics, Path("out/report.pdf"),
        risk_report=risk_report,
        customer_analytics=ca,
    )
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

from analysis.customer_analytics import aggregator_caveat_text
from analysis.financial_analyst import FinancialMetrics, MonthlyStats
from analysis.financial_health_alerts import classify_churn, classify_concentration, classify_nrr, classify_runway
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
# Paragraph styles
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
        name, fontName=font, fontSize=size, leading=leading,
        textColor=color, alignment=align, spaceBefore=space_before,
        spaceAfter=space_after,
    )


_S_HEADER_TITLE  = _style("HdrTitle",  "Helvetica-Bold", 13, 16, _WHITE,      TA_LEFT)
_S_HEADER_DATE   = _style("HdrDate",   "Helvetica",       8, 10, _WHITE,      TA_RIGHT)
_S_LABEL         = _style("Label",     "Helvetica",       7, 10, _MID_GREY,   TA_LEFT)
_S_BODY          = _style("Body",      "Helvetica",       8, 11, colors.black, TA_LEFT)
_S_COMPANY       = _style("Company",   "Helvetica-Bold", 10, 13, _DARK_BLUE,  TA_LEFT)
_S_SUBLINE       = _style("Subline",   "Helvetica",       8, 10, _MID_GREY,   TA_LEFT)
_S_VERDICT_LABEL = _style("VrdLbl",    "Helvetica-Bold", 13, 17, colors.black, TA_CENTER)
_S_VERDICT_RAT   = _style("VrdRat",    "Helvetica",       8, 11, _MID_GREY,   TA_CENTER)
_S_KPI_VALUE     = _style("KpiVal",    "Helvetica-Bold", 13, 16, _DARK_BLUE,  TA_CENTER)
_S_KPI_LABEL     = _style("KpiLbl",    "Helvetica",       7, 10, _MID_GREY,   TA_CENTER)
_S_TBL_HEAD      = _style("TblHead",   "Helvetica-Bold",  8, 10, _DARK_BLUE,  TA_LEFT)
_S_TBL_CELL      = _style("TblCell",   "Helvetica",       8, 10, colors.black, TA_RIGHT)
_S_TBL_CELL_L    = _style("TblCellL",  "Helvetica",       8, 10, colors.black, TA_LEFT)
_S_SIGNAL        = _style("Signal",    "Helvetica",       7.5, 10.5, colors.black, TA_LEFT)
_S_SECTION       = _style("Section",   "Helvetica-Bold",  9, 12, _DARK_BLUE,  TA_LEFT, space_before=4)
_S_FOOTER        = _style("Footer",    "Helvetica",        7,  9, _MID_GREY,  TA_CENTER)
_S_RISK_LABEL    = _style("RLbl",      "Helvetica",        8, 10, _MID_GREY,  TA_CENTER)
_S_RISK_VALUE    = _style("RVal",      "Helvetica-Bold",  10, 13, _DARK_BLUE, TA_CENTER)


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


def _verdict_rationale(
    metrics: FinancialMetrics,
    risk_report: object | None,
    ca: object | None,
) -> str:
    """One-line rationale explaining the verdict decision."""
    runway = metrics.runway_months
    growth = metrics.avg_mom_growth
    label, _, _ = _verdict(metrics)

    parts = []
    if metrics.is_profitable:
        parts.append("cash-flow positive")
    if runway is not None:
        parts.append(f"{float(runway):.1f}mo runway")
    if growth is not None:
        pct = float(growth) * 100
        parts.append(f"{pct:+.1f}% MoM revenue growth")

    if ca is not None:
        nrr_vals = list(ca.nrr_per_month.values())
        if nrr_vals:
            nrr = nrr_vals[-1]
            parts.append(f"NRR {nrr*100:.0f}%")

    reason = " · ".join(parts) if parts else "based on available financial data"

    if label == "INVESTABLE":
        return f"Meets automated screening criteria for further diligence — {reason}"
    if label == "MONITOR":
        return f"Borderline — {reason}; track for 60–90 days before further diligence"
    return f"High risk — {reason}; significant concerns warrant resolution before further diligence"


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _fmt_amount(amount: Decimal) -> str:
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
    from datetime import datetime
    return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")


def _period_str(doc: StatementDocument) -> str:
    s = doc.statement_period_start.strftime("%b %Y")
    e = doc.statement_period_end.strftime("%b %Y")
    return s if s == e else f"{s} – {e}"


# ─────────────────────────────────────────────────────────────────────────────
# Health signals — explanatory, investor-facing
# ─────────────────────────────────────────────────────────────────────────────

def _signals(
    metrics: FinancialMetrics,
    risk_report: object | None = None,
    ca: object | None = None,
) -> list[tuple[str, str]]:
    """Return list of (prefix, full_text) for health signal lines.

    prefix is one of: 'OK', '!', '!!', ' '
    """
    lines: list[tuple[str, str]] = []
    growth = metrics.avg_mom_growth

    # 1. Revenue trend
    if growth is not None:
        pct = float(growth) * 100
        if growth > Decimal("0.05"):
            lines.append(("OK",
                f"Revenue growing <b>{pct:+.1f}%/mo</b> — well above the 5% threshold that may "
                "indicate strong traction. If sustained, ARR would double in under 12 months — "
                "verify this growth rate holds across a longer window before relying on it."))
        elif growth >= _ZERO:
            lines.append(("!",
                f"Revenue <b>flat ({pct:+.1f}%/mo)</b> — growth has stalled. "
                "Investigate whether this reflects a seasonal pause or structural retention issue."))
        else:
            lines.append(("!!",
                f"Revenue <b>declining {pct:.1f}%/mo</b> — this is the single biggest red flag "
                "for an angel investor. Identify whether existing customers are churning or new "
                "acquisition has dried up."))
    else:
        lines.append((" ",
            "Only one month of data — revenue trend unavailable. "
            "Request 6–12 months of statements for a meaningful picture."))

    # 2. Profitability / burn
    if metrics.is_profitable:
        lines.append(("OK",
            "Business is <b>cash-flow positive</b> — revenue exceeds all operating costs. "
            "This reduces near-term dilution pressure and may strengthen the founder's negotiating position."))
    else:
        excess = _fmt_amount(metrics.avg_monthly_burn - metrics.avg_monthly_revenue)
        lines.append(("!",
            f"Burning <b>{excess}/mo</b> more than it earns. "
            "Pre-revenue or early-stage companies must show a credible path to breakeven — "
            "verify the cost structure is investment-driven, not structural."))

    # 3. Runway — bands shared with analysis/financial_health_alerts.py
    runway = metrics.runway_months
    band = classify_runway(runway)
    if runway is None:
        lines.append(("OK",
            "<b>No runway risk</b> — self-sustaining on operating revenue. "
            "This removes funding dependency, which is uncommon at this stage and worth noting."))
    elif band == "healthy" and runway >= Decimal("12"):
        lines.append(("OK",
            f"<b>{_fmt_runway(runway)} runway</b> — comfortable time to hit next milestones "
            "before a fundraise. With >12 months, the company can negotiate from strength."))
    elif band == "healthy":
        lines.append((" ",
            f"<b>{_fmt_runway(runway)} runway</b> — plan a fundraise within 3–4 months "
            "to avoid negotiating under pressure. Adequate but not comfortable."))
    elif band == "short":
        lines.append((" ",
            f"<b>{_fmt_runway(runway)} runway</b> — fundraising conversations should already be "
            "underway given typical 3–6 month closing timelines. Not yet critical."))
    elif band == "low":
        lines.append(("!",
            f"<b>{_fmt_runway(runway)} runway</b> — urgent fundraise required. "
            "Founders are likely distracted by survival; operational risk is elevated."))
    else:
        lines.append(("!!",
            f"<b>{_fmt_runway(runway)} runway</b> — <b>critically low.</b> "
            "Company may not survive to deploy invested capital. "
            "Milestone-based tranches with an operational bridge are strongly recommended."))

    # 4. Customer concentration (top-3 share) + 5. Customer analytics (NRR, churn)
    # — bands shared with analysis/financial_health_alerts.py; both need `ca`.
    if ca is not None:
        caveat = aggregator_caveat_text(ca)
        if caveat:
            lines.append(("!!", f"<b>{caveat}</b>"))

        if ca.concentration_trajectory:
            top3 = ca.concentration_trajectory[-1].top3_share
            pct = top3 * 100
            conc_band = classify_concentration(top3)
            if conc_band == "high":
                lines.append(("!!",
                    f"<b>Top 3 customers: {pct:.0f}% of revenue</b> — high concentration. "
                    "Losing any one of them would be a material, possibly existential, revenue shock. "
                    "Verify contract duration and exclusivity terms."))
            elif conc_band == "medium":
                lines.append(("!",
                    f"<b>Top 3 customers: {pct:.0f}% of revenue</b> — moderate concentration. "
                    "Worth monitoring; diversification would reduce risk."))
            else:
                lines.append(("OK",
                    f"<b>Top 3 customers: {pct:.0f}% of revenue</b> — healthy diversification. "
                    "No small group of clients can cause existential revenue loss if churned."))

        nrr_vals = list(ca.nrr_per_month.values())
        if nrr_vals:
            nrr = nrr_vals[-1]
            nrr_pct = nrr * 100
            nrr_band = classify_nrr(nrr)
            if nrr_band == "healthy" and nrr >= 1.1:
                lines.append(("OK",
                    f"<b>NRR: {nrr_pct:.0f}%</b> — net revenue retention exceeds 100%, "
                    "meaning existing customers are spending more over time (expansion revenue). "
                    "This is a positive signal for compounding revenue, though worth confirming "
                    "it isn't driven by a small number of large accounts."))
            elif nrr_band == "healthy":
                lines.append(("OK",
                    f"<b>NRR: {nrr_pct:.0f}%</b> — stable existing customer base. "
                    "Churn is balanced by expansion. Improving upsell would move this above 110%."))
            elif nrr_band in ("below_par", "contraction"):
                lines.append(("!",
                    f"<b>NRR: {nrr_pct:.0f}%</b> — existing customers are contracting. "
                    "Every month, the business loses ground even without gaining new customers."))
            else:
                lines.append(("!!",
                    f"<b>NRR: {nrr_pct:.0f}%</b> — severe revenue shrinkage from existing base. "
                    "Investigate whether pricing, product fit, or competitive displacement is driving churn."))

        churn_count = len(ca.churn_events)
        if churn_count > 0:
            peak_active = max(ca.monthly_active_customers.values(), default=1)
            churn_band = classify_churn(churn_count / max(peak_active, 1))
            prefix = "!!" if churn_band == "high" else "!"
            lines.append((prefix,
                f"<b>{churn_count} churn event(s) detected</b> — "
                f"customers silent for 3+ consecutive months. "
                "Request exit interviews and verify whether revenue is shifting to competitors."))

    # 6. Risk flags — only if available
    if risk_report is not None and risk_report.flags:
        high = sum(1 for f in risk_report.flags if str(f.severity) == "HIGH")
        if high > 0:
            lines.append(("!!",
                f"<b>{high} HIGH-severity risk flag(s)</b> — automated analysis detected "
                "serious patterns (structuring, round-tripping, or founder over-extraction). "
                "These require independent verification before committing capital."))

    return lines[:8]


# ─────────────────────────────────────────────────────────────────────────────
# Report builder
# ─────────────────────────────────────────────────────────────────────────────

class AngelLensReport:
    """Generates a 1-page A4 Angel investor PDF report."""

    MARGIN = 18 * mm
    COL_WIDTH = A4[0] - 2 * MARGIN

    def generate(
        self,
        doc: StatementDocument,
        metrics: FinancialMetrics,
        output_path: Path | str,
        company_name: str | None = None,
        risk_report: object | None = None,
        customer_analytics: object | None = None,
    ) -> Path:
        """Write the PDF and return the output path."""
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)

        display_name = company_name or f"Account {doc.account_id}"
        verdict_label, verdict_fg, verdict_bg = _verdict(metrics)
        rationale = _verdict_rationale(metrics, risk_report, customer_analytics)

        pdf = SimpleDocTemplate(
            str(out), pagesize=A4,
            leftMargin=self.MARGIN, rightMargin=self.MARGIN,
            topMargin=self.MARGIN, bottomMargin=self.MARGIN,
        )

        story: list = []
        story += self._build_header(display_name, doc)
        story += self._build_verdict(verdict_label, verdict_fg, verdict_bg, rationale)
        story += self._build_risk_mini(risk_report)
        story += self._build_kpi_row(metrics)
        story += self._build_monthly_table(metrics)
        story += self._build_signals(metrics, risk_report, customer_analytics)
        story += self._build_footer()

        pdf.build(story)
        log.info("Angel lens report written: %s", out)
        return out

    # ── Sections ─────────────────────────────────────────────────────────────

    def _build_header(self, display_name: str, doc: StatementDocument) -> list:
        from datetime import date as _date
        today = _date.today().strftime("%d %b %Y")

        data = [[
            Paragraph("BSAA &bull; Angel Lens Report", _S_HEADER_TITLE),
            Paragraph(f"Generated: {today}", _S_HEADER_DATE),
        ]]
        tbl = Table(data, colWidths=[self.COL_WIDTH * 0.65, self.COL_WIDTH * 0.35])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _DARK_BLUE),
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
        self, label: str, fg: object, bg: object, rationale: str
    ) -> list:
        data = [
            [Paragraph(f"&bull; &nbsp; {label} &nbsp; &bull;", _S_VERDICT_LABEL)],
            [Paragraph(rationale, _S_VERDICT_RAT)],
        ]
        tbl = Table(data, colWidths=[self.COL_WIDTH])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), bg),
            ("TEXTCOLOR",     (0, 0), (0, 0),   fg),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("BOX",           (0, 0), (-1, -1), 0.5, fg),
        ]))
        return [tbl, Spacer(1, 4)]

    def _build_risk_mini(self, risk_report: object | None) -> list:
        if risk_report is None:
            return []

        score = risk_report.composite_score
        flags = risk_report.flags
        high   = sum(1 for f in flags if str(f.severity) == "HIGH")
        medium = sum(1 for f in flags if str(f.severity) == "MEDIUM")
        low    = sum(1 for f in flags if str(f.severity) == "LOW")

        if score >= 50:
            score_color = _RED
            score_bg = _RED_BG
        elif score >= 20:
            score_color = _AMBER
            score_bg = _AMBER_BG
        else:
            score_color = _GREEN
            score_bg = _GREEN_BG

        severity_str = f"{high} HIGH · {medium} MED · {low} LOW"
        flag_str = f"{len(flags)} flag(s)" if flags else "No flags"

        score_style = ParagraphStyle("RS", fontName="Helvetica-Bold", fontSize=11,
                                     textColor=score_color, alignment=TA_CENTER)

        data = [[
            Paragraph(f"{score:.0f}<br/><font size='6'>/ 100</font>", score_style),
            Paragraph(f"{flag_str}<br/><font size='6'>{severity_str}</font>",
                      ParagraphStyle("RF", fontName="Helvetica", fontSize=8,
                                     textColor=score_color, alignment=TA_CENTER, leading=11)),
            Paragraph(
                f"Risk score 0–100 (0=clean, ≥50=serious concerns). "
                f"{'No automated risk patterns detected.' if not flags else 'Review the risk flags section in the VC Lens report for full details.'}",
                ParagraphStyle("RN", fontName="Helvetica", fontSize=7,
                               textColor=_MID_GREY, alignment=TA_LEFT, leading=10)
            ),
        ]]
        tbl = Table(data, colWidths=[
            self.COL_WIDTH * 0.12,
            self.COL_WIDTH * 0.25,
            self.COL_WIDTH * 0.63,
        ])
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (1, 0), score_bg),
            ("TOPPADDING",    (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING",   (0, 0), (-1, -1), 6),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("BOX",           (0, 0), (1, 0), 0.5, score_color),
        ]))
        return [tbl, Spacer(1, 5)]

    def _build_kpi_row(self, metrics: FinancialMetrics) -> list:
        cw = self.COL_WIDTH / 4

        kpis = [
            ("Total Revenue",   _fmt_amount(metrics.total_revenue),
             "All customer inflows over the period"),
            ("Avg Monthly Burn", _fmt_amount(metrics.avg_monthly_burn) + "/mo",
             "Average monthly operating spend"),
            ("Runway",          _fmt_runway(metrics.runway_months),
             "Months of cash left at current burn"),
            ("Avg MoM Growth",  _fmt_growth(metrics.avg_mom_growth),
             ">5% = strong · 0-5% = stable · <0% = declining"),
        ]

        labels_row = [
            Paragraph(f"{k}<br/><font size='6' color='#9CA3AF'>{note}</font>", _S_KPI_LABEL)
            for k, _, note in kpis
        ]
        values_row = [Paragraph(v, _S_KPI_VALUE) for _, v, _ in kpis]

        tbl = Table([values_row, labels_row], colWidths=[cw] * 4)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, -1), _LIGHT_GREY),
            ("TOPPADDING",    (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LINEAFTER",     (0, 0), (2, -1),  0.5, _TABLE_HEAD),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
        ]))
        return [tbl, Spacer(1, 6)]

    def _build_monthly_table(self, metrics: FinancialMetrics) -> list:
        months = metrics.monthly_stats[-6:]
        if not months:
            return []

        cw = self.COL_WIDTH
        col_w = [cw * 0.25, cw * 0.22, cw * 0.22, cw * 0.18, cw * 0.13]

        header = [
            Paragraph("Month",   _S_TBL_HEAD),
            Paragraph("Revenue", _S_TBL_HEAD),
            Paragraph("Burn",    _S_TBL_HEAD),
            Paragraph("Net",     _S_TBL_HEAD),
            Paragraph("MoM Gr.", _S_TBL_HEAD),
        ]
        rows = [header]

        growth_rates = metrics.mom_revenue_growth_rates
        for i, m in enumerate(months):
            g_idx = (i - 1)  # first month has no growth rate
            growth_val = growth_rates[g_idx] if 0 <= g_idx < len(growth_rates) else None
            growth_str = (_fmt_growth(growth_val) if growth_val is not None else "—")
            net_color = "#166534" if m.net >= _ZERO else "#991B1B"

            rows.append([
                Paragraph(_month_label(m.year_month), _S_TBL_CELL_L),
                Paragraph(_fmt_amount(m.revenue), _S_TBL_CELL),
                Paragraph(_fmt_amount(m.burn),    _S_TBL_CELL),
                Paragraph(
                    f"<font color='{net_color}'>{_fmt_amount(m.net)}</font>",
                    _S_TBL_CELL
                ),
                Paragraph(growth_str, _S_TBL_CELL),
            ])

        tbl = Table(rows, colWidths=col_w)
        tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  _TABLE_HEAD),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("TOPPADDING",    (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING",   (0, 0), (0, -1),  4),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.25, _TABLE_HEAD),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [_WHITE, _LIGHT_GREY]),
        ]))
        return [
            Paragraph("Monthly Breakdown", _S_SECTION),
            Spacer(1, 3),
            tbl,
            Spacer(1, 6),
        ]

    def _build_signals(
        self,
        metrics: FinancialMetrics,
        risk_report: object | None = None,
        ca: object | None = None,
    ) -> list:
        signal_list = _signals(metrics, risk_report, ca)
        elems: list = [Paragraph("Investor Health Signals", _S_SECTION), Spacer(1, 3)]

        for prefix, text in signal_list:
            if prefix == "OK":
                marker = "<font color='#166534'><b>&#10003;</b></font>&nbsp;&nbsp;"
            elif prefix == "!!":
                marker = "<font color='#991B1B'><b>!!</b></font>&nbsp;&nbsp;"
            elif prefix == "!":
                marker = "<font color='#92400E'><b>!</b></font>&nbsp;&nbsp;"
            else:
                marker = "&nbsp;&nbsp;&nbsp;&nbsp;"

            elems.append(Paragraph(marker + text, _S_SIGNAL))
            elems.append(Spacer(1, 2))
        return elems

    def _build_footer(self) -> list:
        text = (
            "Generated by BSAA (Bank Statement Analysis Agent). "
            "For informational purposes only — not financial advice. "
            "Figures are derived from the bank statement provided and may not "
            "reflect the complete financial position of the entity."
        )
        return [
            Spacer(1, 4),
            HRFlowable(width=self.COL_WIDTH, thickness=0.5, color=_TABLE_HEAD),
            Spacer(1, 3),
            Paragraph(text, _S_FOOTER),
        ]
