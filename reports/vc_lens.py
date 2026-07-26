"""VC Lens report — 6-sheet XLSX workbook + 3-page PDF summary.

Usage:
    from reports.vc_lens import AnalysisResult, VCLensReport
    xlsx_bytes, pdf_bytes = VCLensReport().generate(result)
"""
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from decimal import Decimal
from io import BytesIO

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from reportlab.lib import colors as rl_colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
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

from analysis.customer_analytics import CustomerAnalyticsReport, aggregator_caveat_text
from analysis.financial_analyst import FinancialMetrics
from analysis.financial_health_alerts import classify_burn_ratio, classify_concentration, classify_nrr
from analysis.risk import RiskReport, Severity
from schema.canonical import StatementDocument

log = logging.getLogger(__name__)

_ZERO = Decimal("0")

# ---------------------------------------------------------------------------
# Input container
# ---------------------------------------------------------------------------

@dataclass
class AnalysisResult:
    """All upstream analysis outputs needed by the VC lens report."""
    doc: StatementDocument
    metrics: FinancialMetrics
    risk_report: RiskReport
    customer_analytics: CustomerAnalyticsReport
    company_name: str | None = None
    health_report: object | None = None  # FinancialHealthReport, if available
    narrative: str | None = None  # one-paragraph summary — see analysis/report_narrative.py


# ---------------------------------------------------------------------------
# Shared formatting helpers
# ---------------------------------------------------------------------------

def _fmt_amount(amount: Decimal | float) -> str:
    a = Decimal(str(amount))
    abs_a = abs(a)
    sign = "-" if a < _ZERO else ""
    if abs_a >= Decimal("10000000"):
        return f"{sign}Rs.{abs_a / Decimal('10000000'):.1f}Cr"
    if abs_a >= Decimal("100000"):
        return f"{sign}Rs.{abs_a / Decimal('100000'):.1f}L"
    if abs_a >= Decimal("1000"):
        return f"{sign}Rs.{abs_a / Decimal('1000'):.0f}K"
    return f"{sign}Rs.{abs_a:.0f}"


def _fmt_pct(value: float | Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100:+.1f}%"


def _month_label(ym: str) -> str:
    return datetime.datetime.strptime(ym, "%Y-%m").strftime("%b %Y")


def _period_str(doc: StatementDocument) -> str:
    s = doc.statement_period_start.strftime("%b %Y")
    e = doc.statement_period_end.strftime("%b %Y")
    return s if s == e else f"{s} – {e}"


def _decimal_val(v: Decimal | None) -> float:
    return float(v) if v is not None else 0.0


# ---------------------------------------------------------------------------
# XLSX styles
# ---------------------------------------------------------------------------

_BLUE      = "1A2B4A"
_ACCENT    = "2563EB"
_GREEN_F   = "166534"
_GREEN_B   = "DCFCE7"
_RED_F     = "991B1B"
_RED_B     = "FEE2E2"
_AMBER_F   = "92400E"
_AMBER_B   = "FEF3C7"
_GREY_H    = "E5E7EB"
_GREY_L    = "F3F4F6"
_TEAL_B    = "DBEAFE"
_TEAL_F    = "1E40AF"

def _hfill(hex_color: str) -> PatternFill:
    return PatternFill("solid", fgColor=hex_color)

def _hfont(bold: bool = False, color: str = "000000", size: int = 10) -> Font:
    return Font(bold=bold, color=color, size=size, name="Calibri")

def _center() -> Alignment:
    return Alignment(horizontal="center", vertical="center", wrap_text=True)

def _right() -> Alignment:
    return Alignment(horizontal="right", vertical="center")

def _left() -> Alignment:
    return Alignment(horizontal="left", vertical="center", wrap_text=True)

def _thin_border() -> Border:
    side = Side(style="thin", color="CCCCCC")
    return Border(left=side, right=side, top=side, bottom=side)


def _write_header_row(ws, row: int, values: list[str], widths: list[int] | None = None) -> None:
    for col, val in enumerate(values, 1):
        cell = ws.cell(row=row, column=col, value=val)
        cell.fill = _hfill(_BLUE)
        cell.font = _hfont(bold=True, color="FFFFFF", size=10)
        cell.alignment = _center()
        cell.border = _thin_border()
    if widths:
        for col, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(col)].width = w


def _write_section_row(ws, row: int, label: str, col_span: int = 1) -> None:
    cell = ws.cell(row=row, column=1, value=label)
    cell.fill = _hfill(_GREY_H)
    cell.font = _hfont(bold=True, color=_BLUE, size=10)
    cell.alignment = _left()


def _write_kv_row(
    ws, row: int, label: str, value: object,
    value_fmt: str | None = None, explanation: str | None = None
) -> None:
    lc = ws.cell(row=row, column=1, value=label)
    lc.font = _hfont(color="374151")
    lc.alignment = _left()
    vc = ws.cell(row=row, column=2, value=value)
    vc.font = _hfont(bold=True, color=_BLUE)
    vc.alignment = _right()
    if value_fmt:
        vc.number_format = value_fmt
    lc.fill = _hfill(_GREY_L)
    vc.fill = _hfill(_GREY_L)
    if explanation is not None:
        ec = ws.cell(row=row, column=3, value=explanation)
        ec.font = _hfont(color="6B7280", size=9)
        ec.alignment = _left()
        ec.fill = _hfill("FFFFFF")


def _apply_row_stripe(ws, row: int, cols: int, odd: bool) -> None:
    fill = _hfill("F9FAFB") if odd else _hfill("FFFFFF")
    for col in range(1, cols + 1):
        ws.cell(row=row, column=col).fill = fill


# ---------------------------------------------------------------------------
# Sheet 1 — Summary (with explanation column)
# ---------------------------------------------------------------------------

