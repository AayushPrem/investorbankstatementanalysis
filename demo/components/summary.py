"""Header strip above the lens tabs: flag banner, KPI row, status badges, summary."""
from __future__ import annotations

import streamlit as st

from demo.components.formatting import badge, flag_count_badge, period_str
from demo.pipeline_runner import DemoResult
from reports.angel_lens import _fmt_amount, _fmt_growth, _fmt_runway

# ─────────────────────────────────────────────────────────────────────────────
# Section: Flag summary banner
# ─────────────────────────────────────────────────────────────────────────────
# Deliberately no verdict (INVESTABLE/MONITOR/CAUTION) and no composite risk
# score — just counts by severity for each finding category. The reader
# forms their own judgment; the tool reports what it found.

def render_flag_summary_banner(result: DemoResult) -> None:
    doc = result.doc
    n_txns = len(doc.transactions)

    badges = [
        flag_count_badge("Risk flags", result.risk_report.flags),
        flag_count_badge("Financial health alerts", result.health_report.alerts),
        flag_count_badge("Compliance", result.compliance_report.exceptions),
    ]
    if result.reconciliation_report.claims_provided:
        badges.append(flag_count_badge("Reconciliation", result.reconciliation_report.findings))

    st.markdown(
        f"""
        <div style="background:#F3F4F6;border:1.5px solid #1A2B4A;border-radius:8px;
                    padding:14px 20px;margin-bottom:12px;">
          <span style="color:#1A2B4A;font-weight:700;font-size:1rem;">
            {(doc.bank_name or '').upper()} &nbsp;|&nbsp; Account {doc.account_id}
            &nbsp;|&nbsp; {period_str(doc)} &nbsp;|&nbsp; {n_txns} transactions
          </span>
          <div style="margin-top:10px;">{'&nbsp;&nbsp;'.join(badges)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Section: KPI row
# ─────────────────────────────────────────────────────────────────────────────

def render_kpi_row(result: DemoResult) -> None:
    m  = result.metrics
    ca = result.customer_analytics
    with st.container(border=True):
        c1, c2, c3, c4, c5, c6 = st.columns(6)
        with c1:
            st.metric("Total Revenue", _fmt_amount(m.total_revenue))
        with c2:
            st.metric("Avg Monthly Burn", _fmt_amount(m.avg_monthly_burn) + "/mo")
        with c3:
            st.metric("Runway", _fmt_runway(m.runway_months))
        with c4:
            growth = m.avg_mom_growth
            delta = f"{float(growth)*100:+.1f}%" if growth is not None else None
            st.metric("MoM Growth", _fmt_growth(growth), delta=delta)
        with c5:
            active_counts = list(ca.monthly_active_customers.values())
            st.metric("Active Customers", active_counts[-1] if active_counts else 0)
        with c6:
            nrr_vals = list(ca.nrr_per_month.values())
            nrr = nrr_vals[-1] if nrr_vals else None
            nrr_str = f"{nrr*100:.0f}%" if nrr else "N/A"
            delta_nrr = f"{(nrr-1)*100:+.0f}%" if nrr is not None else None
            st.metric("Latest NRR", nrr_str, delta=delta_nrr)


def render_status_badges(result: DemoResult) -> None:
    status  = result.doc.validation_status
    v_colours = {
        "passed":      ("#166534", "#DCFCE7"),
        "failed":      ("#991B1B", "#FEE2E2"),
        "unvalidated": ("#1E40AF", "#DBEAFE"),
    }
    vfg, vbg = v_colours.get(str(status).lower(), ("#374151", "#F3F4F6"))
    n_rp = sum(1 for t in result.doc.transactions if t.is_related_party)
    st.markdown(
        '<div style="margin-bottom:8px;">'
        + badge(f"Validation: {str(status).upper()}", vfg, vbg)
        + '&nbsp;&nbsp;'
        + badge(
            f"{len(result.customer_analytics.monthly_active_customers)} months tracked",
            "#6B21A8", "#F3E8FF",
          )
        + ('&nbsp;&nbsp;' + badge(f"{n_rp} related-party txns", "#92400E", "#FEF3C7")
           if n_rp else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def render_tab_summary(result: DemoResult) -> None:
    """One-paragraph synthesis of the whole analysis — see
    analysis/report_narrative.py. No verdict, no score: describes what was
    found; the reader forms their own judgment."""
    st.markdown("### Summary")
    st.markdown(result.narrative)
