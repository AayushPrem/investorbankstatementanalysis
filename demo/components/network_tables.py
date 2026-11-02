"""Network Lens comparison table/chart and per-company drill-down."""
from __future__ import annotations

import streamlit as st

from demo.components.formatting import badge
from demo.components.theme import SEV_COLOURS
from reports.network_lens import CompanySummary


def render_network_comparison(summaries: list[CompanySummary]) -> None:
    import pandas as pd

    rows = [{
        "Company": s.company_name,
        "Period": f"{s.statement_period_start} – {s.statement_period_end}",
        "Monthly Burn": float(s.monthly_burn),
        "Monthly Revenue": float(s.monthly_revenue),
        "Runway (mo)": s.runway_months,
        "Revenue Growth": s.revenue_growth,
        "Active Customers": s.active_customers,
        "Churn Rate": s.churn_rate,
        "NRR": s.nrr,
        "Top Customer Share": s.top_customer_share,
        "Risk Score": s.composite_risk_score,
        "Red Flags": s.red_flag_count,
        "Compliance": s.compliance_status,
    } for s in summaries]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    import plotly.graph_objects as go

    buckets = {"0-19 (Low)": 0, "20-49 (Medium)": 0, "50+ (High)": 0}
    for s in summaries:
        if s.composite_risk_score < 20:
            buckets["0-19 (Low)"] += 1
        elif s.composite_risk_score < 50:
            buckets["20-49 (Medium)"] += 1
        else:
            buckets["50+ (High)"] += 1
    fig = go.Figure(go.Bar(
        x=list(buckets.keys()), y=list(buckets.values()),
        marker_color=["#22C55E", "#F59E0B", "#EF4444"],
    ))
    fig.update_layout(title="Risk Score Distribution", height=300,
                       margin={"l": 10, "r": 10, "t": 40, "b": 10})
    st.plotly_chart(fig, use_container_width=True)


def render_network_detail(summaries: list[CompanySummary]) -> None:
    """Per-company drill-down: the actual risk flags and compliance
    exceptions behind each company's counts — not just the numbers.
    """
    with_detail = [s for s in summaries if s.risk_flags or s.compliance_exceptions]
    if not with_detail:
        st.success("No risk flags or compliance exceptions detected across the cohort.")
        return

    for s in sorted(with_detail, key=lambda s: -s.composite_risk_score):
        n_items = len(s.risk_flags) + len(s.compliance_exceptions)
        with st.expander(
            f"**{s.company_name}** — risk {s.composite_risk_score:.0f}/100 · "
            f"{s.compliance_status} · {n_items} item(s)",
            expanded=(s.composite_risk_score >= 50),
        ):
            for f in s.risk_flags:
                fg, bg = SEV_COLOURS.get(f.severity, ("#374151", "#F3F4F6"))
                st.markdown(
                    f"{badge(f.severity, fg, bg)} &nbsp; **{f.detector_name}** — {f.description}",
                    unsafe_allow_html=True,
                )
            for e in s.compliance_exceptions:
                fg, bg = SEV_COLOURS.get(e.severity, ("#374151", "#F3F4F6"))
                st.markdown(
                    f"{badge(e.severity, fg, bg)} &nbsp; **{e.rule_name}** "
                    f"<span style='color:#6B7280;font-size:0.85rem;'>({e.regulatory_citation})</span> "
                    f"— {e.description}",
                    unsafe_allow_html=True,
                )