def _sheet1_summary(wb: Workbook, result: AnalysisResult) -> None:
    ws = wb.create_sheet("Summary")
    ws.column_dimensions["A"].width = 32
    ws.column_dimensions["B"].width = 22
    ws.column_dimensions["C"].width = 50

    m = result.metrics
    r = result.risk_report
    ca = result.customer_analytics
    doc = result.doc

    row = 1

    ws.cell(row=row, column=1, value="BSAA · VC Lens Report").font = _hfont(bold=True, color=_BLUE, size=14)
    row += 1
    ws.cell(row=row, column=1, value=result.company_name or f"Account {doc.account_id}").font = _hfont(color="374151", size=11)
    ws.cell(row=row, column=2, value=f"Generated: {datetime.date.today().isoformat()}").alignment = _right()
    row += 1
    ws.cell(row=row, column=1, value=_period_str(doc)).font = _hfont(color="6B7280")
    row += 1

    # SUMMARY (replaces the removed verdict banner — no INVESTABLE/MONITOR/
    # CAUTION label, no composite score; see analysis/report_narrative.py)
    if result.narrative:
        _write_section_row(ws, row, "SUMMARY")
        row += 1
        nc = ws.cell(row=row, column=1, value=result.narrative)
        nc.font = _hfont(color="374151", size=9)
        nc.alignment = _left()
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        ws.row_dimensions[row].height = 60
        row += 2

    # Explanation column header
    ws.cell(row=row, column=3, value="What this means").font = _hfont(bold=True, color="6B7280", size=9)
    row += 1

    # KEY FINANCIAL METRICS
    _write_section_row(ws, row, "KEY FINANCIAL METRICS")
    row += 1

    kvs = [
        ("Total Revenue",
         float(m.total_revenue), "#,##0",
         "All customer inflows over the statement period. Primary top-line health indicator."),
        ("Total Burn",
         float(m.total_burn), "#,##0",
         "All operating outflows (salaries + vendors + taxes + fees) for the period."),
        ("Avg Monthly Revenue",
         float(m.avg_monthly_revenue), "#,##0",
         "Average monthly inflow from customers — foundation of the financial model."),
        ("Avg Monthly Burn",
         float(m.avg_monthly_burn), "#,##0",
         "Average monthly operating cost. Compare to revenue to assess path to profitability."),
        ("Closing Balance",
         float(m.closing_balance), "#,##0",
         "Cash in bank at period end. This is the starting capital for the next period."),
        ("Runway (months)",
         float(m.runway_months) if m.runway_months is not None else "Profitable",
         "#,##0.0",
         "Months until zero cash at current burn rate. ≥12 = comfortable, <6 = urgent, <3 = critical."),
        ("Avg MoM Revenue Growth",
         _fmt_pct(m.avg_mom_growth), None,
         ">5% = strong growth / 0–5% = stable / <0% = declining. Target >5% for Series A readiness."),
    ]
    for label, val, fmt, expl in kvs:
        _write_kv_row(ws, row, label, val, fmt, expl)
        row += 1

    row += 1

    # RISK ASSESSMENT
    _write_section_row(ws, row, "RISK ASSESSMENT")
    row += 1
    high = sum(1 for f in r.flags if f.severity == Severity.HIGH)
    med  = sum(1 for f in r.flags if f.severity == Severity.MEDIUM)
    low  = sum(1 for f in r.flags if f.severity == Severity.LOW)
    _write_kv_row(ws, row, "Total Red Flags", len(r.flags), None,
                  "Count of automated risk patterns. Each flag has an explanation in the Red Flags sheet.")
    row += 1
    _write_kv_row(ws, row, "Flags by Severity (H / M / L)", f"{high} / {med} / {low}", None,
                  "HIGH = immediate attention required. MEDIUM = monitor. LOW = informational.")
    row += 2

    # CUSTOMER HEALTH
    _write_section_row(ws, row, "CUSTOMER HEALTH SNAPSHOT")
    row += 1
    active_counts = list(ca.monthly_active_customers.values())
    latest_active = active_counts[-1] if active_counts else 0
    total_churn   = len(ca.churn_events)
    nrr_values    = list(ca.nrr_per_month.values())
    latest_nrr    = nrr_values[-1] if nrr_values else None

    _write_kv_row(ws, row, "Active Customers (latest month)", latest_active, None,
                  "Distinct revenue-paying customers in the most recent month.")
    row += 1
    _write_kv_row(ws, row, "Churn Events Detected", total_churn, None,
                  "Customers who stopped paying for 3+ consecutive months. High churn erodes ARR.")
    row += 1
    nrr_str = f"{latest_nrr*100:.1f}%" if latest_nrr is not None else "N/A"
    _write_kv_row(ws, row, "Latest NRR", nrr_str, None,
                  "Net Revenue Retention: ≥110% = expanding wallets (best-in-class), 100% = stable, <100% = shrinking.")
    row += 2

    # RISK NARRATIVE
    if r.narrative:
        _write_section_row(ws, row, "RISK NARRATIVE (AI-GENERATED)")
        row += 1
        nc = ws.cell(row=row, column=1, value=r.narrative)
        nc.font = _hfont(color="374151", size=9)
        nc.alignment = _left()
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=3)
        ws.row_dimensions[row].height = 60
        row += 2

    # TOP FINDINGS
    _write_section_row(ws, row, "TOP FINDINGS")
    row += 1
    top_flags = r.flags[:6]
    if not top_flags:
        ws.cell(row=row, column=1, value="No flags detected in scope — see Red Flags sheet for detector coverage.").font = _hfont(color="166534")
        row += 1
    else:
        for flag in top_flags:
            sev_color = _RED_F if flag.severity == Severity.HIGH else (
                _AMBER_F if flag.severity == Severity.MEDIUM else _GREEN_F)
            sev_cell = ws.cell(row=row, column=1, value=f"[{flag.severity}] {flag.detector_name}")
            sev_cell.font = _hfont(bold=True, color=sev_color)
            desc_cell = ws.cell(row=row, column=2, value=flag.description)
            desc_cell.alignment = _left()
            desc_cell.font = _hfont(color="374151")
            row += 1


# ---------------------------------------------------------------------------
# Sheet 2 — Financial Health
# ---------------------------------------------------------------------------

def _sheet2_financial_health(wb: Workbook, result: AnalysisResult) -> None:
    ws = wb.create_sheet("Financial Health")
    headers = ["Month", "Revenue (Rs)", "Burn (Rs)", "Net (Rs)", "MoM Growth",
               "Revenue trend", "Burn note"]
    widths  = [14, 18, 18, 18, 14, 30, 30]
    _write_header_row(ws, 1, headers, widths)

    # Add a note row explaining columns
    note_row = 2
    notes = ["", "Total customer credits", "Total operating debits",
             "Revenue minus Burn", "Month-over-month revenue change",
             "Interpretation of growth signal", "Cost structure note"]
    for col, note in enumerate(notes, 1):
        c = ws.cell(row=note_row, column=col, value=note)
        c.font = _hfont(color="6B7280", size=8)
        c.alignment = _left()
        c.fill = _hfill("F9FAFB")

    for i, ms in enumerate(result.metrics.monthly_stats, 3):
        odd = (i % 2 == 0)
        _apply_row_stripe(ws, i, len(headers), odd)

        growth_idx = i - 4  # offset for note row
        growth_rates = result.metrics.mom_revenue_growth_rates
        growth_val = float(growth_rates[growth_idx]) if 0 <= growth_idx < len(growth_rates) else None

        ws.cell(row=i, column=1, value=_month_label(ms.year_month)).alignment = _left()
        for col, val in enumerate([float(ms.revenue), float(ms.burn), float(ms.net)], 2):
            c = ws.cell(row=i, column=col, value=val)
            c.number_format = "#,##0"
            c.alignment = _right()
            c.border = _thin_border()

        if growth_val is not None:
            gc = ws.cell(row=i, column=5, value=growth_val)
            gc.number_format = "0.0%"
            gc.alignment = _right()
            gc.border = _thin_border()
            # Interpretation
            if growth_val > 0.05:
                signal = "Strong — >5% monthly growth signals healthy market traction"
            elif growth_val >= 0:
                signal = "Stable — positive but below 5% growth threshold"
            else:
                signal = f"Declining — investigate root cause (churn vs acquisition)"
            ws.cell(row=i, column=6, value=signal).font = _hfont(
                color=_GREEN_F if growth_val > 0.05 else (_AMBER_F if growth_val >= 0 else _RED_F),
                size=9)
        else:
            ws.cell(row=i, column=5, value="—").alignment = _center()
            ws.cell(row=i, column=6, value="First month — no prior month to compare").font = _hfont(color="6B7280", size=9)

        # Burn note — bands shared with analysis/financial_health_alerts.py
        burn_ratio = float(ms.burn) / float(ms.revenue) if ms.revenue > _ZERO else None
        if burn_ratio is not None:
            band = classify_burn_ratio(ms.burn, ms.revenue)
            if band == "normal":
                burn_note = f"Burn/Revenue = {burn_ratio:.0%} — within normal range"
            elif band == "high":
                burn_note = f"Burn/Revenue = {burn_ratio:.0%} — spending significantly more than earned"
            else:
                burn_note = f"Burn/Revenue = {burn_ratio:.0%} — unsustainable burn relative to revenue"
            ws.cell(row=i, column=7, value=burn_note).font = _hfont(
                color=_GREEN_F if band == "normal" else (_AMBER_F if band == "high" else _RED_F),
                size=9)

    # Conditional formatting: MoM growth column
    last_data_row = 2 + len(result.metrics.monthly_stats)
    if last_data_row >= 3:
        growth_range = f"E3:E{last_data_row}"
        ws.conditional_formatting.add(
            growth_range,
            CellIsRule(operator="greaterThan", formula=["0"],
                       fill=_hfill(_GREEN_B), font=Font(color=_GREEN_F))
        )
        ws.conditional_formatting.add(
            growth_range,
            CellIsRule(operator="lessThan", formula=["0"],
                       fill=_hfill(_RED_B), font=Font(color=_RED_F))
        )


