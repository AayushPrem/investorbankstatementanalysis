"""Sidebar controls: mode switch, statement source, pitch-deck claims, affiliates."""
from __future__ import annotations

from decimal import Decimal

import streamlit as st

from analysis.reconciliation import CompanyClaims
from demo.components.formatting import inr
from demo.components.labels import ALL_FLAGS, FLAG_LABELS, PROFILE_LABELS
from demo.pipeline_runner import DemoResult
from pipeline.related_party import Affiliate, extract_counterparties

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — generator + upload + pipeline info
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar_header() -> str:
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
            ["👼 Angel", "💼 VC", "🏛️ Workbench", "🌐 Network"],
            key="analysis_mode",
        )
        st.divider()
    return analysis_mode


def render_statement_source() -> tuple[bytes | None, str, str]:
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
                    options=list(PROFILE_LABELS),
                    format_func=lambda k: PROFILE_LABELS[k],
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
                    options=ALL_FLAGS,
                    format_func=lambda f: FLAG_LABELS.get(f, f),
                    key="gen_flags",
                )
                custom_flags = selected_flags

            use_seed = st.checkbox("Fix random seed (reproducible)", key="gen_use_seed")
            seed_val = st.number_input("Seed", value=42, step=1, key="gen_seed") if use_seed else None

            generate_clicked = st.button("⚡ Generate & Analyse", type="primary", use_container_width=True)

            if generate_clicked:
                from tools.synthetic_gen import generate_statement_to_bytes

                with st.spinner(f"Generating {n_months}-month {PROFILE_LABELS[profile]} statement…"):
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

def claims_panel() -> CompanyClaims | None:
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

def affiliate_selector(initial_result: DemoResult) -> list[Affiliate]:
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
        f"{name}  ({cnt} txns · {inr(vol)})"
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
