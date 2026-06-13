"""BSAA — Angel Investor Lens demo (Streamlit).

Run:
    streamlit run demo/app.py

Upload an HDFC or ICICI bank statement PDF.  The pipeline runs in ~2 seconds,
then shows key metrics, a monthly trend chart, a transaction table, and lets
you download the 1-page Angel Lens PDF report.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

# Ensure project root is on sys.path when Streamlit launches from any directory
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from analysis.financial_analyst import FinancialMetrics
from demo.pipeline_runner import DemoResult, run_pipeline_on_bytes
from reports.angel_lens import _fmt_amount, _fmt_growth, _fmt_runway, _verdict
from schema.canonical import StatementDocument

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="BSAA · Angel Lens",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

_VERDICT_COLOURS = {
    "INVESTABLE": ("#166534", "#DCFCE7"),
    "MONITOR":    ("#92400E", "#FEF3C7"),
    "CAUTION":    ("#991B1B", "#FEE2E2"),
}

_CATEGORY_COLOURS = {
    "REVENUE":            "#22C55E",
    "VENDOR_PAYMENT":     "#F97316",
    "SALARY":             "#3B82F6",
    "LOAN_IN":            "#A78BFA",
    "LOAN_OUT":           "#EC4899",
    "TAX":                "#EAB308",
    "TRANSFER":           "#94A3B8",
    "FOUNDER_WITHDRAWAL": "#F43F5E",
    "FEES":               "#6B7280",
    "OTHER":              "#D1D5DB",
}


# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def _period_str(doc: StatementDocument) -> str:
    s = doc.statement_period_start.strftime("%b %Y")
    e = doc.statement_period_end.strftime("%b %Y")
    return s if s == e else f"{s} – {e}"


def _month_label(ym: str) -> str:
    from datetime import datetime
    return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")


def _render_verdict_banner(result: DemoResult) -> None:
    label = result.verdict_label
    fg, bg = _VERDICT_COLOURS.get(label, ("#1A2B4A", "#F3F4F6"))
    doc = result.doc
    bank = (doc.bank_name or "").upper()
    n_txns = len(doc.transactions)

    st.markdown(
        f"""
        <div style="background:{bg};border:1.5px solid {fg};border-radius:8px;
                    padding:14px 20px;margin-bottom:12px;">
          <span style="font-size:1.3rem;font-weight:700;color:{fg};">
            &#9679; {label}
          </span>
          <span style="color:#6B7280;margin-left:20px;font-size:0.9rem;">
            {bank} &nbsp;|&nbsp; Account {doc.account_id}
            &nbsp;|&nbsp; {_period_str(doc)} &nbsp;|&nbsp; {n_txns} transactions
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_kpi_row(metrics: FinancialMetrics) -> None:
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Total Revenue", _fmt_amount(metrics.total_revenue))
    with c2:
        st.metric("Avg Monthly Burn", _fmt_amount(metrics.avg_monthly_burn) + "/mo")
    with c3:
        st.metric("Runway", _fmt_runway(metrics.runway_months))
    with c4:
        growth = metrics.avg_mom_growth
        delta = f"{float(growth)*100:+.1f}%" if growth is not None else None
        st.metric("Avg MoM Growth", _fmt_growth(growth), delta=delta)