# ---------------------------------------------------------------------------
# Sheet 3 — Red Flags (with "What this means" column)
# ---------------------------------------------------------------------------

_FLAG_EXPLANATIONS: dict[str, str] = {
    "StructuringDetector":
        "Cash deposits just under the ₹2L reporting threshold suggest deliberate splitting to "
        "avoid banking scrutiny under §269ST. Could indicate undisclosed cash revenue.",
    "RoundTrippingDetector":
        "Money leaves and returns via the same counterparty within days. A classic "
        "mechanism for inflating bank balances or disguising fictitious revenue.",
    "RevenueSpikeDetector":
        "Revenue jumped more than 3× in a single month with no prior growth trend. "
        "Could be genuine — verify with signed contracts and invoices.",
    "RevenueDrainDetector":
        "Revenue dropped more than 50% month-over-month. Could signal customer loss, "
        "seasonal effects, or invoicing timing. Request customer contracts.",
    "ConcentrationDetector":
        "Over 70% of revenue from a single customer. Loss of this one relationship would "
        "be existential. Verify contract duration and exclusivity terms.",
    "RoundAmountsDetector":
        "Unusually high proportion of transactions are perfectly round numbers. "
        "Real-world business activity rarely produces this pattern — warrants scrutiny.",
    "FounderExtractionDetector":
        "Withdrawals to founder personal accounts significantly exceed declared salary. "
        "Could represent informal compensation or asset extraction.",
}

def _get_flag_explanation(detector_name: str) -> str:
    for key, expl in _FLAG_EXPLANATIONS.items():
        if key.lower() in detector_name.lower() or detector_name.lower() in key.lower():
            return expl
    return "Automated risk pattern detected — review triggering transactions for context."


def _sheet3_red_flags(wb: Workbook, result: AnalysisResult) -> None:
    ws = wb.create_sheet("Red Flags")
    headers = ["Detector", "Severity", "Description", "What This Means for Investors",
               "Triggering Transaction IDs"]
    widths  = [24, 12, 40, 50, 36]
    _write_header_row(ws, 1, headers, widths)

    if not result.risk_report.flags:
        ws.cell(row=2, column=1, value="No flags detected across the 6 automated detectors in scope.").font = _hfont(color="166534")
        return

    for i, flag in enumerate(result.risk_report.flags, 2):
        sev_color = (_RED_F if flag.severity == Severity.HIGH
                     else _AMBER_F if flag.severity == Severity.MEDIUM else _GREEN_F)
        sev_bg    = (_RED_B if flag.severity == Severity.HIGH
                     else _AMBER_B if flag.severity == Severity.MEDIUM else _GREEN_B)

        ws.cell(row=i, column=1, value=flag.detector_name).alignment = _left()
        sev_c = ws.cell(row=i, column=2, value=str(flag.severity))
        sev_c.fill = _hfill(sev_bg)
        sev_c.font = _hfont(bold=True, color=sev_color)
        sev_c.alignment = _center()

        ws.cell(row=i, column=3, value=flag.description).alignment = _left()

        expl_c = ws.cell(row=i, column=4, value=_get_flag_explanation(flag.detector_name))
        expl_c.font = _hfont(color="374151", size=9)
        expl_c.alignment = _left()

        ids_str = ", ".join(flag.triggering_transaction_ids[:8])
        if len(flag.triggering_transaction_ids) > 8:
            ids_str += f" … (+{len(flag.triggering_transaction_ids) - 8} more)"
        ws.cell(row=i, column=5, value=ids_str).alignment = _left()

        ws.row_dimensions[i].height = 45
        for col in range(1, 6):
            ws.cell(row=i, column=col).border = _thin_border()

    # Risk narrative at the bottom
    row = len(result.risk_report.flags) + 3
    _write_section_row(ws, row, "ANALYST RISK NARRATIVE")
    row += 1
    nc = ws.cell(row=row, column=1, value=result.risk_report.narrative)
    nc.font = _hfont(color="374151", size=9)
    nc.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=5)
    ws.row_dimensions[row].height = 60


# ---------------------------------------------------------------------------
# Sheet 4 — Customer Analytics (with explanatory context)
# ---------------------------------------------------------------------------

