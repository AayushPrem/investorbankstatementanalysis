"""Financial tab and financial-health alerts."""
from __future__ import annotations

import streamlit as st

from analysis.risk import Severity
from demo.components.formatting import badge, month_label
from demo.components.theme import CATEGORY_COLOURS, SEV_COLOURS
from demo.pipeline_runner import DemoResult

# ─────────────────────────────────────────────────────────────────────────────
# Tab 1: Financial overview
# ─────────────────────────────────────────────────────────────────────────────

def render_tab_financial(result: DemoResult) -> None:
    import plotly.graph_objects as go

    m = result.metrics
    months = m.monthly_stats
    if not months:
        st.info("No monthly data available.")
        return

    labels   = [month_label(ms.year_month) for ms in months]
    revenues = [float(ms.revenue) for ms in months]
    burns    = [float(ms.burn)    for ms in months]
    nets     = [float(ms.net)     for ms in months]

    col_chart, col_pie = st.columns([3, 2])

    with col_chart:
        fig = go.Figure()
        fig.add_bar(name="Revenue", x=labels, y=revenues,
                    marker_color="#22C55E", opacity=0.85)
        fig.add_bar(name="Burn", x=labels, y=burns,
                    marker_color="#F97316", opacity=0.85)
        fig.add_scatter(name="Net cash flow", x=labels, y=nets,
                        mode="lines+markers",
                        line=dict(color="#3B82F6", width=2), marker=dict(size=7))
        fig.update_layout(
            barmode="group", title="Monthly Revenue vs Burn (INR)",
            height=340, margin=dict(l=0, r=0, t=40, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_pie:
        counts: dict[str, int] = {}
        for t in result.doc.transactions:
            key = t.category.value if t.category else "OTHER"
            counts[key] = counts.get(key, 0) + 1
        labels_pie  = list(counts.keys())
        values_pie  = list(counts.values())
        colours_pie = [CATEGORY_COLOURS.get(lb, "#D1D5DB") for lb in labels_pie]
        fig2 = go.Figure(go.Pie(
            labels=labels_pie, values=values_pie, marker_colors=colours_pie,
            hole=0.45, textinfo="label+percent",
        ))
        fig2.update_layout(
            title="Transaction Categories", height=340,
            margin=dict(l=0, r=0, t=40, b=0),
            showlegend=False, paper_bgcolor="rgba(0,0,0,0)",
        )
        st.plotly_chart(fig2, use_container_width=True)

    st.divider()
    st.markdown("#### ⚠️ Financial Health Alerts")
    render_health_alerts(result)


# ─────────────────────────────────────────────────────────────────────────────
# Section: Financial health alerts (rendered inside Financial tab)
# ─────────────────────────────────────────────────────────────────────────────

def render_health_alerts(result: DemoResult) -> None:
    hr = result.health_report
    if not hr.has_alerts:
        st.success("No financial health alerts — metrics look healthy.")
        return

    counts = []
    if hr.high_count:
        counts.append(f"{hr.high_count} HIGH")
    if hr.medium_count:
        counts.append(f"{hr.medium_count} MEDIUM")
    if hr.low_count:
        counts.append(f"{hr.low_count} LOW")

    st.markdown(f"**{len(hr.alerts)} financial health alert(s):** {' · '.join(counts)}")

    for alert in hr.alerts:
        fg, bg = SEV_COLOURS.get(str(alert.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{badge(str(alert.severity), fg, bg)} &nbsp; **{alert.metric_name}** &nbsp; "
            f"<span style='color:#6B7280;font-size:0.85rem;'>{alert.metric_value}</span>",
            expanded=(alert.severity == Severity.HIGH),
        ):
            st.markdown(alert.description)
            if alert.evidence:
                with st.expander("Raw evidence", expanded=False):
                    st.json(alert.evidence)
