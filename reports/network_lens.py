"""Network Lens — cross-company comparison workbook for angel investor
networks (Sprint 4, Step 4.1).

Unlike the other three lenses (which drill into one company), this one
takes a *list* of already-analysed companies and produces a sortable
comparison table, the actual risk/compliance detail behind each company's
counts, a portfolio-level narrative, and a cohort-level dashboard — it never
touches individual transactions, only the summary metrics each company's
pipeline run already produced.

Usage:
    from reports.network_lens import CompanySummary, NetworkLensReport, build_company_summary

    summaries = [build_company_summary("Acme", doc, metrics, risk_report,
                                        customer_analytics, compliance_report) for ...]
    xlsx_bytes, pdf_bytes = NetworkLensReport().generate(summaries)
"""
from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from io import BytesIO
from typing import TYPE_CHECKING

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table as ExcelTable
from openpyxl.worksheet.table import TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet
from reportlab.lib import colors as rl_colors
from reportlab.lib.pagesizes import A4
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

if TYPE_CHECKING:
    from analysis.compliance import ComplianceReport
    from analysis.customer_analytics import CustomerAnalyticsReport
    from analysis.financial_analyst import FinancialMetrics
    from analysis.risk import RiskReport
    from schema.canonical import StatementDocument

_ZERO = Decimal("0")


# ─────────────────────────────────────────────────────────────────────────────
# Input container
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class FlagDetail:
    """One risk flag, carried through to the Network Lens detail sheet/UI —
    a lightweight copy of analysis.risk.Flag so this module stays decoupled
    from the analysis layer's types (TYPE_CHECKING-only import above).
    """
    severity: str
    detector_name: str
    description: str


@dataclass
class ComplianceDetail:
    """One compliance exception, carried through to the Network Lens."""
    severity: str
    rule_name: str
    regulatory_citation: str
    description: str


@dataclass
class CompanySummary:
    """One row's worth of cross-company comparison data."""
    company_name: str
    statement_period_start: datetime.date
    statement_period_end: datetime.date
    monthly_burn: Decimal
    monthly_revenue: Decimal
    runway_months: float | None
    revenue_growth: float | None       # avg MoM growth, e.g. 0.05 = +5%/mo
    active_customers: int
    churn_rate: float | None           # churned customers / peak active
    nrr: float | None                  # most recent month's NRR
    top_customer_share: float          # top single customer's revenue %
    composite_risk_score: float        # 0-100
    red_flag_count: int
    compliance_status: str             # "clean" | "warnings" | "violations"
    risk_flags: list[FlagDetail] = field(default_factory=list)
    compliance_exceptions: list[ComplianceDetail] = field(default_factory=list)
    sector: str | None = None
    detail_report_path: str | None = None  # optional link target for Sheet 4


def build_company_summary(
    company_name: str,
    doc: StatementDocument,
    metrics: FinancialMetrics,
    risk_report: RiskReport,
    customer_analytics: CustomerAnalyticsReport,
    compliance_report: ComplianceReport | None = None,
    sector: str | None = None,
    detail_report_path: str | None = None,
) -> CompanySummary:
    """Flatten one company's full pipeline output into a comparison row."""
    latest_month = max(customer_analytics.monthly_active_customers, default=None)
    active = customer_analytics.monthly_active_customers.get(latest_month, 0) if latest_month else 0
    nrr = customer_analytics.nrr_per_month.get(latest_month) if latest_month else None

    peak_active = max(customer_analytics.monthly_active_customers.values(), default=0)
    churn_rate = (
        len(customer_analytics.churn_events) / peak_active if peak_active else None
    )

    top_share = 0.0
    if customer_analytics.concentration_trajectory:
        top_share = customer_analytics.concentration_trajectory[-1].top3_share

    if compliance_report is None:
        compliance_status = "unknown"
    elif compliance_report.high_count > 0:
        compliance_status = "violations"
    elif compliance_report.exceptions:
        compliance_status = "warnings"
    else:
        compliance_status = "clean"

    risk_flags = [
        FlagDetail(severity=str(f.severity), detector_name=f.detector_name, description=f.description)
        for f in risk_report.flags
    ]
    compliance_exceptions = (
        [
            ComplianceDetail(
                severity=str(e.severity), rule_name=e.rule_name,
                regulatory_citation=e.regulatory_citation, description=e.description,
            )
            for e in compliance_report.exceptions
        ]
        if compliance_report is not None else []
    )

    return CompanySummary(
        company_name=company_name,
        statement_period_start=doc.statement_period_start,
        statement_period_end=doc.statement_period_end,
        monthly_burn=metrics.avg_monthly_burn,
        monthly_revenue=metrics.avg_monthly_revenue,
        runway_months=float(metrics.runway_months) if metrics.runway_months is not None else None,
        revenue_growth=float(metrics.avg_mom_growth) if metrics.avg_mom_growth is not None else None,
        active_customers=active,
        churn_rate=churn_rate,
        nrr=nrr,
        top_customer_share=top_share,
        composite_risk_score=risk_report.composite_score,
        red_flag_count=len(risk_report.flags),
        compliance_status=compliance_status,
        risk_flags=risk_flags,
        compliance_exceptions=compliance_exceptions,
        sector=sector,
        detail_report_path=detail_report_path,
    )