def _render_monthly_chart(metrics: FinancialMetrics) -> None:
    import plotly.graph_objects as go

    months = metrics.monthly_stats
    if not months:
        st.info("No monthly data available.")
        return

    labels = [_month_label(m.year_month) for m in months]
    revenues = [float(m.revenue) for m in months]
    burns    = [float(m.burn)    for m in months]
    nets     = [float(m.net)     for m in months]

    fig = go.Figure()
    fig.add_bar(name="Revenue", x=labels, y=revenues,
                marker_color="#22C55E", opacity=0.85)
    fig.add_bar(name="Burn",    x=labels, y=burns,
                marker_color="#F97316", opacity=0.85)
    fig.add_scatter(name="Net cash flow", x=labels, y=nets, mode="lines+markers",
                    line=dict(color="#3B82F6", width=2), marker=dict(size=7))
    fig.update_layout(
        barmode="group", title="Monthly Revenue vs Burn (INR)",
        xaxis_title="Month", yaxis_title="Amount (INR)",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        height=360, margin=dict(l=0, r=0, t=40, b=0),
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_category_pie(doc: StatementDocument) -> None:
    import plotly.graph_objects as go

    counts: dict[str, int] = {}
    for t in doc.transactions:
        key = t.category.value if t.category else "OTHER"
        counts[key] = counts.get(key, 0) + 1
    if not counts:
        return

    labels  = list(counts.keys())
    values  = list(counts.values())
    colours = [_CATEGORY_COLOURS.get(lb, "#D1D5DB") for lb in labels]

    fig = go.Figure(go.Pie(
        labels=labels, values=values, marker_colors=colours,
        hole=0.45, textinfo="label+percent",
    ))
    fig.update_layout(
        title="Transaction Categories", height=340,
        margin=dict(l=0, r=0, t=40, b=0),
        showlegend=False, paper_bgcolor="rgba(0,0,0,0)",
    )
    st.plotly_chart(fig, use_container_width=True)


def _render_transaction_table(doc: StatementDocument) -> None:
    import pandas as pd

    if not doc.transactions:
        st.info("No transactions extracted.")
        return

    rows = [
        {
            "Date":        str(t.date),
            "Description": t.description[:60] + ("…" if len(t.description) > 60 else ""),
            "Debit":       float(t.debit)   if t.debit   else None,
            "Credit":      float(t.credit)  if t.credit  else None,
            "Balance":     float(t.balance),
            "Category":    t.category.value if t.category else "",
        }
        for t in doc.transactions
    ]
    df = pd.DataFrame(rows)

    categories = sorted(df["Category"].unique().tolist())
    selected = st.multiselect(
        "Filter by category", categories, default=categories, key="cat_filter"
    )
    filtered = df[df["Category"].isin(selected)] if selected else df

    st.dataframe(
        filtered, use_container_width=True, height=400,
        column_config={
            "Debit":   st.column_config.NumberColumn("Debit (INR)",   format="₹%.2f"),
            "Credit":  st.column_config.NumberColumn("Credit (INR)",  format="₹%.2f"),
            "Balance": st.column_config.NumberColumn("Balance (INR)", format="₹%.2f"),
        },
        hide_index=True,
    )
    st.caption(f"{len(filtered):,} of {len(df):,} transactions shown")


def _render_validation_badge(doc: StatementDocument) -> None:
    status = doc.validation_status
    colours = {
        "passed":      ("#166534", "#DCFCE7"),
        "failed":      ("#991B1B", "#FEE2E2"),
        "unvalidated": ("#1E40AF", "#DBEAFE"),
    }
    fg, bg = colours.get(status, ("#374151", "#F3F4F6"))
    st.markdown(
        f'<span style="background:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:4px;font-weight:600;font-size:0.85rem;">'
        f'Validation: {status.upper()}</span>',
        unsafe_allow_html=True,
    )


def _render_landing_help() -> None:
    st.markdown("---")
    c1, c2, c3 = st.columns(3)
    with c1:
        st.markdown("### 📤 Upload")
        st.markdown("Drop an HDFC or ICICI bank statement PDF in the sidebar.")
    with c2:
        st.markdown("### ⚡ Analyse")
        st.markdown(
            "The pipeline extracts, normalises, validates, and categorises "
            "every transaction."
        )
    with c3:
        st.markdown("### 📊 Invest")
        st.markdown(
            "Get a verdict (INVESTABLE / MONITOR / CAUTION) and a "
            "downloadable PDF report."
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main entrypoint
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    with st.sidebar:
        st.markdown("## 🏦 BSAA")
        st.caption("Bank Statement Analysis Agent · Sprint 1")
        st.divider()
        uploaded = st.file_uploader(
            "Upload a bank statement PDF",
            type=["pdf"],
            help="Supports HDFC and ICICI digital PDF statements.",
        )
        st.divider()
        st.markdown(
            "**Supported banks**\n- HDFC Bank\n- ICICI Bank\n\n"
            "**Pipeline stages**\n"
            "1. PDF extraction\n2. Normalisation\n3. Validation\n"
            "4. Categorisation\n5. Financial analysis\n6. Report generation"
        )

    st.title("Angel Investor Lens")
    st.caption(
        "Upload an HDFC or ICICI bank statement to get an instant "
        "financial health analysis."
    )

    if uploaded is None:
        st.info("Upload a PDF in the sidebar to begin.")
        _render_landing_help()
        return

    pdf_bytes = uploaded.read()
    file_hash = hashlib.sha1(pdf_bytes).hexdigest()

    if st.session_state.get("file_hash") != file_hash:
        with st.spinner("Analysing statement…"):
            try:
                result = run_pipeline_on_bytes(pdf_bytes, uploaded.name)
                st.session_state["result"]    = result
                st.session_state["file_hash"] = file_hash
                st.session_state.pop("pipeline_error", None)
            except Exception as exc:
                st.session_state["pipeline_error"] = str(exc)
                st.session_state.pop("result", None)

    if "pipeline_error" in st.session_state:
        st.error(f"Pipeline error: {st.session_state['pipeline_error']}")
        st.stop()

    result: DemoResult = st.session_state["result"]

    _render_verdict_banner(result)
    _render_kpi_row(result.metrics)
    _render_validation_badge(result.doc)
    st.divider()

    tab_trend, tab_txns, tab_report = st.tabs([
        "📈 Monthly Trend", "📋 Transactions", "📄 Download Report"
    ])

    with tab_trend:
        col_chart, col_pie = st.columns([3, 2])
        with col_chart:
            _render_monthly_chart(result.metrics)
        with col_pie:
            _render_category_pie(result.doc)

    with tab_txns:
        _render_transaction_table(result.doc)

    with tab_report:
        st.subheader("Angel Lens PDF Report")
        st.caption(
            "A 1-page investor-ready summary with verdict, KPIs, and health signals."
        )
        st.download_button(
            label="⬇ Download Angel Lens Report",
            data=result.report_bytes,
            file_name=f"angel_lens_{result.doc.account_id}.pdf",
            mime="application/pdf",
            type="primary",
        )


if __name__ == "__main__":
    main()