def _sheet4_customer_analytics(wb: Workbook, result: AnalysisResult) -> None:
    ws = wb.create_sheet("Customer Analytics")
    ws.column_dimensions["A"].width = 16

    ca = result.customer_analytics
    row = 1

    # Explanation of what customer analytics means
    intro = ws.cell(row=row, column=1, value=(
        "Customer analytics uses transaction-level data to measure retention, churn, "
        "and revenue concentration — key signals of business durability."
    ))
    intro.font = _hfont(color="374151", size=9)
    intro.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 2

    caveat = aggregator_caveat_text(ca)
    if caveat:
        cell = ws.cell(row=row, column=1, value=caveat)
        cell.font = _hfont(color=_RED_F, size=9, bold=True)
        cell.alignment = _left()
        ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
        row += 2

    # — Active customers per month —
    _write_section_row(ws, row, "ACTIVE CUSTOMERS PER MONTH")
    row += 1
    note = ws.cell(row=row, column=1, value=(
        "Count of distinct revenue-paying customers each month. "
        "Rising trend = acquiring more than churning. Flat = replacement-level. Declining = net churn."
    ))
    note.font = _hfont(color="6B7280", size=8)
    note.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 1
    _write_header_row(ws, row, ["Month", "Active Customers", "Change", "Signal"])
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 12
    ws.column_dimensions["D"].width = 30
    row += 1
    prev_count = None
    for ym, count in sorted(ca.monthly_active_customers.items()):
        ws.cell(row=row, column=1, value=_month_label(ym)).alignment = _left()
        ws.cell(row=row, column=2, value=count).alignment = _right()
        if prev_count is not None:
            delta = count - prev_count
            dc = ws.cell(row=row, column=3, value=delta)
            dc.alignment = _right()
            dc.font = _hfont(color=_GREEN_F if delta >= 0 else _RED_F)
            signal = "Growing" if delta > 0 else ("Stable" if delta == 0 else "Declining")
            ws.cell(row=row, column=4, value=signal).font = _hfont(
                color=_GREEN_F if delta > 0 else (_AMBER_F if delta == 0 else _RED_F))
        row += 1
        prev_count = count
    row += 1

    # — NRR per month —
    _write_section_row(ws, row, "NET REVENUE RETENTION (NRR) PER MONTH")
    row += 1
    note2 = ws.cell(row=row, column=1, value=(
        "NRR measures how much revenue existing customers generate next month vs this month. "
        "≥100% = stable or expanding · 85-99% = below par · 70-84% = contracting · <70% = severe contraction."
    ))
    note2.font = _hfont(color="6B7280", size=8)
    note2.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 1
    _write_header_row(ws, row, ["Month", "NRR", "Interpretation"])
    ws.column_dimensions["C"].width = 40
    nrr_start_row = row + 1
    row += 1
    for ym, nrr in sorted(ca.nrr_per_month.items()):
        ws.cell(row=row, column=1, value=_month_label(ym)).alignment = _left()
        c = ws.cell(row=row, column=2, value=nrr)
        c.number_format = "0.0%"
        c.alignment = _right()
        band = classify_nrr(nrr)
        if band == "healthy" and nrr >= 1.1:
            interp, color = "Excellent — existing customers expanding wallet share", _GREEN_F
        elif band == "healthy":
            interp, color = "Stable — churn balanced by expansion; room to grow", _TEAL_F
        elif band == "below_par":
            interp, color = "Below par — not yet generating net expansion from existing customers", _AMBER_F
        elif band == "contraction":
            interp, color = "Contraction — losing existing revenue; investigate churn causes", _AMBER_F
        else:
            interp, color = "Severe contraction — critical revenue shrinkage from existing base", _RED_F
        ic = ws.cell(row=row, column=3, value=interp)
        ic.font = _hfont(color=color, size=9)
        ic.alignment = _left()
        row += 1
    nrr_end_row = row - 1

    if nrr_end_row >= nrr_start_row:
        nrr_range = f"B{nrr_start_row}:B{nrr_end_row}"
        ws.conditional_formatting.add(
            nrr_range,
            CellIsRule(operator="greaterThanOrEqual", formula=["1"],
                       fill=_hfill(_GREEN_B), font=Font(color=_GREEN_F))
        )
        ws.conditional_formatting.add(
            nrr_range,
            CellIsRule(operator="lessThan", formula=["1"],
                       fill=_hfill(_RED_B), font=Font(color=_RED_F))
        )
    row += 1

    # — Churn events —
    _write_section_row(ws, row, "CHURN EVENTS (3-MONTH SILENCE RULE)")
    row += 1
    note3 = ws.cell(row=row, column=1, value=(
        "A customer is flagged as churned if they paid in earlier months but have been "
        "completely silent (no revenue transactions) for 3+ consecutive months."
    ))
    note3.font = _hfont(color="6B7280", size=8)
    note3.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 1
    _write_header_row(ws, row,
        ["Customer ID", "Last Payment Date", "Avg Monthly Spend (Rs)", "Months Active"],
        [22, 20, 26, 16])
    ws.column_dimensions["C"].width = 26
    ws.column_dimensions["D"].width = 16
    row += 1
    if not ca.churn_events:
        ws.cell(row=row, column=1, value="No churn events detected.").font = _hfont(color="166534")
        row += 1
    else:
        for evt in ca.churn_events:
            ws.cell(row=row, column=1, value=evt.customer_id)
            ws.cell(row=row, column=2, value=str(evt.last_payment_date))
            c = ws.cell(row=row, column=3, value=float(evt.previous_avg_monthly_spend))
            c.number_format = "#,##0"
            c.alignment = _right()
            ws.cell(row=row, column=4, value=evt.months_active_before_churn)
            row += 1
    row += 1

    # — Cohort retention matrix —
    _write_section_row(ws, row, "COHORT RETENTION MATRIX")
    row += 1
    note4 = ws.cell(row=row, column=1, value=(
        "Each row is a cohort (customers who paid for the first time in that month). "
        "Month +0 = 100% (cohort size). Month +1 = % still paying the next month. "
        "Healthy B2B SaaS: >80% Month +1 retention."
    ))
    note4.font = _hfont(color="6B7280", size=8)
    note4.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=8)
    row += 1
    if ca.cohort_retention:
        max_offset = max(max(v) for v in ca.cohort_retention.values())
        cohort_header = ["Cohort"] + [f"Month +{n}" for n in range(max_offset + 1)]
        _write_header_row(ws, row, cohort_header)
        row += 1
        for cohort_month in sorted(ca.cohort_retention):
            offsets = ca.cohort_retention[cohort_month]
            ws.cell(row=row, column=1, value=_month_label(cohort_month))
            for n in range(max_offset + 1):
                val = offsets.get(n)
                if val is not None:
                    c = ws.cell(row=row, column=n + 2, value=val)
                    c.number_format = "0%"
                    c.alignment = _center()
                    c.fill = _hfill(_GREEN_B if val >= 0.8 else (_AMBER_B if val >= 0.5 else _RED_B))
            row += 1
    row += 1

    # — Concentration trajectory —
    _write_section_row(ws, row, "REVENUE CONCENTRATION TRAJECTORY")
    row += 1
    note5 = ws.cell(row=row, column=1, value=(
        "Measures how much revenue is controlled by the top customers each month. "
        "Top-3 share >80% = high concentration risk, >60% = moderate. Declining trend = healthy diversification."
    ))
    note5.font = _hfont(color="6B7280", size=8)
    note5.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=4)
    row += 1
    _write_header_row(ws, row, ["Month", "Top-3 Share", "Top-10 Share", "Signal"])
    row += 1
    for pt in ca.concentration_trajectory:
        ws.cell(row=row, column=1, value=_month_label(pt.month)).alignment = _left()
        for col, val in [(2, pt.top3_share), (3, pt.top10_share)]:
            c = ws.cell(row=row, column=col, value=val)
            c.number_format = "0.0%"
            c.alignment = _right()
        band = classify_concentration(pt.top3_share)
        signal = {"high": "High concentration risk", "medium": "Moderate — monitor",
                  "low": "Well diversified"}[band]
        sc = ws.cell(row=row, column=4, value=signal)
        sc.font = _hfont(color={"high": _RED_F, "medium": _AMBER_F, "low": _GREEN_F}[band], size=9)
        row += 1


# ---------------------------------------------------------------------------
# Sheet 5 — All Transactions
# ---------------------------------------------------------------------------

