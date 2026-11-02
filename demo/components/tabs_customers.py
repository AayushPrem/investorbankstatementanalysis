"""Customer-analytics, transaction-ledger, and related-party tabs."""
from __future__ import annotations

import streamlit as st

from demo.components.formatting import inr, month_label
from demo.pipeline_runner import DemoResult
from pipeline.related_party import extract_counterparties
from reports.angel_lens import _fmt_amount

# ─────────────────────────────────────────────────────────────────────────────
# Tab 3: Customer analytics
# ─────────────────────────────────────────────────────────────────────────────

def render_tab_customers(result: DemoResult) -> None:
    import pandas as pd
    import plotly.graph_objects as go

    ca = result.customer_analytics

    if ca.monthly_active_customers:
        months  = sorted(ca.monthly_active_customers)
        counts  = [ca.monthly_active_customers[m] for m in months]
        labels  = [month_label(m) for m in months]

        col_active, col_nrr = st.columns(2)

        with col_active:
            fig = go.Figure()
            fig.add_bar(x=labels, y=counts, marker_color="#3B82F6", name="Active customers")
            trend = ca.monthly_active_trend
            trend_str = f"{trend:+.1f}/mo" if trend else ""
            fig.update_layout(
                title=f"Active Customers per Month (trend: {trend_str})",
                height=300, margin=dict(l=0, r=0, t=40, b=0),
                plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            )
            st.plotly_chart(fig, use_container_width=True)

        with col_nrr:
            if ca.nrr_per_month:
                nrr_months = sorted(ca.nrr_per_month)
                nrr_vals   = [ca.nrr_per_month[m] for m in nrr_months]
                nrr_labels = [month_label(m) for m in nrr_months]
                colours    = ["#22C55E" if v >= 1.0 else "#F97316" for v in nrr_vals]

                fig2 = go.Figure()
                fig2.add_bar(x=nrr_labels, y=[v * 100 for v in nrr_vals],
                             marker_color=colours, name="NRR %")
                fig2.add_hline(y=100, line_dash="dash", line_color="#6B7280",
                               annotation_text="100% baseline")
                fig2.update_layout(
                    title="Net Revenue Retention (NRR %)",
                    yaxis_title="NRR (%)",
                    height=300, margin=dict(l=0, r=0, t=40, b=0),
                    plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
                )
                st.plotly_chart(fig2, use_container_width=True)
            else:
                st.info("NRR requires ≥ 2 months of customer data.")

    if ca.concentration_trajectory:
        st.markdown("#### Revenue Concentration")
        conc_months  = [month_label(p.month) for p in ca.concentration_trajectory]
        top3_shares  = [p.top3_share * 100 for p in ca.concentration_trajectory]
        top10_shares = [p.top10_share * 100 for p in ca.concentration_trajectory]

        fig3 = go.Figure()
        fig3.add_scatter(x=conc_months, y=top3_shares, name="Top-3 share",
                         mode="lines+markers", line=dict(color="#F97316", width=2))
        fig3.add_scatter(x=conc_months, y=top10_shares, name="Top-10 share",
                         mode="lines+markers", line=dict(color="#3B82F6", width=2))
        fig3.add_hline(y=80, line_dash="dash", line_color="#EAB308",
                       annotation_text="80% concentration warning")
        fig3.update_layout(
            title="Revenue Concentration Trajectory (%)",
            yaxis=dict(range=[0, 105]), height=280,
            margin=dict(l=0, r=0, t=40, b=0),
            plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)",
            legend=dict(orientation="h", yanchor="bottom", y=1.02),
        )
        st.plotly_chart(fig3, use_container_width=True)

    st.markdown("#### Churn Events")
    if ca.churn_events:
        churn_rows = [
            {
                "Customer ID":          e.customer_id,
                "Last Payment":         str(e.last_payment_date),
                "Avg Monthly Spend":    _fmt_amount(e.previous_avg_monthly_spend),
                "Months Active":        e.months_active_before_churn,
            }
            for e in ca.churn_events
        ]
        st.dataframe(pd.DataFrame(churn_rows), use_container_width=True, hide_index=True)
    else:
        st.success("No churn events detected.")

    if ca.payment_regularity_alerts:
        st.markdown("#### Payment Regularity Alerts")
        alert_rows = [
            {
                "Customer ID":         a.customer_id,
                "Expected Gap (days)": a.expected_gap_days,
                "Actual Gap (days)":   a.actual_gap_days,
                "Last Payment":        str(a.last_payment_date),
            }
            for a in ca.payment_regularity_alerts
        ]
        st.dataframe(pd.DataFrame(alert_rows), use_container_width=True, hide_index=True)

    if ca.cohort_retention:
        st.markdown("#### Cohort Retention Matrix")

        cohorts  = sorted(ca.cohort_retention)
        max_off  = max(max(v) for v in ca.cohort_retention.values())
        offsets  = list(range(max_off + 1))
        z_matrix = []
        y_labels = []
        for c in cohorts:
            row = [ca.cohort_retention[c].get(n) for n in offsets]
            z_matrix.append([(v * 100 if v is not None else None) for v in row])
            y_labels.append(month_label(c))

        import plotly.graph_objects as go
        fig4 = go.Figure(go.Heatmap(
            z=z_matrix,
            x=[f"+{n}mo" for n in offsets],
            y=y_labels,
            colorscale="RdYlGn",
            zmin=0, zmax=100,
            text=[[f"{v:.0f}%" if v is not None else "" for v in row] for row in z_matrix],
            texttemplate="%{text}",
        ))
        fig4.update_layout(
            title="Cohort Retention (%)",
            height=max(220, 60 * len(cohorts) + 60),
            margin=dict(l=0, r=0, t=40, b=0),
        )
        st.plotly_chart(fig4, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 4: Transactions
# ─────────────────────────────────────────────────────────────────────────────

def render_tab_transactions(result: DemoResult) -> None:
    import pandas as pd

    doc = result.doc
    if not doc.transactions:
        st.info("No transactions extracted.")
        return

    rows = [
        {
            "Date":          str(t.date),
            "Description":   t.description[:60] + ("…" if len(t.description) > 60 else ""),
            "Debit":         float(t.debit)  if t.debit  else None,
            "Credit":        float(t.credit) if t.credit else None,
            "Balance":       float(t.balance),
            "Category":      t.category.value if t.category else "",
            "Customer ID":   t.customer_id or "",
            "Related Party": "✓" if t.is_related_party else "",
        }
        for t in doc.transactions
    ]
    df = pd.DataFrame(rows)

    col_f1, col_f2 = st.columns([2, 1])
    with col_f1:
        categories = sorted(df["Category"].unique().tolist())
        selected = st.multiselect("Filter by category", categories, default=categories, key="cat_filter")
    with col_f2:
        rp_only = st.checkbox("Related parties only", key="rp_filter")

    filtered = df[df["Category"].isin(selected)] if selected else df
    if rp_only:
        filtered = filtered[filtered["Related Party"] == "✓"]

    st.dataframe(
        filtered, use_container_width=True, height=420,
        column_config={
            "Debit":   st.column_config.NumberColumn("Debit (INR)",   format="₹%.0f"),
            "Credit":  st.column_config.NumberColumn("Credit (INR)",  format="₹%.0f"),
            "Balance": st.column_config.NumberColumn("Balance (INR)", format="₹%.0f"),
        },
        hide_index=True,
    )
    st.caption(f"{len(filtered):,} of {len(df):,} transactions shown")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 5: Related parties
# ─────────────────────────────────────────────────────────────────────────────

def render_tab_related_parties(result: DemoResult) -> None:
    import pandas as pd

    # Top counterparties detected (always shown regardless of affiliate tagging)
    counterparties = extract_counterparties(result.doc)
    skip = {"cust_razorpay_aggregated", "cust_daily_sales"}
    cp_data = [(n, c, v) for n, c, v in counterparties if n.lower() not in skip and c >= 2]

    if cp_data:
        st.markdown("#### Auto-detected Counterparties")
        st.caption(
            "All unique entities extracted from transaction narrations, ranked by total volume. "
            "Use the sidebar multi-select to mark any as related parties."
        )
        cp_rows = [
            {"Entity": name, "Transactions": cnt, "Total Volume": inr(vol)}
            for name, cnt, vol in cp_data[:30]
        ]
        st.dataframe(pd.DataFrame(cp_rows), use_container_width=True, hide_index=True)

    st.divider()

    rp_txns = [t for t in result.doc.transactions if t.is_related_party]
    if not rp_txns:
        st.info(
            "No related-party transactions tagged. "
            "Select counterparties as affiliates in the sidebar above to enable tagging."
        )
        return

    st.markdown("#### Tagged Related-Party Transactions")
    rows = [
        {
            "Date":        str(t.date),
            "Description": t.description[:55],
            "Debit":       float(t.debit)  if t.debit  else None,
            "Credit":      float(t.credit) if t.credit else None,
            "Affiliate":   t.related_party_match or "",
        }
        for t in rp_txns
    ]
    df = pd.DataFrame(rows)
    st.dataframe(
        df, use_container_width=True, height=320,
        column_config={
            "Debit":  st.column_config.NumberColumn("Debit (INR)",  format="₹%.0f"),
            "Credit": st.column_config.NumberColumn("Credit (INR)", format="₹%.0f"),
        },
        hide_index=True,
    )

    st.markdown("#### Aggregated by Affiliate")
    agg = (
        df.groupby("Affiliate")
        .agg(
            Transactions=("Affiliate", "count"),
            Total_Debit=("Debit", "sum"),
            Total_Credit=("Credit", "sum"),
        )
        .reset_index()
    )
    st.dataframe(agg, use_container_width=True, hide_index=True)