def build_portfolio_narrative(summaries: list[CompanySummary], llm_enabled: bool = True) -> str:
    """Cross-company narrative for the cohort — see analysis.portfolio_narrative."""
    from analysis.portfolio_narrative import PortfolioCompany, generate_portfolio_narrative

    companies = [
        PortfolioCompany(
            name=s.company_name,
            risk_score=s.composite_risk_score,
            red_flag_descriptions=[f.description for f in s.risk_flags],
            compliance_status=s.compliance_status,
            compliance_rule_names=[c.rule_name for c in s.compliance_exceptions],
            runway_months=s.runway_months,
            churn_rate=s.churn_rate,
        )
        for s in summaries
    ]
    return generate_portfolio_narrative(companies, llm_enabled=llm_enabled)


# ─────────────────────────────────────────────────────────────────────────────
# Formatting helpers
# ─────────────────────────────────────────────────────────────────────────────

def _inr(v: Decimal | float | None) -> str:
    if v is None:
        return "—"
    f = float(v)
    if abs(f) >= 1e7:
        return f"₹{f/1e7:.2f}Cr"
    if abs(f) >= 1e5:
        return f"₹{f/1e5:.2f}L"
    return f"₹{f:,.0f}"


def _pct(v: float | None) -> str:
    return f"{v*100:.0f}%" if v is not None else "—"


_HEADER_FONT = Font(name="Calibri", bold=True, color="FFFFFF", size=10)
_BODY_FONT = Font(name="Calibri", size=9)
_NAVY = PatternFill("solid", fgColor="1F3864")
_RED = PatternFill("solid", fgColor="FCE4D6")
_AMBER = PatternFill("solid", fgColor="FFEB9C")
_GREEN = PatternFill("solid", fgColor="E2EFDA")
_THIN = Border(
    left=Side(style="thin"), right=Side(style="thin"),
    top=Side(style="thin"), bottom=Side(style="thin"),
)
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT_WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)


def _risk_fill(score: float) -> PatternFill:
    if score >= 50:
        return _RED
    if score >= 20:
        return _AMBER
    return _GREEN


def _compliance_fill(status: str) -> PatternFill:
    return {"violations": _RED, "warnings": _AMBER, "clean": _GREEN}.get(status, PatternFill())


def _severity_fill(severity: str) -> PatternFill:
    s = severity.upper()
    if "HIGH" in s:
        return _RED
    if "MEDIUM" in s:
        return _AMBER
    return _GREEN


def _autowidth(ws: Worksheet, min_w: int = 10, max_w: int = 40) -> None:
    for col in ws.columns:
        length = max((len(str(c.value or "")) for c in col), default=min_w)
        ws.column_dimensions[get_column_letter(col[0].column)].width = min(max(length + 2, min_w), max_w)


