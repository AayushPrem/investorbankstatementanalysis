"""BSAA — full-feature investor workbench demo (Streamlit).

Run:
    streamlit run demo/app.py

Single Company mode: upload an HDFC or ICICI bank statement PDF, or generate
a synthetic one right inside the app. Shows financial health, risk flags,
customer analytics, related parties (auto-detected), Indian compliance, and
pitch-deck reconciliation, with Angel/VC/Workbench report downloads.

Network mode: upload multiple statements, or generate a synthetic cohort, to
compare portfolio companies side by side via the Network Lens.
"""
from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from decimal import Decimal

import streamlit as st

from analysis.reconciliation import CompanyClaims
from analysis.risk import Severity
from demo.pipeline_runner import DemoResult, run_pipeline_on_bytes
from pipeline.batch import BatchInput, BatchOrchestrator
from pipeline.related_party import Affiliate, extract_counterparties
from reports.angel_lens import _fmt_amount, _fmt_growth, _fmt_runway
from reports.network_lens import (
    CompanySummary,
    NetworkLensReport,
    build_company_summary,
    build_portfolio_narrative,
)
from schema.canonical import StatementDocument

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="BSAA · Investor Workbench",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _inject_theme_css() -> None:
    st.markdown(
        """
        <style>
        .block-container { padding-top: 2rem; padding-bottom: 3rem; max-width: 1200px; }
        footer { visibility: hidden; }
        #MainMenu { visibility: hidden; }

        h1, h2, h3 { color: #1F3864; letter-spacing: -0.01em; }
        h1 { font-weight: 700; }

        section[data-testid="stSidebar"] {
            background: #F4F6FA;
            border-right: 1px solid #E2E8F0;
        }
        section[data-testid="stSidebar"] .stRadio label p { font-weight: 600; }

        .stTabs [data-baseweb="tab-list"] { gap: 4px; }
        .stTabs [data-baseweb="tab"] {
            height: 46px;
            padding: 0 18px;
            border-radius: 8px 8px 0 0;
            font-weight: 600;
        }
        .stTabs [aria-selected="true"] {
            background-color: #EEF2FB;
            color: #1F3864 !important;
        }

        .stButton > button, .stDownloadButton > button {
            border-radius: 8px;
            font-weight: 600;
        }

        [data-testid="stMetricValue"] { font-size: 1.5rem; color: #1F3864; }
        [data-testid="stMetricLabel"] { font-weight: 600; color: #6B7280; }

        .bsaa-hero {
            background: linear-gradient(135deg, #1F3864 0%, #2E4E8F 100%);
            border-radius: 14px;
            padding: 28px 32px;
            margin-bottom: 22px;
            color: white;
        }
        .bsaa-hero h1 { color: white; margin: 0; font-size: 1.9rem; font-weight: 700; }
        .bsaa-hero p { color: #C9D6EE; margin: 8px 0 0 0; font-size: 1rem; }

        .bsaa-eyebrow {
            display: inline-block; text-transform: uppercase; letter-spacing: 0.08em;
            font-size: 0.72rem; font-weight: 700; color: #93A6CC; margin-bottom: 4px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _hero(title: str, subtitle: str, eyebrow: str = "BSAA") -> None:
    st.markdown(
        f"""
        <div class="bsaa-hero">
            <span class="bsaa-eyebrow">{eyebrow}</span>
            <h1>{title}</h1>
            <p>{subtitle}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


_inject_theme_css()

# ─────────────────────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────────────────────

_VERDICT_COLOURS = {
    "INVESTABLE": ("#166534", "#DCFCE7"),
    "MONITOR":    ("#92400E", "#FEF3C7"),
    "CAUTION":    ("#991B1B", "#FEE2E2"),
}

_SEV_COLOURS = {
    "HIGH":   ("#991B1B", "#FEE2E2"),
    "MEDIUM": ("#92400E", "#FEF3C7"),
    "LOW":    ("#166534", "#DCFCE7"),
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

_ALL_FLAGS = [
    "structuring",
    "round_tripping",
    "founder_extraction",
    "customer_churn",
    "revenue_concentration",
    "related_party_leakage",
]

_PROFILE_LABELS = {
    "healthy_saas":    "Healthy SaaS",
    "burning_startup": "Burning Startup",
    "services_firm":   "Services Firm (B2G)",
    "ecommerce":       "E-Commerce",
    "restaurant":      "Restaurant / F&B",
}

_FLAG_LABELS = {
    "structuring":           "Cash structuring (just under ₹2L)",
    "round_tripping":        "Round-tripping (money out and back)",
    "founder_extraction":    "Founder over-extraction",
    "customer_churn":        "Significant customer churn",
    "revenue_concentration": "Revenue concentration (whale customer)",
    "related_party_leakage": "Related-party leakage",
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


def _badge(text: str, fg: str, bg: str, bold: bool = False) -> str:
    weight = "700" if bold else "600"
    return (
        f'<span style="background:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:4px;font-weight:{weight};font-size:0.82rem;">'
        f"{text}</span>"
    )


def _inr(v: float) -> str:
    if v >= 1e7:
        return f"₹{v/1e7:.1f}Cr"
    if v >= 1e5:
        return f"₹{v/1e5:.1f}L"
    if v >= 1e3:
        return f"₹{v/1e3:.0f}K"
    return f"₹{v:.0f}"


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — generator + upload + pipeline info
# ─────────────────────────────────────────────────────────────────────────────

def _render_sidebar_header() -> str:
    """Render the BSAA header + top-level analysis-mode switch. Returns the mode."""
    with st.sidebar:
        st.markdown(
            '<div style="font-size:1.5rem;font-weight:800;color:#1F3864;">🏦 BSAA</div>'
            '<div style="color:#6B7280;font-size:0.82rem;margin-bottom:14px;">'
            'Agentic Bank Statement Analysis</div>',
            unsafe_allow_html=True,
        )
        analysis_mode = st.radio(
            "Analysis mode",
            ["Single Company", "Network (multi-company)"],
            key="analysis_mode",
        )
        st.divider()
    return analysis_mode


def _sidebar() -> tuple[bytes | None, str, str]:
    """Render single-company sidebar controls. Returns (pdf_bytes, source_name, company_name)."""
    with st.sidebar:
        source_mode = st.radio(
            "Statement source",
            ["📤 Upload PDF", "🏗️ Generate Synthetic"],
            horizontal=True,
        )
        st.markdown("")

        pdf_bytes: bytes | None = None
        source_name: str = ""
        company_name: str = ""

        if source_mode == "📤 Upload PDF":
            uploaded = st.file_uploader(
                "Upload a bank statement PDF",
                type=["pdf"],
                help="Supports HDFC and ICICI digital PDF statements.",
            )
            company_name = st.text_input(
                "Company name (optional)", placeholder="Acme Pvt Ltd", key="upload_company"
            )
            if uploaded is not None:
                pdf_bytes = uploaded.read()
                source_name = uploaded.name

        else:
            # ── Generator ───────────────────────────────────────────────────
            st.markdown("#### Statement generator")

            col_p, col_b = st.columns(2)
            with col_p:
                profile = st.selectbox(
                    "Company profile",
                    options=list(_PROFILE_LABELS),
                    format_func=lambda k: _PROFILE_LABELS[k],
                    key="gen_profile",
                )
            with col_b:
                bank = st.selectbox("Bank", ["hdfc", "icici"],
                                    format_func=str.upper, key="gen_bank")

            n_months = st.slider("Duration (months)", 3, 24, 6, key="gen_months")

            risk_mode = st.radio(
                "Risk profile",
                ["clean", "realistic", "custom"],
                format_func=lambda m: {
                    "clean":     "Clean (no flags)",
                    "realistic": "Realistic (auto-random, matches real incidence rates)",
                    "custom":    "Custom (pick flags manually)",
                }[m],
                key="gen_risk_mode",
            )

            custom_flags: list[str] = []
            if risk_mode == "custom":
                selected_flags = st.multiselect(
                    "Inject these flags:",
                    options=_ALL_FLAGS,
                    format_func=lambda f: _FLAG_LABELS.get(f, f),
                    key="gen_flags",
                )
                custom_flags = selected_flags

            use_seed = st.checkbox("Fix random seed (reproducible)", key="gen_use_seed")
            seed_val = st.number_input("Seed", value=42, step=1, key="gen_seed") if use_seed else None

            generate_clicked = st.button("⚡ Generate & Analyse", type="primary", use_container_width=True)

            if generate_clicked:
                from tools.synthetic_gen import generate_statement_to_bytes

                with st.spinner(f"Generating {n_months}-month {_PROFILE_LABELS[profile]} statement…"):
                    pdf_bytes, gen_company, injected = generate_statement_to_bytes(
                        bank=bank,
                        profile=profile,
                        flags=custom_flags if risk_mode == "custom" else None,
                        risk_mode=risk_mode,
                        n_months=n_months,
                        seed=int(seed_val) if seed_val is not None else None,
                    )
                    st.session_state["gen_pdf_bytes"]    = pdf_bytes
                    st.session_state["gen_source_name"]  = f"{bank.upper()}_{profile}_{n_months}mo.pdf"
                    st.session_state["gen_company_name"] = gen_company
                    st.session_state["gen_injected"]     = injected
                    # Clear prior pipeline results so fresh run happens
                    st.session_state.pop("initial_cache_key", None)
                    st.session_state.pop("final_cache_key",   None)
                    st.session_state.pop("pipeline_error",    None)

                if injected:
                    st.success(f"Generated ✓  |  Injected: {', '.join(injected)}")
                else:
                    st.success("Generated ✓  |  No risk flags injected")

            if "gen_pdf_bytes" in st.session_state:
                pdf_bytes   = st.session_state["gen_pdf_bytes"]
                source_name = st.session_state.get("gen_source_name", "synthetic.pdf")
                company_name = st.session_state.get("gen_company_name", "")

                injected = st.session_state.get("gen_injected", [])
                if injected:
                    st.info(f"Active flags: {', '.join(injected)}")

                st.download_button(
                    label="⬇ Download Bank Statement PDF",
                    data=st.session_state["gen_pdf_bytes"],
                    file_name=st.session_state.get("gen_source_name", "synthetic.pdf"),
                    mime="application/pdf",
                    use_container_width=True,
                    key="dl_synthetic_pdf",
                )

        # ── Pipeline info ────────────────────────────────────────────────────
        st.divider()
        with st.expander("ℹ️ How this works", expanded=False):
            st.caption("**Banks supported:** HDFC · ICICI")
            st.caption(
                "**Pipeline:** extraction → validation → categorisation → "
                "related-party tagging → customer identity → risk analysis → "
                "customer analytics → compliance (India) → reconciliation → reports"
            )
            st.caption(
                "**Reports:** every run produces an Angel Lens PDF, VC Lens "
                "XLSX/PDF, and Workbench XLSX/PDF — all downloadable below."
            )

    return pdf_bytes, source_name, company_name


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — company claims panel (Sprint 3)
# ─────────────────────────────────────────────────────────────────────────────

def _claims_panel() -> CompanyClaims | None:
    """Render pitch-deck claims inputs in sidebar. Returns CompanyClaims or None."""
    with st.sidebar:
        st.divider()
        with st.expander("📋 Pitch Deck Claims (optional)", expanded=False):
            st.caption(
                "Enter metrics as declared in the pitch deck. The reconciliation "
                "engine will compare them against bank statement actuals."
            )
            rev_str = st.text_input(
                "Declared total revenue (₹)", placeholder="e.g. 12000000", key="claim_rev"
            )
            cust_str = st.text_input(
                "Declared customer count", placeholder="e.g. 45", key="claim_cust"
            )
            nrr_str = st.text_input(
                "Declared NRR (%)", placeholder="e.g. 110 for 110%", key="claim_nrr"
            )
            never_lost = st.checkbox("'We've never lost a customer'", key="claim_never_lost")
            headcount_str = st.text_input(
                "Declared headcount", placeholder="e.g. 22", key="claim_hc"
            )
            salary_str = st.text_input(
                "Avg monthly salary per employee (₹)", placeholder="e.g. 80000", key="claim_sal"
            )

        # Only return claims if at least one field is filled
        has_any = any([rev_str, cust_str, nrr_str, never_lost, headcount_str, salary_str])
        if not has_any:
            return None

        def _decimal_or_none(s: str) -> Decimal | None:
            try:
                return Decimal(s.replace(",", "").strip()) if s.strip() else None
            except Exception:
                return None

        def _int_or_none(s: str) -> int | None:
            try:
                return int(s.strip()) if s.strip() else None
            except Exception:
                return None

        return CompanyClaims(
            declared_revenue_total=_decimal_or_none(rev_str),
            declared_customer_count=_int_or_none(cust_str),
            declared_nrr=float(nrr_str.strip()) if nrr_str.strip() else None,
            declared_never_lost_customer=never_lost or None,
            declared_headcount=_int_or_none(headcount_str),
            declared_avg_monthly_salary=_decimal_or_none(salary_str),
        )


# ─────────────────────────────────────────────────────────────────────────────
# Smart affiliate selector — populated from auto-detected counterparties
# ─────────────────────────────────────────────────────────────────────────────

def _affiliate_selector(initial_result: DemoResult) -> list[Affiliate]:
    """Show multi-select populated from extracted counterparties.

    Returns Affiliate list for the re-run (empty = no tagging).
    """
    counterparties = extract_counterparties(initial_result.doc)
    # Keep top 40 by volume; skip Razorpay / daily sales aggregates
    skip = {"cust_razorpay_aggregated", "cust_daily_sales"}
    options_data = [
        (name, cnt, vol)
        for name, cnt, vol in counterparties[:40]
        if name.lower() not in skip and cnt >= 2
    ]

    if not options_data:
        return []

    option_labels = [
        f"{name}  ({cnt} txns · {_inr(vol)})"
        for name, cnt, vol in options_data
    ]
    label_to_name = {lbl: name for lbl, (name, _, _) in zip(option_labels, options_data)}

    with st.sidebar:
        st.divider()
        st.markdown("**🔗 Mark as affiliates**")
        st.caption("Counterparties auto-detected from narrations. Select any that are related parties.")
        selected_labels = st.multiselect(
            label=" ",
            options=option_labels,
            default=[],
            key="affiliate_multiselect",
            label_visibility="collapsed",
        )

    return [Affiliate(name=label_to_name[lbl]) for lbl in selected_labels]


# ─────────────────────────────────────────────────────────────────────────────
# Section: Verdict banner
# ─────────────────────────────────────────────────────────────────────────────

def _render_verdict_banner(result: DemoResult) -> None:
    label = result.verdict_label
    fg, bg = _VERDICT_COLOURS.get(label, ("#1A2B4A", "#F3F4F6"))
    doc    = result.doc
    n_txns = len(doc.transactions)
    n_flags = len(result.risk_report.flags)
    risk_score = result.risk_report.composite_score

    risk_badge = ""
    if n_flags:
        rf, rb = _SEV_COLOURS.get(
            "HIGH" if risk_score >= 50 else "MEDIUM" if risk_score >= 20 else "LOW",
            ("#374151", "#F3F4F6"),
        )
        risk_badge = f'&nbsp;&nbsp;{_badge(f"Risk: {risk_score:.0f}/100 · {n_flags} flag(s)", rf, rb)}'

    st.markdown(
        f"""
        <div style="background:{bg};border:1.5px solid {fg};border-radius:8px;
                    padding:14px 20px;margin-bottom:12px;">
          <span style="font-size:1.3rem;font-weight:700;color:{fg};">
            &#9679; {label}
          </span>
          <span style="color:#6B7280;margin-left:18px;font-size:0.9rem;">
            {(doc.bank_name or '').upper()} &nbsp;|&nbsp; Account {doc.account_id}
            &nbsp;|&nbsp; {_period_str(doc)} &nbsp;|&nbsp; {n_txns} transactions
          </span>
          {risk_badge}
        </div>
        """,
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Section: KPI row
# ─────────────────────────────────────────────────────────────────────────────

def _render_kpi_row(result: DemoResult) -> None:
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


# ─────────────────────────────────────────────────────────────────────────────
# Tab 1: Financial overview
# ─────────────────────────────────────────────────────────────────────────────

def _render_tab_financial(result: DemoResult) -> None:
    import plotly.graph_objects as go

    m = result.metrics
    months = m.monthly_stats
    if not months:
        st.info("No monthly data available.")
        return

    labels   = [_month_label(ms.year_month) for ms in months]
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
        colours_pie = [_CATEGORY_COLOURS.get(lb, "#D1D5DB") for lb in labels_pie]
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
    _render_health_alerts(result)


# ─────────────────────────────────────────────────────────────────────────────
# Section: Financial health alerts (rendered inside Financial tab)
# ─────────────────────────────────────────────────────────────────────────────

def _render_health_alerts(result: DemoResult) -> None:
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
        fg, bg = _SEV_COLOURS.get(str(alert.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{_badge(str(alert.severity), fg, bg)} &nbsp; **{alert.metric_name}** &nbsp; "
            f"<span style='color:#6B7280;font-size:0.85rem;'>{alert.metric_value}</span>",
            expanded=(alert.severity == Severity.HIGH),
        ):
            st.markdown(alert.description)
            if alert.evidence:
                with st.expander("Raw evidence", expanded=False):
                    st.json(alert.evidence)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 2: Risk flags
# ─────────────────────────────────────────────────────────────────────────────

def _render_tab_risk(result: DemoResult) -> None:
    r = result.risk_report

    score    = r.composite_score
    score_fg = "#991B1B" if score >= 50 else "#92400E" if score >= 20 else "#166534"
    score_bg = "#FEE2E2" if score >= 50 else "#FEF3C7" if score >= 20 else "#DCFCE7"
    st.markdown(
        f'<div style="display:inline-block;background:{score_bg};border:2px solid {score_fg};'
        f'border-radius:8px;padding:12px 24px;margin-bottom:16px;">'
        f'<span style="font-size:2rem;font-weight:700;color:{score_fg};">{score:.0f}</span>'
        f'<span style="color:#6B7280;font-size:1rem;"> / 100 composite risk score</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not r.flags:
        st.success("No risk flags detected — statement appears clean.")
        return

    st.markdown(f"**{len(r.flags)} flag(s) detected:**")
    for flag in r.flags:
        fg, bg = _SEV_COLOURS.get(str(flag.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{_badge(str(flag.severity), fg, bg)} &nbsp; **{flag.detector_name}**",
            expanded=(flag.severity == Severity.HIGH),
        ):
            st.markdown(f"**Description:** {flag.description}")
            if flag.triggering_transaction_ids:
                ids = flag.triggering_transaction_ids[:10]
                st.markdown(
                    f"**Triggering transactions ({len(flag.triggering_transaction_ids)}):** "
                    + ", ".join(f"`{i}`" for i in ids)
                    + ("…" if len(flag.triggering_transaction_ids) > 10 else "")
                )
            if flag.evidence:
                st.json(flag.evidence)

    if r.narrative:
        st.divider()
        st.markdown("**Analyst Narrative**")
        st.info(r.narrative)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 3: Customer analytics
# ─────────────────────────────────────────────────────────────────────────────

def _render_tab_customers(result: DemoResult) -> None:
    import pandas as pd
    import plotly.graph_objects as go

    ca = result.customer_analytics

    if ca.monthly_active_customers:
        months  = sorted(ca.monthly_active_customers)
        counts  = [ca.monthly_active_customers[m] for m in months]
        labels  = [_month_label(m) for m in months]

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
                nrr_labels = [_month_label(m) for m in nrr_months]
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
        conc_months  = [_month_label(p.month) for p in ca.concentration_trajectory]
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
            y_labels.append(_month_label(c))

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

def _render_tab_transactions(result: DemoResult) -> None:
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

def _render_tab_related_parties(result: DemoResult) -> None:
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
            {"Entity": name, "Transactions": cnt, "Total Volume": _inr(vol)}
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


# ─────────────────────────────────────────────────────────────────────────────
# Tab 6: Compliance (Sprint 3)
# ─────────────────────────────────────────────────────────────────────────────

def _render_tab_compliance(result: DemoResult) -> None:
    cr = result.compliance_report
    if not cr.has_exceptions:
        st.success("No compliance exceptions found — statement is clean under Indian regulations.")
        return

    counts = []
    if cr.high_count:
        counts.append(f"{cr.high_count} HIGH")
    med = cr.by_severity.get("MEDIUM", 0)
    low = cr.by_severity.get("LOW", 0)
    if med:
        counts.append(f"{med} MEDIUM")
    if low:
        counts.append(f"{low} LOW")

    st.markdown(f"**{len(cr.exceptions)} compliance exception(s) — Jurisdiction: {cr.jurisdiction}** · {' · '.join(counts)}")

    for exc in cr.exceptions:
        fg, bg = _SEV_COLOURS.get(str(exc.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{_badge(str(exc.severity), fg, bg)} &nbsp; **{exc.rule_name}**",
            expanded=(str(exc.severity) == "HIGH"),
        ):
            st.markdown(f"**Citation:** `{exc.regulatory_citation}`")
            st.markdown(f"**Finding:** {exc.description}")
            st.markdown(f"**Investor impact:** {exc.investor_risk_framing}")
            if exc.triggering_transaction_ids:
                n = len(exc.triggering_transaction_ids)
                ids = exc.triggering_transaction_ids[:8]
                st.markdown(
                    f"**Triggering transactions ({n}):** "
                    + ", ".join(f"`{i}`" for i in ids)
                    + ("…" if n > 8 else "")
                )
            if exc.evidence:
                with st.expander("Evidence", expanded=False):
                    st.json(exc.evidence)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 7: Reconciliation (Sprint 3)
# ─────────────────────────────────────────────────────────────────────────────

def _render_tab_reconciliation(result: DemoResult) -> None:
    rr = result.reconciliation_report
    if not rr.claims_provided:
        st.info(
            "No company claims provided. Expand the **Pitch Deck Claims** panel in the sidebar "
            "to enter declared metrics — revenue, customers, NRR, headcount, etc. — "
            "and the engine will compare them against bank statement actuals."
        )
        return

    if not rr.findings:
        st.success(
            "All claims reconcile within tolerance — no material mismatches detected "
            "between pitch-deck metrics and bank statement actuals."
        )
        return

    st.markdown(
        f"**{len(rr.findings)} mismatch(es) found** "
        f"({rr.high_count} HIGH)"
    )

    for f in rr.findings:
        fg, bg = _SEV_COLOURS.get(f.severity, ("#374151", "#F3F4F6"))
        delta_str = f"{f.delta_pct:+.0f}%" if f.delta_pct is not None else ""
        with st.expander(
            f"{_badge(f.severity, fg, bg)} &nbsp; **{f.check_name}** "
            f"<span style='color:#6B7280;font-size:0.85rem;'>claimed {f.claimed_value} · "
            f"actual {f.actual_value} {delta_str}</span>",
            expanded=(f.severity == "HIGH"),
        ):
            st.markdown(f"**Direction:** {str(f.direction).replace('_', ' ').title()}")
            st.markdown(f"**Finding:** {f.description}")
            st.markdown(f"**Investor impact:** {f.investor_framing}")


# ─────────────────────────────────────────────────────────────────────────────
# Tab 8: Downloads
# ─────────────────────────────────────────────────────────────────────────────

def _render_tab_downloads(result: DemoResult) -> None:
    acc = result.doc.account_id

    st.markdown("### Angel Lens — 1-page Investor PDF")
    st.caption("Concise verdict + KPIs + health signals, formatted for angel investors.")
    st.download_button(
        label="⬇ Download Angel Lens PDF",
        data=result.angel_report_bytes,
        file_name=f"angel_lens_{acc}.pdf",
        mime="application/pdf",
        type="primary",
        key="dl_angel",
    )

    st.divider()
    st.markdown("### VC Lens — 6-sheet XLSX Workbook")
    st.caption(
        "Full workbook: Summary · Financial Health · Red Flags · "
        "Customer Analytics · All Transactions · Related Parties"
    )
    st.download_button(
        label="⬇ Download VC Lens XLSX",
        data=result.vc_xlsx_bytes,
        file_name=f"vc_lens_{acc}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key="dl_xlsx",
    )

    st.divider()
    st.markdown("### VC Lens — 3-page PDF Summary")
    st.caption("Three-page PDF: Financial · Customer Analytics · Risk & Due Diligence.")
    st.download_button(
        label="⬇ Download VC Lens PDF",
        data=result.vc_pdf_bytes,
        file_name=f"vc_lens_{acc}.pdf",
        mime="application/pdf",
        key="dl_vc_pdf",
    )

    st.divider()
    st.markdown("### Workbench Lens — 10-sheet XLSX")
    st.caption(
        "Deepest institutional workbook: Executive Summary · Financial Health · Risk Flags · "
        "Customer Analytics · Compliance Findings · Reconciliation · All Transactions · "
        "Related Parties · Customer Master · Raw Export"
    )
    st.download_button(
        label="⬇ Download Workbench XLSX",
        data=result.workbench_xlsx_bytes,
        file_name=f"workbench_{acc}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        key="dl_wb_xlsx",
    )

    st.divider()
    st.markdown("### Workbench Lens — 4-page PDF Summary")
    st.caption("Four-page PDF: Financial, Compliance, Reconciliation, Customer Analytics.")
    st.download_button(
        label="⬇ Download Workbench PDF",
        data=result.workbench_pdf_bytes,
        file_name=f"workbench_{acc}.pdf",
        mime="application/pdf",
        key="dl_wb_pdf",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Landing page (shown when no statement is loaded)
# ─────────────────────────────────────────────────────────────────────────────

_LENS_CARDS = [
    ("👼", "Angel Lens", "1-page PDF", "Verdict, KPIs, runway gauge, and the top health "
     "signals — the quick read for an individual angel investor."),
    ("💼", "VC Lens", "6-sheet XLSX + 3-page PDF", "Financial health, risk flags, customer "
     "analytics, full transaction ledger, and related parties, with a composite risk score."),
    ("🏛️", "Workbench Lens", "10-sheet XLSX + 4-page PDF", "The deepest view: everything in "
     "VC Lens plus compliance, reconciliation, customer master, raw data export, and "
     "fund-specific custom detectors."),
    ("🌐", "Network Lens", "Comparison XLSX + PDF", "Compare many portfolio companies side "
     "by side — sortable table, the actual risk flags and compliance issues behind each "
     "company's numbers, and an LLM-generated portfolio narrative for the investment committee."),
]


def _render_landing() -> None:
    st.info(
        "👈 **Get started in the sidebar** — upload a bank statement PDF, or generate a "
        "synthetic one right here in the app. Every input path works for every report below."
    )

    st.markdown("#### One pipeline, four investor-ready reports")
    st.caption(
        "Analyse a statement once — every lens below is generated from the same run, and "
        "every lens supports **both** uploading a real statement and generating a synthetic "
        "one (Network Lens included, via its own mode in the sidebar)."
    )

    cols = st.columns(4)
    for col, (icon, name, fmt, desc) in zip(cols, _LENS_CARDS):
        with col, st.container(border=True):
            st.markdown(f"### {icon} {name}")
            st.caption(fmt)
            st.markdown(desc)

    st.markdown("")
    st.markdown("#### How it works")
    step_cols = st.columns(4)
    steps = [
        ("1 · Get a statement", "Upload an HDFC/ICICI PDF, or generate a realistic synthetic "
         "one — 5 company profiles, 3–24 months, injectable risk flags."),
        ("2 · Analyse", "Extraction → validation → categorisation → related-party tagging → "
         "customer identity → risk → customer analytics → compliance → reconciliation."),
        ("3 · Review", "Verdict banner, KPIs, six automated red-flag detectors, Indian "
         "regulatory compliance checks, and pitch-deck reconciliation."),
        ("4 · Export", "Download the Angel, VC, and Workbench reports for this company — or "
         "switch to Network mode to compare a whole cohort at once."),
    ]
    for col, (title, desc) in zip(step_cols, steps):
        with col:
            st.markdown(f"**{title}**")
            st.caption(desc)


# ─────────────────────────────────────────────────────────────────────────────
# Network mode — multi-company batch analysis (Sprint 4)
# ─────────────────────────────────────────────────────────────────────────────

def _generate_synthetic_cohort(
    count: int, risk_mode: str, n_months: int, seed_base: int | None,
) -> list[tuple[str, bytes]]:
    """Generate *count* synthetic statements cycling through the 5 company
    profiles and both banks, for the Network Lens "generate cohort" path.
    """
    from tools.synthetic_gen import generate_statement_to_bytes

    profiles = list(_PROFILE_LABELS)
    banks = ["hdfc", "icici"]
    files: list[tuple[str, bytes]] = []
    for i in range(count):
        profile = profiles[i % len(profiles)]
        bank = banks[i % len(banks)]
        seed = (seed_base + i) if seed_base is not None else None
        pdf_bytes, company_name, _injected = generate_statement_to_bytes(
            bank=bank, profile=profile, flags=None, risk_mode=risk_mode,
            n_months=n_months, seed=seed,
        )
        safe_name = (company_name or f"company_{i + 1}").replace(" ", "_").replace("/", "-")
        files.append((f"{safe_name}.pdf", pdf_bytes))
    return files


def _run_network_batch(files: list[tuple[str, bytes]], concurrency: int) -> None:
    """Write statement files to disk, run the batch pipeline, build the
    network lens, and stash everything needed to render it in st.session_state.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        inputs: list[BatchInput] = []
        for name, content in files:
            file_path = tmp_path / name
            file_path.write_bytes(content)
            inputs.append(BatchInput(path=str(file_path), company_name=Path(name).stem))

        progress_bar = st.progress(0.0, text="Starting batch…")
        done = {"count": 0}

        def _progress(path: str, outcome: object) -> None:
            done["count"] += 1
            progress_bar.progress(
                done["count"] / len(inputs), text=f"Processed {done['count']}/{len(inputs)}"
            )

        batch_result = BatchOrchestrator(concurrency=concurrency).run_batch(
            inputs, progress_callback=_progress,
        )
        progress_bar.empty()

        summaries: list[CompanySummary] = []
        failures: dict[str, str] = {}
        for item in inputs:
            outcome = batch_result.results.get(item.path)
            if isinstance(outcome, Exception):
                failures[item.resolved_company_name()] = str(outcome)
                continue
            summaries.append(build_company_summary(
                company_name=item.resolved_company_name(),
                doc=outcome.doc, metrics=outcome.metrics,
                risk_report=outcome.risk_report,
                customer_analytics=outcome.customer_analytics,
                compliance_report=outcome.compliance_report,
            ))

        narrative = build_portfolio_narrative(summaries)
        xlsx_bytes, pdf_bytes = NetworkLensReport().generate(summaries, narrative=narrative)

        st.session_state["network_batch_summary"] = batch_result.summary
        st.session_state["network_summaries"] = summaries
        st.session_state["network_failures"] = failures
        st.session_state["network_narrative"] = narrative
        st.session_state["network_xlsx_bytes"] = xlsx_bytes
        st.session_state["network_pdf_bytes"] = pdf_bytes


def _render_network_comparison(summaries: list[CompanySummary]) -> None:
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


def _render_network_detail(summaries: list[CompanySummary]) -> None:
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
                fg, bg = _SEV_COLOURS.get(f.severity, ("#374151", "#F3F4F6"))
                st.markdown(
                    f"{_badge(f.severity, fg, bg)} &nbsp; **{f.detector_name}** — {f.description}",
                    unsafe_allow_html=True,
                )
            for e in s.compliance_exceptions:
                fg, bg = _SEV_COLOURS.get(e.severity, ("#374151", "#F3F4F6"))
                st.markdown(
                    f"{_badge(e.severity, fg, bg)} &nbsp; **{e.rule_name}** "
                    f"<span style='color:#6B7280;font-size:0.85rem;'>({e.regulatory_citation})</span> "
                    f"— {e.description}",
                    unsafe_allow_html=True,
                )


def _render_network_source() -> list[tuple[str, bytes]]:
    """Render the Network Lens input controls (upload or generate). Returns
    the list of (filename, pdf_bytes) statements ready for batch analysis.
    """
    source_mode = st.radio(
        "Statement source",
        ["📤 Upload PDFs", "🏗️ Generate Synthetic Cohort"],
        horizontal=True,
        key="net_source_mode",
    )

    if source_mode == "📤 Upload PDFs":
        uploaded_files = st.file_uploader(
            "Upload bank statement PDFs (one per company)",
            type=["pdf"],
            accept_multiple_files=True,
            help="Supports HDFC and ICICI digital PDF statements.",
        )
        return [(f.name, f.read()) for f in uploaded_files] if uploaded_files else []

    with st.container(border=True):
        st.markdown("##### Cohort generator")
        st.caption(
            "Generates a mix of synthetic companies across all 5 profiles and both banks — "
            "no real statements needed to try the Network Lens."
        )
        col1, col2, col3 = st.columns(3)
        with col1:
            count = st.slider("Number of companies", 2, 10, 4, key="net_gen_count")
        with col2:
            n_months = st.slider("Duration (months)", 3, 24, 6, key="net_gen_months")
        with col3:
            risk_mode = st.selectbox(
                "Risk profile", ["realistic", "clean"],
                format_func=lambda m: {
                    "realistic": "Realistic (mixed flags)", "clean": "Clean (no flags)",
                }[m],
                key="net_gen_risk",
            )
        use_seed = st.checkbox("Fix random seed (reproducible)", key="net_gen_use_seed")
        seed_val = (
            st.number_input("Seed", value=42, step=1, key="net_gen_seed") if use_seed else None
        )

        if st.button("🏗️ Generate Cohort", type="primary", use_container_width=True):
            with st.spinner(f"Generating {count} synthetic statements…"):
                st.session_state["net_gen_files"] = _generate_synthetic_cohort(
                    count, risk_mode, n_months, int(seed_val) if seed_val is not None else None,
                )
            st.session_state.pop("network_batch_summary", None)

    files = st.session_state.get("net_gen_files", [])
    if files:
        st.success(f"Generated {len(files)} statement(s): " + ", ".join(n for n, _ in files))
    return files


def _render_network_mode() -> None:
    _hero(
        "Network Lens",
        "Compare portfolio companies side by side: burn, runway, risk score, compliance "
        "status, and more — from uploaded statements or a generated synthetic cohort.",
    )

    files = _render_network_source()

    if len(files) < 2:
        st.info("Add at least two statements — upload PDFs or generate a synthetic cohort — "
                 "to build a comparison workbook.")
        return

    concurrency = st.slider("Concurrency", 1, 8, 4, key="net_concurrency")
    run_clicked = st.button("⚡ Run Batch Analysis", type="primary", use_container_width=True)

    if run_clicked:
        with st.spinner(f"Analysing {len(files)} statements…"):
            _run_network_batch(files, concurrency)

    if "network_batch_summary" not in st.session_state:
        return

    summary = st.session_state["network_batch_summary"]
    summaries: list[CompanySummary] = st.session_state["network_summaries"]
    failures: dict[str, str] = st.session_state["network_failures"]

    st.markdown(f"**{summary.succeeded}/{summary.total} statements analysed successfully**")

    if failures:
        with st.expander(f"⚠️ {len(failures)} statement(s) failed", expanded=False):
            for name, err in failures.items():
                st.error(f"**{name}**: {err}")

    if not summaries:
        st.warning("No statements succeeded — nothing to compare.")
        return

    narrative = st.session_state.get("network_narrative", "")
    if narrative:
        st.markdown("##### 🧭 Portfolio Narrative")
        st.info(narrative)

    tab_compare, tab_detail, tab_dl = st.tabs([
        "📊 Comparison", "🚩 Risk & Compliance Detail", "📄 Downloads",
    ])

    with tab_compare:
        _render_network_comparison(summaries)

    with tab_detail:
        _render_network_detail(summaries)

    with tab_dl:
        st.markdown("### Network Lens — Comparison Workbook")
        st.caption(
            "5 sheets: Comparison (sortable/filterable) · Risk & Compliance Detail "
            "(with portfolio narrative) · Risk Distribution · Sector Breakdown · Company Reports."
        )
        st.download_button(
            "⬇ Download Network XLSX", data=st.session_state["network_xlsx_bytes"],
            file_name="network_lens.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            type="primary", use_container_width=True, key="dl_network_xlsx",
        )
        st.divider()
        st.markdown("### Cohort Dashboard PDF")
        st.caption(
            "Portfolio narrative, median/P10/P90 stats, high-risk companies, burn outliers, "
            "and full risk & compliance detail per company."
        )
        st.download_button(
            "⬇ Download Cohort Dashboard PDF", data=st.session_state["network_pdf_bytes"],
            file_name="network_dashboard.pdf", mime="application/pdf",
            use_container_width=True, key="dl_network_pdf",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    analysis_mode = _render_sidebar_header()
    if analysis_mode == "Network (multi-company)":
        _render_network_mode()
        return

    pdf_bytes, source_name, company_name = _sidebar()

    _hero(
        "Investor Workbench",
        "Upload an HDFC or ICICI bank statement — or generate a synthetic one — for "
        "financial health, risk flags, customer analytics, Indian compliance, pitch-deck "
        "reconciliation, and four downloadable investor reports.",
    )

    if pdf_bytes is None:
        _render_landing()
        return

    file_hash = hashlib.sha1(pdf_bytes).hexdigest()

    # ── Step 1: initial pipeline run (no affiliates) to populate counterparty list
    initial_key = file_hash + "_initial"
    if st.session_state.get("initial_cache_key") != initial_key:
        with st.spinner("Running pipeline (pass 1 — counterparty discovery)…"):
            try:
                initial_result = run_pipeline_on_bytes(
                    pdf_bytes, source_name,
                    affiliates=None,
                    company_name=company_name or None,
                )
                st.session_state["initial_result"]    = initial_result
                st.session_state["initial_cache_key"] = initial_key
                st.session_state.pop("pipeline_error", None)
            except Exception as exc:
                st.session_state["pipeline_error"] = str(exc)
                st.session_state.pop("initial_result", None)

    if "pipeline_error" in st.session_state:
        st.error(f"Pipeline error: {st.session_state['pipeline_error']}")
        st.stop()

    initial_result: DemoResult = st.session_state["initial_result"]

    # ── Step 2: affiliate selector + claims panel (in sidebar)
    affiliates = _affiliate_selector(initial_result)
    claims = _claims_panel()
    affiliate_key = str(sorted(a.name for a in affiliates)) + str(company_name)
    claims_key = str(vars(claims)) if claims else "noclaims"

    # ── Step 3: final pipeline run (with any selected affiliates + claims)
    final_key = file_hash + affiliate_key + claims_key
    if st.session_state.get("final_cache_key") != final_key:
        if affiliates or claims:
            with st.spinner(f"Re-running pipeline with {len(affiliates)} affiliate(s)…"):
                try:
                    result = run_pipeline_on_bytes(
                        pdf_bytes, source_name,
                        affiliates=affiliates,
                        company_name=company_name or None,
                        claims=claims,
                    )
                    st.session_state["result"]         = result
                    st.session_state["final_cache_key"] = final_key
                except Exception as exc:
                    st.session_state["pipeline_error"] = str(exc)
        else:
            st.session_state["result"]          = initial_result
            st.session_state["final_cache_key"] = final_key

    result: DemoResult = st.session_state.get("result", initial_result)

    # ── Render analysis UI ───────────────────────────────────────────────────
    _render_verdict_banner(result)
    _render_kpi_row(result)

    status  = result.doc.validation_status
    v_colours = {
        "passed":      ("#166534", "#DCFCE7"),
        "failed":      ("#991B1B", "#FEE2E2"),
        "unvalidated": ("#1E40AF", "#DBEAFE"),
    }
    vfg, vbg = v_colours.get(str(status).lower(), ("#374151", "#F3F4F6"))
    n_rp = sum(1 for t in result.doc.transactions if t.is_related_party)
    compliance_high = result.compliance_report.high_count
    recon_high = result.reconciliation_report.high_count
    st.markdown(
        '<div style="margin-bottom:8px;">'
        + _badge(f"Validation: {str(status).upper()}", vfg, vbg)
        + '&nbsp;&nbsp;'
        + _badge(f"{len(result.doc.transactions)} transactions", "#1E40AF", "#DBEAFE")
        + '&nbsp;&nbsp;'
        + _badge(
            f"{len(result.customer_analytics.monthly_active_customers)} months tracked",
            "#6B21A8", "#F3E8FF",
          )
        + ('&nbsp;&nbsp;' + _badge(f"{n_rp} related-party txns", "#92400E", "#FEF3C7")
           if n_rp else "")
        + ('&nbsp;&nbsp;' + _badge(f"{compliance_high} compliance HIGH", "#991B1B", "#FEE2E2")
           if compliance_high else "")
        + ('&nbsp;&nbsp;' + _badge(f"{recon_high} recon HIGH", "#991B1B", "#FEE2E2")
           if recon_high else "")
        + "</div>",
        unsafe_allow_html=True,
    )
    st.divider()

    tab_fin, tab_risk, tab_cust, tab_compliance, tab_recon, tab_txns, tab_rp, tab_dl = st.tabs([
        "📈 Financial",
        "🚩 Risk Flags",
        "👥 Customers",
        "⚖️ Compliance",
        "🔍 Reconciliation",
        "📋 Transactions",
        "🔗 Related Parties",
        "📄 Downloads",
    ])

    with tab_fin:
        _render_tab_financial(result)

    with tab_risk:
        _render_tab_risk(result)

    with tab_cust:
        _render_tab_customers(result)

    with tab_compliance:
        _render_tab_compliance(result)

    with tab_recon:
        _render_tab_reconciliation(result)

    with tab_txns:
        _render_tab_transactions(result)

    with tab_rp:
        _render_tab_related_parties(result)

    with tab_dl:
        _render_tab_downloads(result)


if __name__ == "__main__":
    main()