def _sheet5_all_transactions(wb: Workbook, result: AnalysisResult) -> None:
    ws = wb.create_sheet("All Transactions")
    headers = [
        "ID", "Date", "Description", "Debit (Rs)", "Credit (Rs)",
        "Balance (Rs)", "Category", "Customer ID", "Related Party", "Anomaly Flags",
    ]
    widths = [20, 12, 36, 14, 14, 14, 20, 22, 16, 30]
    _write_header_row(ws, 1, headers, widths)

    for i, txn in enumerate(result.doc.transactions, 2):
        _apply_row_stripe(ws, i, len(headers), i % 2 == 0)
        row_data = [
            txn.transaction_id,
            str(txn.date),
            txn.description,
            float(txn.debit)   if txn.debit   is not None else None,
            float(txn.credit)  if txn.credit  is not None else None,
            float(txn.balance),
            str(txn.category)  if txn.category else "",
            txn.customer_id    or "",
            "Yes" if txn.is_related_party else "No",
            ", ".join(txn.anomaly_flags),
        ]
        for col, val in enumerate(row_data, 1):
            c = ws.cell(row=i, column=col, value=val)
            if col in (4, 5, 6) and val is not None:
                c.number_format = "#,##0"
                c.alignment = _right()
            elif col == 9:
                if val == "Yes":
                    c.fill = _hfill(_AMBER_B)
                    c.font = _hfont(color=_AMBER_F, bold=True)
            else:
                c.alignment = _left()


# ---------------------------------------------------------------------------
# Sheet 6 — Related Parties
# ---------------------------------------------------------------------------

def _sheet6_related_parties(wb: Workbook, result: AnalysisResult) -> None:
    ws = wb.create_sheet("Related Parties")

    rp_txns = [t for t in result.doc.transactions if t.is_related_party]

    headers = ["ID", "Date", "Description", "Debit (Rs)", "Credit (Rs)", "Affiliate"]
    widths  = [20, 12, 36, 14, 14, 24]
    _write_header_row(ws, 1, headers, widths)

    if not rp_txns:
        ws.cell(row=2, column=1, value=(
            "No related-party transactions found. "
            "Add affiliate names in the Streamlit sidebar to enable matching."
        )).font = _hfont(color="6B7280")
        return

    for i, txn in enumerate(rp_txns, 2):
        ws.cell(row=i, column=1, value=txn.transaction_id)
        ws.cell(row=i, column=2, value=str(txn.date)).alignment = _center()
        ws.cell(row=i, column=3, value=txn.description).alignment = _left()
        for col, val in [(4, txn.debit), (5, txn.credit)]:
            if val is not None:
                c = ws.cell(row=i, column=col, value=float(val))
                c.number_format = "#,##0"
                c.alignment = _right()
        ws.cell(row=i, column=6, value=txn.related_party_match or "").alignment = _left()
        for col in range(1, 7):
            ws.cell(row=i, column=col).border = _thin_border()

    # Aggregated summary
    row = len(rp_txns) + 3
    _write_section_row(ws, row, "AGGREGATED BY AFFILIATE")
    row += 1
    note = ws.cell(row=row, column=1, value=(
        "Related-party transactions with company affiliates, family members, or group entities. "
        "High debit totals to related parties may indicate asset extraction or non-arm's-length dealings."
    ))
    note.font = _hfont(color="6B7280", size=8)
    note.alignment = _left()
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=6)
    row += 1
    _write_header_row(ws, row,
        ["Affiliate", "Total Transactions", "Total Debit (Rs)", "Total Credit (Rs)", "Net Flow"])
    row += 1

    from collections import defaultdict
    agg: dict[str, dict] = defaultdict(lambda: {"txn_count": 0, "debit": _ZERO, "credit": _ZERO})
    for txn in rp_txns:
        key = txn.related_party_match or "(unknown)"
        agg[key]["txn_count"] += 1
        agg[key]["debit"]  += txn.debit  or _ZERO
        agg[key]["credit"] += txn.credit or _ZERO

    for affiliate, totals in sorted(agg.items()):
        ws.cell(row=row, column=1, value=affiliate)
        ws.cell(row=row, column=2, value=totals["txn_count"]).alignment = _right()
        for col, key in [(3, "debit"), (4, "credit")]:
            c = ws.cell(row=row, column=col, value=float(totals[key]))
            c.number_format = "#,##0"
            c.alignment = _right()
        net = totals["credit"] - totals["debit"]
        nc = ws.cell(row=row, column=5, value=float(net))
        nc.number_format = "#,##0"
        nc.font = _hfont(color=_GREEN_F if net >= _ZERO else _RED_F, bold=True)
        nc.alignment = _right()
        row += 1


# ---------------------------------------------------------------------------
# XLSX builder
# ---------------------------------------------------------------------------

def _build_xlsx(result: AnalysisResult) -> bytes:
    wb = Workbook()
    default = wb.active
    if default is not None:
        wb.remove(default)

    _sheet1_summary(wb, result)
    _sheet2_financial_health(wb, result)
    _sheet3_red_flags(wb, result)
    _sheet4_customer_analytics(wb, result)
    _sheet5_all_transactions(wb, result)
    _sheet6_related_parties(wb, result)

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# PDF styles
# ---------------------------------------------------------------------------

_RL_BLUE    = rl_colors.HexColor("#1A2B4A")
_RL_ACCENT  = rl_colors.HexColor("#2563EB")
_RL_GREEN   = rl_colors.HexColor("#166534")
_RL_GREEN_B = rl_colors.HexColor("#DCFCE7")
_RL_RED     = rl_colors.HexColor("#991B1B")
_RL_RED_B   = rl_colors.HexColor("#FEE2E2")
_RL_AMBER   = rl_colors.HexColor("#92400E")
_RL_AMBER_B = rl_colors.HexColor("#FEF3C7")
_RL_LGREY   = rl_colors.HexColor("#F3F4F6")
_RL_HGREY   = rl_colors.HexColor("#E5E7EB")
_RL_MID     = rl_colors.HexColor("#6B7280")
_RL_TEAL    = rl_colors.HexColor("#1E40AF")
_RL_TEAL_B  = rl_colors.HexColor("#DBEAFE")


def _ps(name: str, font: str = "Helvetica", size: float = 9,
        leading: float = 12, color: object = rl_colors.black,
        align: int = TA_LEFT, space_before: float = 0) -> ParagraphStyle:
    return ParagraphStyle(
        name, fontName=font, fontSize=size, leading=leading,
        textColor=color, alignment=align, spaceBefore=space_before,
    )


_PS_TITLE    = _ps("VCTitle",  "Helvetica-Bold", 13, 17, _RL_BLUE,  TA_LEFT)
_PS_SECTION  = _ps("VCSec",    "Helvetica-Bold",  9, 12, _RL_BLUE,  TA_LEFT, 5)
_PS_LABEL    = _ps("VCLbl",    "Helvetica",        7, 10, _RL_MID,  TA_LEFT)
_PS_VALUE    = _ps("VCVal",    "Helvetica-Bold",   9, 12, _RL_BLUE, TA_RIGHT)
_PS_BODY     = _ps("VCBody",   "Helvetica",        8, 11, rl_colors.black, TA_LEFT)
_PS_EXPL     = _ps("VCExpl",   "Helvetica",        7.5, 10, _RL_MID, TA_LEFT)
_PS_CAVEAT   = _ps("VCCaveat", "Helvetica-Bold",   8.5, 11, _RL_RED, TA_LEFT)
_PS_FOOTER   = _ps("VCFoot",   "Helvetica",        7,  9, _RL_MID,  TA_CENTER)
_PS_FLAG_HDR = _ps("VCFlagH",  "Helvetica-Bold",   8, 11, rl_colors.black, TA_LEFT)
_PS_FLAG     = _ps("VCFlag",   "Helvetica",        8, 11, rl_colors.black, TA_LEFT)