# ─────────────────────────────────────────────────────────────────────────────
# Sheet 1 — Comparison table
# ─────────────────────────────────────────────────────────────────────────────

_COMPARISON_COLUMNS = [
    "Company", "Period Start", "Period End", "Monthly Burn", "Monthly Revenue",
    "Runway (mo)", "Revenue Growth", "Active Customers", "Churn Rate", "NRR",
    "Top Customer Share", "Risk Score", "Red Flags", "Compliance",
]


def _sheet_comparison(wb: Workbook, summaries: list[CompanySummary]) -> None:
    ws = wb.active
    ws.title = "Comparison"

    for c, name in enumerate(_COMPARISON_COLUMNS, 1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.font = _HEADER_FONT
        cell.fill = _NAVY
        cell.border = _THIN
        cell.alignment = _CENTER

    for r, s in enumerate(summaries, 2):
        values = [
            s.company_name, str(s.statement_period_start), str(s.statement_period_end),
            _inr(s.monthly_burn), _inr(s.monthly_revenue),
            f"{s.runway_months:.1f}" if s.runway_months is not None else "—",
            _pct(s.revenue_growth), s.active_customers, _pct(s.churn_rate), _pct(s.nrr),
            _pct(s.top_customer_share), f"{s.composite_risk_score:.0f}", s.red_flag_count,
            s.compliance_status,
        ]
        for c, val in enumerate(values, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.font = _BODY_FONT
            cell.border = _THIN
            if c == 12:  # Risk Score
                cell.fill = _risk_fill(s.composite_risk_score)
            elif c == 14:  # Compliance
                cell.fill = _compliance_fill(s.compliance_status)

    last_row = len(summaries) + 1
    last_col = get_column_letter(len(_COMPARISON_COLUMNS))
    if summaries:
        table = ExcelTable(displayName="CompanyComparison", ref=f"A1:{last_col}{last_row}")
        table.tableStyleInfo = TableStyleInfo(
            name="TableStyleMedium2", showRowStripes=True, showFirstColumn=False,
        )
        ws.add_table(table)  # gives autofilter + sortable headers in Excel

    _autowidth(ws)


# ─────────────────────────────────────────────────────────────────────────────
# Sheet 2 — Risk & compliance detail (the actual substance behind the counts)
# ─────────────────────────────────────────────────────────────────────────────

_DETAIL_COLUMNS = ["Company", "Type", "Severity", "Item", "Detail"]


def _sheet_risk_compliance_detail(
    wb: Workbook, summaries: list[CompanySummary], narrative: str,
) -> None:
    ws = wb.create_sheet("Risk & Compliance Detail")

    ws.merge_cells("A1:E1")
    ws["A1"] = "PORTFOLIO NARRATIVE"
    ws["A1"].font = Font(name="Calibri", bold=True, color="FFFFFF", size=11)
    ws["A1"].fill = _NAVY

    ws.merge_cells("A2:E4")
    narrative_cell = ws["A2"]
    narrative_cell.value = narrative
    narrative_cell.alignment = Alignment(wrap_text=True, vertical="top")
    narrative_cell.font = _BODY_FONT

    row = 6
    for c, name in enumerate(_DETAIL_COLUMNS, 1):
        cell = ws.cell(row=row, column=c, value=name)
        cell.font = _HEADER_FONT
        cell.fill = _NAVY
        cell.border = _THIN
        cell.alignment = _CENTER
    row += 1

    has_detail = False
    for s in summaries:
        for f in s.risk_flags:
            has_detail = True
            fill = _severity_fill(f.severity)
            values = [s.company_name, "Risk Flag", f.severity, f.detector_name, f.description]
            for c, val in enumerate(values, 1):
                cell = ws.cell(row=row, column=c, value=val)
                cell.font = _BODY_FONT
                cell.border = _THIN
                cell.alignment = _LEFT_WRAP if c == 5 else None
                if c == 3:
                    cell.fill = fill
            row += 1
        for e in s.compliance_exceptions:
            has_detail = True
            fill = _severity_fill(e.severity)
            values = [
                s.company_name, "Compliance", e.severity,
                f"{e.rule_name} ({e.regulatory_citation})", e.description,
            ]
            for c, val in enumerate(values, 1):
                cell = ws.cell(row=row, column=c, value=val)
                cell.font = _BODY_FONT
                cell.border = _THIN
                cell.alignment = _LEFT_WRAP if c == 5 else None
                if c == 3:
                    cell.fill = fill
            row += 1

    if not has_detail:
        ws.cell(row=row, column=1,
                value="No risk flags or compliance exceptions detected across the cohort.").font = _BODY_FONT

    _autowidth(ws, max_w=60)


# ─────────────────────────────────────────────────────────────────────────────
# Sheet 3 — Risk distribution
# ─────────────────────────────────────────────────────────────────────────────

_RISK_BUCKETS = [(0, 20, "0-19 (Low)"), (20, 50, "20-49 (Medium)"), (50, 101, "50+ (High)")]


def _sheet_risk_distribution(wb: Workbook, summaries: list[CompanySummary]) -> None:
    ws = wb.create_sheet("Risk Distribution")
    ws.cell(row=1, column=1, value="Risk Bucket").font = _HEADER_FONT
    ws.cell(row=1, column=2, value="Company Count").font = _HEADER_FONT
    ws["A1"].fill = _NAVY
    ws["B1"].fill = _NAVY

    for i, (lo, hi, label) in enumerate(_RISK_BUCKETS, 2):
        count = sum(1 for s in summaries if lo <= s.composite_risk_score < hi)
        ws.cell(row=i, column=1, value=label).font = _BODY_FONT
        cell = ws.cell(row=i, column=2, value=count)
        cell.font = _BODY_FONT
        cell.fill = [_GREEN, _AMBER, _RED][i - 2]

    _autowidth(ws)


# ─────────────────────────────────────────────────────────────────────────────
# Sheet 4 — Sector breakdown
# ─────────────────────────────────────────────────────────────────────────────

def _sheet_sector_breakdown(wb: Workbook, summaries: list[CompanySummary]) -> None:
    ws = wb.create_sheet("Sector Breakdown")
    sectors = {s.sector or "Unclassified" for s in summaries}
    if sectors == {"Unclassified"}:
        ws.cell(row=1, column=1, value="No sector metadata provided for this cohort.").font = _BODY_FONT
        _autowidth(ws)
        return

    ws.cell(row=1, column=1, value="Sector").font = _HEADER_FONT
    ws.cell(row=1, column=2, value="Companies").font = _HEADER_FONT
    ws.cell(row=1, column=3, value="Median Risk Score").font = _HEADER_FONT
    for c in ("A1", "B1", "C1"):
        ws[c].fill = _NAVY

    for i, sector in enumerate(sorted(sectors), 2):
        members = [s for s in summaries if (s.sector or "Unclassified") == sector]
        ws.cell(row=i, column=1, value=sector).font = _BODY_FONT
        ws.cell(row=i, column=2, value=len(members)).font = _BODY_FONT
        median_risk = statistics.median(m.composite_risk_score for m in members)
        ws.cell(row=i, column=3, value=f"{median_risk:.0f}").font = _BODY_FONT

    _autowidth(ws)


# ─────────────────────────────────────────────────────────────────────────────
# Sheet 5 — Individual company links
# ─────────────────────────────────────────────────────────────────────────────

def _sheet_company_links(wb: Workbook, summaries: list[CompanySummary]) -> None:
    ws = wb.create_sheet("Company Reports")
    ws.cell(row=1, column=1, value="Company").font = _HEADER_FONT
    ws.cell(row=1, column=2, value="Detail Report").font = _HEADER_FONT
    ws["A1"].fill = _NAVY
    ws["B1"].fill = _NAVY

    for i, s in enumerate(summaries, 2):
        ws.cell(row=i, column=1, value=s.company_name).font = _BODY_FONT
        cell = ws.cell(row=i, column=2)
        if s.detail_report_path:
            cell.value = s.detail_report_path
            cell.hyperlink = s.detail_report_path
            cell.font = Font(name="Calibri", size=9, color="0563C1", underline="single")
        else:
            cell.value = "—"
            cell.font = _BODY_FONT

    _autowidth(ws)


# ─────────────────────────────────────────────────────────────────────────────
# Cohort dashboard PDF
# ─────────────────────────────────────────────────────────────────────────────

def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, round(pct * (len(ordered) - 1))))
    return ordered[idx]


