"""Angel / VC / Workbench mode layouts: which tabs each lens shows."""
from __future__ import annotations

import streamlit as st

from demo.components.downloads import (
    render_download_angel,
    render_download_vc,
    render_download_workbench,
)
from demo.components.summary import render_tab_summary
from demo.components.tabs_customers import (
    render_tab_customers,
    render_tab_related_parties,
    render_tab_transactions,
)
from demo.components.tabs_financial import render_health_alerts, render_tab_financial
from demo.components.tabs_risk import (
    render_tab_compliance,
    render_tab_reconciliation,
    render_tab_risk,
)
from demo.pipeline_runner import DemoResult

# ─────────────────────────────────────────────────────────────────────────────
# Per-lens analysis modes
# ─────────────────────────────────────────────────────────────────────────────
# Each mode shows only what that lens actually contains — the point is to
# demonstrate "one engine, many lenses": the same pipeline run, presented at
# the depth appropriate to that stakeholder, with only that lens's report(s)
# offered for download.

def render_angel_mode(result: DemoResult) -> None:
    """Angel Lens: the quick read — KPIs (already rendered above), a summary
    paragraph, and the top health signals, plus the 1-page PDF. Two tabs
    only — no transaction ledger, that depth belongs to VC/Workbench mode."""
    tab_summary, tab_signals = st.tabs(["📝 Summary", "🩺 Health Signals"])
    with tab_summary:
        render_tab_summary(result)
        st.divider()
        render_download_angel(result)
    with tab_signals:
        render_health_alerts(result)


def render_vc_mode(result: DemoResult) -> None:
    """VC Lens: financial health, risk flags, customer analytics, the full
    transaction ledger, and related parties — matches AnalysisResult in
    reports/vc_lens.py exactly (no compliance/reconciliation; those are
    Workbench-only)."""
    tab_summary, tab_fin, tab_risk, tab_cust, tab_txns, tab_rp, tab_dl = st.tabs([
        "📝 Summary",
        "📈 Financial",
        "🚩 Risk Flags",
        "👥 Customers",
        "📋 Transactions",
        "🔗 Related Parties",
        "📄 Downloads",
    ])
    with tab_summary:
        render_tab_summary(result)
    with tab_fin:
        render_tab_financial(result)
    with tab_risk:
        render_tab_risk(result)
    with tab_cust:
        render_tab_customers(result)
    with tab_txns:
        render_tab_transactions(result)
    with tab_rp:
        render_tab_related_parties(result)
    with tab_dl:
        render_download_vc(result)


def render_workbench_mode(result: DemoResult) -> None:
    """Workbench Lens: everything — the full analysis depth, plus compliance,
    reconciliation, and the custom-detector-aware risk report."""
    tab_summary, tab_fin, tab_risk, tab_cust, tab_compliance, tab_recon, tab_txns, tab_rp, tab_dl = st.tabs([
        "📝 Summary",
        "📈 Financial",
        "🚩 Risk Flags",
        "👥 Customers",
        "⚖️ Compliance",
        "🔍 Reconciliation",
        "📋 Transactions",
        "🔗 Related Parties",
        "📄 Downloads",
    ])
    with tab_summary:
        render_tab_summary(result)
    with tab_fin:
        render_tab_financial(result)
    with tab_risk:
        render_tab_risk(result)
    with tab_cust:
        render_tab_customers(result)
    with tab_compliance:
        render_tab_compliance(result)
    with tab_recon:
        render_tab_reconciliation(result)
    with tab_txns:
        render_tab_transactions(result)
    with tab_rp:
        render_tab_related_parties(result)
    with tab_dl:
        render_download_workbench(result)


MODE_HERO = {
    "👼 Angel": (
        "Angel Lens",
        "Upload an HDFC or ICICI bank statement — or generate a synthetic one — for "
        "the quick read an angel investor needs: a summary, KPIs, and the top health signals.",
    ),
    "💼 VC": (
        "VC Lens",
        "Upload an HDFC or ICICI bank statement — or generate a synthetic one — for "
        "financial health, risk flags, customer analytics, the full transaction ledger, "
        "and related parties.",
    ),
    "🏛️ Workbench": (
        "Investor Workbench",
        "Upload an HDFC or ICICI bank statement — or generate a synthetic one — for "
        "financial health, risk flags, customer analytics, Indian compliance, pitch-deck "
        "reconciliation, and every downloadable investor report.",
    ),
}