def _pdf_footer(story: list, cw: float) -> None:
    story.append(Spacer(1, 8))
    story.append(HRFlowable(width=cw, thickness=0.5, color=_RL_HGREY))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        "Generated by BSAA (Bank Statement Analysis Agent). "
        "For informational purposes only — not financial advice. "
        "Verify all figures against source bank statements.",
        _PS_FOOTER,
    ))


def _build_pdf(result: AnalysisResult) -> bytes:
    buf = BytesIO()
    margin = 18 * mm
    cw = A4[0] - 2 * margin

    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=margin, rightMargin=margin,
        topMargin=margin, bottomMargin=margin,
    )

    m    = result.metrics
    r    = result.risk_report
    ca   = result.customer_analytics
    doc_obj = result.doc

    story: list = []

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 1 — Executive Summary & Financial Health
    # ═══════════════════════════════════════════════════════════════════════════

    today = datetime.date.today().strftime("%d %b %Y")
    hdr_data = [[
        Paragraph("BSAA · VC Lens Report", _PS_TITLE),
        Paragraph(f"Generated: {today}", _ps("D", size=8, color=_RL_MID, align=TA_RIGHT)),
    ]]
    hdr_tbl = Table(hdr_data, colWidths=[cw * 0.7, cw * 0.3])
    hdr_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _RL_BLUE),
        ("TOPPADDING",    (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING",   (0, 0), (0, 0), 8),
        ("RIGHTPADDING",  (-1, 0), (-1, 0), 8),
        ("TEXTCOLOR",     (0, 0), (-1, -1), rl_colors.white),
    ]))
    story.append(hdr_tbl)
    story.append(Spacer(1, 4))

    company = result.company_name or f"Account {doc_obj.account_id}"
    period  = _period_str(doc_obj)
    story.append(Paragraph(company, _ps("Co", "Helvetica-Bold", 11, 14, _RL_BLUE)))
    story.append(Paragraph(
        f"{(doc_obj.bank_name or '').upper()} | {period} | "
        f"Account {doc_obj.account_id} | {len(doc_obj.transactions)} transactions",
        _PS_LABEL
    ))
    story.append(Spacer(1, 5))
    story.append(HRFlowable(width=cw, thickness=0.5, color=_RL_HGREY))
    story.append(Spacer(1, 5))

    # Summary paragraph (replaces the removed verdict banner — no
    # INVESTABLE/MONITOR/CAUTION label, no composite score; see
    # analysis/report_narrative.py)
    runway = m.runway_months
    growth = m.avg_mom_growth
    nrr_vals = list(ca.nrr_per_month.values())
    if result.narrative:
        story.append(Paragraph("Summary", _PS_SECTION))
        story.append(Paragraph(result.narrative, _PS_BODY))
        story.append(Spacer(1, 5))

    high = sum(1 for f in r.flags if f.severity == Severity.HIGH)
    med  = sum(1 for f in r.flags if f.severity == Severity.MEDIUM)
    low  = sum(1 for f in r.flags if f.severity == Severity.LOW)
    flag_data = [[
        Paragraph("Risk Flags", _ps("FSH", "Helvetica-Bold", 7.5, 10, _RL_MID, TA_CENTER)),
        Paragraph("Total", _ps("FSH2", "Helvetica-Bold", 7.5, 10, _RL_MID, TA_CENTER)),
        Paragraph("High", _ps("FSH3", "Helvetica-Bold", 7.5, 10, _RL_MID, TA_CENTER)),
        Paragraph("Medium", _ps("FSH4", "Helvetica-Bold", 7.5, 10, _RL_MID, TA_CENTER)),
        Paragraph("Low", _ps("FSH5", "Helvetica-Bold", 7.5, 10, _RL_MID, TA_CENTER)),
    ], [
        Paragraph("Detected", _ps("FSV0", "Helvetica", 8, 11, rl_colors.black, TA_LEFT)),
        Paragraph(str(len(r.flags)), _ps("FSV1", "Helvetica-Bold", 8, 11, _RL_BLUE, TA_CENTER)),
        Paragraph(str(high), _ps("FSV2", "Helvetica-Bold", 8, 11, _RL_BLUE, TA_CENTER)),
        Paragraph(str(med), _ps("FSV3", "Helvetica-Bold", 8, 11, _RL_BLUE, TA_CENTER)),
        Paragraph(str(low), _ps("FSV4", "Helvetica-Bold", 8, 11, _RL_BLUE, TA_CENTER)),
    ]]
    flag_tbl = Table(flag_data, colWidths=[cw * 0.30, cw * 0.175, cw * 0.175, cw * 0.175, cw * 0.175])
    flag_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, 0), _RL_LGREY),
        ("GRID",          (0, 0), (-1, -1), 0.3, _RL_HGREY),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    story.append(flag_tbl)
    story.append(Spacer(1, 6))

    # 5-KPI grid
    active_counts = list(ca.monthly_active_customers.values())
    latest_active = active_counts[-1] if active_counts else "—"
    latest_nrr = nrr_vals[-1] if nrr_vals else None

    kpi_labels = [
        ("Revenue", _fmt_amount(m.total_revenue), "Total customer inflows"),
        ("Avg Burn/mo", _fmt_amount(m.avg_monthly_burn), "Average monthly spend"),
        ("Runway", (f"{float(runway):.1f}mo" if runway else "Profitable"), "Cash at current burn"),
        ("MoM Growth", (_fmt_pct(growth) if growth else "N/A"), ">5% = strong"),
        ("NRR", (f"{latest_nrr*100:.0f}%" if latest_nrr else "N/A"), "≥100% = stable base"),
    ]

    kpi_rows = [
        [Paragraph(f"<b>{v}</b>", _ps(f"KV{i}", "Helvetica-Bold", 11, 14, _RL_BLUE, TA_CENTER))
         for i, (_, v, _) in enumerate(kpi_labels)],
        [Paragraph(f"{k}<br/><font size='6' color='#9CA3AF'>{note}</font>",
                   _ps(f"KL{i}", "Helvetica", 7, 10, _RL_MID, TA_CENTER))
         for i, (k, _, note) in enumerate(kpi_labels)],
    ]
    kpi_tbl = Table(kpi_rows, colWidths=[cw / 5] * 5)
    kpi_tbl.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (-1, -1), _RL_LGREY),
        ("TOPPADDING",    (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEAFTER",     (0, 0), (3, -1),  0.5, _RL_HGREY),
        ("ALIGN",         (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(kpi_tbl)
    story.append(Spacer(1, 8))

    # Monthly breakdown table
    story.append(Paragraph("Monthly Financial Breakdown", _PS_SECTION))
    story.append(Paragraph(
        "Revenue = customer credits · Burn = all operating debits · "
        "Net = Revenue minus Burn (green = surplus, red = deficit) · "
        "MoM Growth = month-over-month revenue change",
        _PS_EXPL
    ))
    story.append(Spacer(1, 3))

    months = m.monthly_stats[-6:]
    if months:
        growth_rates = m.mom_revenue_growth_rates
        tbl_data = [[
            Paragraph(h, _ps(f"MH{i}", "Helvetica-Bold", 8, 10, _RL_BLUE))
            for i, h in enumerate(["Month", "Revenue", "Burn", "Net", "MoM Growth"])
        ]]
        for idx, ms in enumerate(months):
            net_col = _RL_GREEN if ms.net >= _ZERO else _RL_RED
            g_idx = idx - 1
            gval = float(growth_rates[g_idx]) if 0 <= g_idx < len(growth_rates) else None
            gstr = f"{gval*100:+.1f}%" if gval is not None else "—"
            g_col = _RL_GREEN if (gval or 0) >= 0 else _RL_RED

            tbl_data.append([
                Paragraph(_month_label(ms.year_month), _PS_BODY),
                Paragraph(_fmt_amount(ms.revenue), _ps("MR", align=TA_RIGHT, size=8)),
                Paragraph(_fmt_amount(ms.burn),    _ps("MB", align=TA_RIGHT, size=8)),
                Paragraph(_fmt_amount(ms.net),     _ps("MN", align=TA_RIGHT, size=8, color=net_col)),
                Paragraph(gstr, _ps("MG", align=TA_RIGHT, size=8, color=g_col)),
            ])
        mon_tbl = Table(tbl_data, colWidths=[cw*0.22, cw*0.2, cw*0.2, cw*0.2, cw*0.18])
        mon_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), _RL_HGREY),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.25, _RL_HGREY),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [rl_colors.white, _RL_LGREY]),
        ]))
        story.append(mon_tbl)

    story.append(Spacer(1, 8))

    # Risk narrative (flag counts already shown at the top of this page)
    if r.narrative:
        story.append(Paragraph("Risk Narrative", _PS_SECTION))
        story.append(Spacer(1, 3))
        story.append(Paragraph(
            r.narrative[:400] + ("…" if len(r.narrative) > 400 else ""),
            _ps("RN", "Helvetica", 8, 11, _RL_MID, TA_LEFT)
        ))

    _pdf_footer(story, cw)

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 2 — Customer Analytics
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(PageBreak())

    story.append(Paragraph(
        f"Customer Analytics — {company}", _PS_TITLE
    ))
    story.append(Paragraph(
        "Derived from transaction-level analysis. Customer identity is resolved via "
        "semantic deduplication of payment narrations — no CRM data required.",
        _PS_EXPL
    ))
    story.append(Spacer(1, 6))

    caveat = aggregator_caveat_text(ca)
    if caveat:
        story.append(Paragraph(caveat, _PS_CAVEAT))
        story.append(Spacer(1, 6))

    # NRR trend table
    if ca.nrr_per_month:
        story.append(Paragraph("Net Revenue Retention (NRR) by Month", _PS_SECTION))
        story.append(Paragraph(
            "NRR measures what happens to revenue from existing customers over time. "
            "≥100% = stable or expanding · 85-99% = below par · 70-84% = contracting · <70% = severe contraction.",
            _PS_EXPL
        ))
        story.append(Spacer(1, 3))

        nrr_data = [[
            Paragraph(h, _ps(f"NH{i}", "Helvetica-Bold", 8, 10, _RL_BLUE))
            for i, h in enumerate(["Month", "NRR", "Signal", "Implication for Investors"])
        ]]
        for ym, nrr in sorted(ca.nrr_per_month.items()):
            band = classify_nrr(nrr)
            if band == "healthy" and nrr >= 1.1:
                sig, imp, col = "Excellent", "Existing customers expanding — strong upsell or pricing power", _RL_GREEN
            elif band == "healthy":
                sig, imp, col = "Stable", "Churn and expansion balanced — focus on upsell to push >110%", _RL_TEAL
            elif band == "below_par":
                sig, imp, col = "Below par", "Not yet generating net expansion from existing customers", _RL_AMBER
            elif band == "contraction":
                sig, imp, col = "Contraction", "Net revenue shrinkage — existing base contracting faster than it expands", _RL_AMBER
            else:
                sig, imp, col = "Critical", "Severe contraction — even perfect new sales cannot offset existing losses", _RL_RED
            nrr_data.append([
                Paragraph(_month_label(ym), _PS_BODY),
                Paragraph(f"{nrr*100:.1f}%", _ps("NV", "Helvetica-Bold", 8, 10, col, TA_RIGHT)),
                Paragraph(sig, _ps("NS", "Helvetica", 8, 10, col)),
                Paragraph(imp, _PS_EXPL),
            ])
        nrr_tbl = Table(nrr_data, colWidths=[cw*0.16, cw*0.12, cw*0.16, cw*0.56])
        nrr_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), _RL_HGREY),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.25, _RL_HGREY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, _RL_LGREY]),
        ]))
        story.append(nrr_tbl)
        story.append(Spacer(1, 8))

    # Active customers + trend
    if ca.monthly_active_customers:
        story.append(Paragraph("Active Customer Count by Month", _PS_SECTION))
        story.append(Paragraph(
            "Active = at least one revenue transaction in the month. "
            "Rising count = net new customer acquisition exceeds churn.",
            _PS_EXPL
        ))
        story.append(Spacer(1, 3))

        act_data = [[
            Paragraph(h, _ps(f"AH{i}", "Helvetica-Bold", 8, 10, _RL_BLUE))
            for i, h in enumerate(["Month", "Active", "Change", "Trend Signal"])
        ]]
        prev = None
        for ym, count in sorted(ca.monthly_active_customers.items()):
            delta_str = "—"
            sig_str = "First month"
            sig_col = _RL_MID
            if prev is not None:
                delta = count - prev
                delta_str = f"{delta:+d}"
                if delta > 0:
                    sig_str, sig_col = "Growing — net new customers acquired", _RL_GREEN
                elif delta == 0:
                    sig_str, sig_col = "Stable — acquisition matches churn", _RL_TEAL
                else:
                    sig_str, sig_col = "Declining — losing more customers than gained", _RL_RED
            act_data.append([
                Paragraph(_month_label(ym), _PS_BODY),
                Paragraph(str(count), _ps("AC", "Helvetica-Bold", 9, 12, _RL_BLUE, TA_RIGHT)),
                Paragraph(delta_str, _ps("AD", "Helvetica", 8, 10, _RL_MID, TA_RIGHT)),
                Paragraph(sig_str, _ps("AS", "Helvetica", 8, 10, sig_col)),
            ])
            prev = count
        act_tbl = Table(act_data, colWidths=[cw*0.18, cw*0.12, cw*0.12, cw*0.58])
        act_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), _RL_HGREY),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("LINEBELOW",     (0, 0), (-1, -2), 0.25, _RL_HGREY),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, _RL_LGREY]),
        ]))
        story.append(act_tbl)
        story.append(Spacer(1, 8))

    # Churn events
    story.append(Paragraph("Churn Events", _PS_SECTION))
    story.append(Paragraph(
        "A customer is flagged as churned if they were active in earlier months but have had "
        "no revenue transactions for 3+ consecutive months. Each churned customer represents "
        "permanent recurring revenue loss unless re-engaged.",
        _PS_EXPL
    ))
    story.append(Spacer(1, 3))
    churn_top = ca.churn_events[:8]
    if not churn_top:
        story.append(Paragraph(
            "No churn events detected — all customers who paid earlier are still active.", _PS_BODY))
    else:
        ch_data = [[
            Paragraph(h, _ps(f"CH{i}", "Helvetica-Bold", 8, 10, _RL_BLUE))
            for i, h in enumerate(["Customer ID", "Last Payment", "Avg Monthly Spend", "Months Active"])
        ]]
        for evt in churn_top:
            ch_data.append([
                Paragraph(evt.customer_id, _PS_BODY),
                Paragraph(str(evt.last_payment_date), _PS_BODY),
                Paragraph(_fmt_amount(evt.previous_avg_monthly_spend),
                          _ps("CM", align=TA_RIGHT, size=8)),
                Paragraph(str(evt.months_active_before_churn), _PS_BODY),
            ])
        ch_tbl = Table(ch_data, colWidths=[cw*0.3, cw*0.2, cw*0.28, cw*0.22])
        ch_tbl.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0), _RL_HGREY),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [rl_colors.white, _RL_LGREY]),
        ]))
        story.append(ch_tbl)
        if len(ca.churn_events) > 8:
            story.append(Spacer(1, 2))
            story.append(Paragraph(
                f"… and {len(ca.churn_events) - 8} more churn events. See the XLSX for the full list.",
                _PS_EXPL
            ))

    _pdf_footer(story, cw)

    # ═══════════════════════════════════════════════════════════════════════════
    # PAGE 3 — Risk Flags & Investment Considerations
    # ═══════════════════════════════════════════════════════════════════════════
    story.append(PageBreak())

    story.append(Paragraph(
        f"Risk Flags & Investment Considerations — {company}", _PS_TITLE
    ))
    story.append(Paragraph(
        "Automated red-flag detection runs 6 independent detectors on the transaction data. "
        "Each flag is scored (HIGH=10, MEDIUM=5, LOW=2 pts) and summed into a composite risk score. "
        "Flags do not necessarily indicate fraud — they highlight patterns that warrant due diligence.",
        _PS_EXPL
    ))
    story.append(Spacer(1, 6))

    if not r.flags:
        story.append(Paragraph(
            "No flags detected in scope across the 6 automated detectors: "
            "structuring, round-tripping, revenue spikes/drains, "
            "customer concentration, round amounts, and founder extraction.",
            _ps("CLEAN", color=_RL_GREEN, size=9)
        ))
    else:
        for flag in r.flags:
            sev_color = _RL_RED if flag.severity == Severity.HIGH else (
                _RL_AMBER if flag.severity == Severity.MEDIUM else _RL_GREEN)
            sev_bg = _RL_RED_B if flag.severity == Severity.HIGH else (
                _RL_AMBER_B if flag.severity == Severity.MEDIUM else _RL_GREEN_B)

            # Flag header row
            flag_hdr = [[
                Paragraph(f"[{flag.severity}] {flag.detector_name}",
                          _ps("FH", "Helvetica-Bold", 9, 12, sev_color)),
                Paragraph(
                    "HIGH — verify before investing" if flag.severity == Severity.HIGH else
                    "MEDIUM — monitor and ask founder" if flag.severity == Severity.MEDIUM else
                    "LOW — informational",
                    _ps("FS", "Helvetica", 7.5, 10, sev_color, TA_RIGHT)
                ),
            ]]
            fh_tbl = Table(flag_hdr, colWidths=[cw * 0.7, cw * 0.3])
            fh_tbl.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, -1), sev_bg),
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ("BOX",           (0, 0), (-1, -1), 0.5, sev_color),
            ]))
            story.append(fh_tbl)

            # Flag body: description + what it means
            expl = _get_flag_explanation(flag.detector_name)
            flag_body = [[
                Paragraph(f"<b>Finding:</b> {flag.description}", _PS_BODY),
                Paragraph(f"<b>Why it matters:</b> {expl}", _PS_EXPL),
            ]]
            fb_tbl = Table(flag_body, colWidths=[cw * 0.48, cw * 0.52])
            fb_tbl.setStyle(TableStyle([
                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LEFTPADDING",   (0, 0), (-1, -1), 6),
                ("LINEBELOW",     (0, 0), (-1, -1), 0.25, _RL_HGREY),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ]))
            story.append(fb_tbl)

            if flag.triggering_transaction_ids:
                ids = flag.triggering_transaction_ids[:6]
                ids_text = ", ".join(f"`{i}`" for i in ids)
                if len(flag.triggering_transaction_ids) > 6:
                    ids_text += f" … +{len(flag.triggering_transaction_ids)-6} more"
                story.append(Paragraph(
                    f"<i>Triggering transactions: {', '.join(ids)}"
                    f"{'…' if len(flag.triggering_transaction_ids)>6 else ''}</i>",
                    _ps("FT", "Helvetica", 7, 9, _RL_MID)
                ))
            story.append(Spacer(1, 4))

    # Narrative
    story.append(Spacer(1, 6))
    story.append(Paragraph("Analyst Narrative", _PS_SECTION))
    story.append(Spacer(1, 3))
    story.append(Paragraph(
        r.narrative or "No narrative available.",
        _ps("NA", "Helvetica", 8.5, 12, rl_colors.black)
    ))

    # Investment recommendations
    story.append(Spacer(1, 8))
    story.append(Paragraph("Due Diligence Checklist", _PS_SECTION))
    story.append(Paragraph(
        "Based on the automated analysis, verify the following before committing capital:",
        _PS_EXPL
    ))
    story.append(Spacer(1, 3))

    checks = []
    if any(str(f.severity) == "HIGH" for f in r.flags):
        checks.append("Request an independent CA-certified audit for the flagged transactions.")
    if m.runway_months is not None and m.runway_months < Decimal("6"):
        checks.append("Obtain a signed bridge-finance plan or cap table to understand dilution path.")
    if m.avg_mom_growth is not None and m.avg_mom_growth < _ZERO:
        checks.append("Request a customer-level cohort analysis to identify churn root cause.")
    if ca.churn_events:
        checks.append(f"Interview the {len(ca.churn_events)} churned customer(s) — understand why they left.")
    if m.top_customer_revenue_pct and float(m.top_customer_revenue_pct) > 0.5:
        checks.append("Obtain signed multi-year contract from the top revenue customer.")
    checks.append("Verify GST returns and TDS filings match the tax payments in the statement.")
    checks.append("Cross-check salary disbursements with employee payroll register.")

    for item in checks:
        story.append(Paragraph(f"&#9679; {item}", _ps("CI", "Helvetica", 8, 11, _RL_BLUE)))
        story.append(Spacer(1, 2))

    _pdf_footer(story, cw)

    doc.build(story)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class VCLensReport:
    """Generates a 6-sheet XLSX workbook and a 3-page PDF summary."""

    def generate(self, result: AnalysisResult) -> tuple[bytes, bytes]:
        """Return (xlsx_bytes, pdf_bytes)."""
        xlsx_bytes = _build_xlsx(result)
        pdf_bytes  = _build_pdf(result)
        log.info(
            "VC lens report generated: xlsx=%d bytes, pdf=%d bytes",
            len(xlsx_bytes), len(pdf_bytes),
        )
        return xlsx_bytes, pdf_bytes