def _build_dashboard_pdf(summaries: list[CompanySummary], narrative: str) -> bytes:
    buf = BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
        topMargin=15 * mm, bottomMargin=15 * mm,
    )
    styles = {
        "h1": ParagraphStyle("h1", fontName="Helvetica-Bold", fontSize=16,
                              textColor=rl_colors.HexColor("#1F3864"), spaceAfter=6),
        "h2": ParagraphStyle("h2", fontName="Helvetica-Bold", fontSize=11,
                              textColor=rl_colors.HexColor("#44546A"), spaceAfter=4),
        "body": ParagraphStyle("body", fontName="Helvetica", fontSize=9, leading=12, spaceAfter=4),
    }
    navy = rl_colors.HexColor("#1F3864")
    story = [
        Paragraph("COHORT DASHBOARD", styles["h1"]),
        Paragraph(f"Generated: {datetime.date.today()} | Companies analysed: {len(summaries)}",
                  styles["body"]),
        HRFlowable(width="100%", thickness=1, color=navy),
        Spacer(1, 4 * mm),
    ]

    if not summaries:
        story.append(Paragraph("No companies in this cohort.", styles["body"]))
        doc.build(story)
        return buf.getvalue()

    story.append(Paragraph("Portfolio Narrative", styles["h2"]))
    story.append(Paragraph(narrative, styles["body"]))
    story.append(Spacer(1, 4 * mm))

    burns = [float(s.monthly_burn) for s in summaries]
    runways = [s.runway_months for s in summaries if s.runway_months is not None]
    nrrs = [s.nrr for s in summaries if s.nrr is not None]
    risks = [s.composite_risk_score for s in summaries]

    stats_data = [
        ["Metric", "Median", "P10", "P90"],
        ["Monthly Burn", _inr(statistics.median(burns)), _inr(_percentile(burns, 0.1)), _inr(_percentile(burns, 0.9))],
        ["Runway (mo)", f"{statistics.median(runways):.1f}" if runways else "—",
         f"{_percentile(runways, 0.1):.1f}" if runways else "—",
         f"{_percentile(runways, 0.9):.1f}" if runways else "—"],
        ["NRR", _pct(statistics.median(nrrs)) if nrrs else "—",
         _pct(_percentile(nrrs, 0.1)) if nrrs else "—", _pct(_percentile(nrrs, 0.9)) if nrrs else "—"],
        ["Risk Score", f"{statistics.median(risks):.0f}", f"{_percentile(risks, 0.1):.0f}", f"{_percentile(risks, 0.9):.0f}"],
    ]
    stats_tbl = Table(stats_data, colWidths=[45 * mm, 40 * mm, 40 * mm, 40 * mm])
    stats_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#44546A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, rl_colors.lightgrey),
        ("PADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(Paragraph("Cohort Statistics", styles["h2"]))
    story.append(stats_tbl)
    story.append(Spacer(1, 6 * mm))

    high_risk = sorted((s for s in summaries if s.composite_risk_score >= 50),
                        key=lambda s: -s.composite_risk_score)
    story.append(Paragraph(f"High-Risk Companies ({len(high_risk)})", styles["h2"]))
    if high_risk:
        hr_data = [["Company", "Risk Score", "Red Flags", "Compliance"]]
        for s in high_risk[:15]:
            hr_data.append([s.company_name, f"{s.composite_risk_score:.0f}",
                             str(s.red_flag_count), s.compliance_status])
        hr_tbl = Table(hr_data, colWidths=[60 * mm, 30 * mm, 30 * mm, 35 * mm])
        hr_tbl.setStyle(TableStyle([
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#C00000")),
            ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
            ("GRID", (0, 0), (-1, -1), 0.3, rl_colors.lightgrey),
            ("PADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(hr_tbl)
    else:
        story.append(Paragraph("No companies scored 50+ in this cohort.", styles["body"]))
    story.append(Spacer(1, 6 * mm))

    burn_sorted = sorted(summaries, key=lambda s: s.monthly_burn)
    n_outliers = max(1, round(len(summaries) * 0.1))
    story.append(Paragraph("Outliers (burn — top/bottom 10%)", styles["h2"]))
    outlier_data = [["Lowest Burn", "Highest Burn"]]
    lowest = ", ".join(s.company_name for s in burn_sorted[:n_outliers])
    highest = ", ".join(s.company_name for s in burn_sorted[-n_outliers:])
    outlier_data.append([lowest or "—", highest or "—"])
    out_tbl = Table(outlier_data, colWidths=[80 * mm, 80 * mm])
    out_tbl.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), rl_colors.HexColor("#44546A")),
        ("TEXTCOLOR", (0, 0), (-1, 0), rl_colors.white),
        ("GRID", (0, 0), (-1, -1), 0.3, rl_colors.lightgrey),
        ("PADDING", (0, 0), (-1, -1), 4),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ]))
    story.append(out_tbl)

    companies_with_detail = [s for s in summaries if s.risk_flags or s.compliance_exceptions]
    if companies_with_detail:
        story.append(PageBreak())
        story.append(Paragraph("Risk & Compliance Detail", styles["h1"]))
        story.append(HRFlowable(width="100%", thickness=1, color=navy))
        story.append(Spacer(1, 4 * mm))
        for s in companies_with_detail:
            story.append(Paragraph(
                f"{s.company_name} — risk {s.composite_risk_score:.0f}/100, "
                f"compliance: {s.compliance_status}",
                styles["h2"],
            ))
            for f in s.risk_flags:
                story.append(Paragraph(
                    f"<b>[{f.severity}] {f.detector_name}</b> — {f.description}", styles["body"],
                ))
            for e in s.compliance_exceptions:
                story.append(Paragraph(
                    f"<b>[{e.severity}] {e.rule_name}</b> ({e.regulatory_citation}) — "
                    f"{e.description}",
                    styles["body"],
                ))
            story.append(Spacer(1, 3 * mm))

    doc.build(story)
    return buf.getvalue()


# ─────────────────────────────────────────────────────────────────────────────
# Main report class
# ─────────────────────────────────────────────────────────────────────────────

class NetworkLensReport:
    """Cross-company comparison workbook + cohort dashboard PDF.

    Beyond the sortable comparison table, this surfaces the actual risk
    flags and compliance exceptions behind each company's counts (Sheet 2 /
    PDF page 2) and a portfolio-level narrative synthesised across the whole
    cohort — via Claude Haiku when ANTHROPIC_API_KEY is set, falling back to
    a rule-based summary otherwise (see analysis.portfolio_narrative).
    """

    def generate(
        self,
        summaries: list[CompanySummary],
        llm_enabled: bool = True,
        narrative: str | None = None,
    ) -> tuple[bytes, bytes]:
        """Returns (xlsx_bytes, pdf_bytes).

        Pass a pre-computed *narrative* (e.g. from calling
        build_portfolio_narrative() once to also display in a UI) to avoid a
        second LLM round-trip; otherwise one is generated here.
        """
        if narrative is None:
            narrative = build_portfolio_narrative(summaries, llm_enabled=llm_enabled)

        wb = Workbook()
        _sheet_comparison(wb, summaries)
        _sheet_risk_compliance_detail(wb, summaries, narrative)
        _sheet_risk_distribution(wb, summaries)
        _sheet_sector_breakdown(wb, summaries)
        _sheet_company_links(wb, summaries)

        buf = BytesIO()
        wb.save(buf)
        xlsx_bytes = buf.getvalue()

        pdf_bytes = _build_dashboard_pdf(summaries, narrative)
        return xlsx_bytes, pdf_bytes
