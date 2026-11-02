"""Network mode: multi-company batch analysis (Sprint 4)."""
from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from demo.components.labels import PROFILE_LABELS
from demo.components.network_tables import render_network_comparison, render_network_detail
from demo.components.theme import hero
from pipeline.batch import BatchInput, BatchOrchestrator
from reports.network_lens import (
    CompanySummary,
    NetworkLensReport,
    build_company_summary,
    build_portfolio_narrative,
)

# ─────────────────────────────────────────────────────────────────────────────
# Network mode — multi-company batch analysis (Sprint 4)
# ─────────────────────────────────────────────────────────────────────────────

def generate_synthetic_cohort(
    count: int, risk_mode: str, n_months: int, seed_base: int | None,
) -> list[tuple[str, bytes]]:
    """Generate *count* synthetic statements cycling through the 5 company
    profiles and both banks, for the Network Lens "generate cohort" path.
    """
    from tools.synthetic_gen import generate_statement_to_bytes

    profiles = list(PROFILE_LABELS)
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


def run_network_batch(files: list[tuple[str, bytes]], concurrency: int) -> None:
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


def render_network_source() -> list[tuple[str, bytes]]:
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
                st.session_state["net_gen_files"] = generate_synthetic_cohort(
                    count, risk_mode, n_months, int(seed_val) if seed_val is not None else None,
                )
            st.session_state.pop("network_batch_summary", None)

    files = st.session_state.get("net_gen_files", [])
    if files:
        st.success(f"Generated {len(files)} statement(s): " + ", ".join(n for n, _ in files))
    return files


def render_network_mode() -> None:
    hero(
        "Network Lens",
        "Compare portfolio companies side by side: burn, runway, risk score, compliance "
        "status, and more — from uploaded statements or a generated synthetic cohort.",
    )

    files = render_network_source()

    if len(files) < 2:
        st.info("Add at least two statements — upload PDFs or generate a synthetic cohort — "
                 "to build a comparison workbook.")
        return

    concurrency = st.slider("Concurrency", 1, 8, 4, key="net_concurrency")
    run_clicked = st.button("⚡ Run Batch Analysis", type="primary", use_container_width=True)

    if run_clicked:
        with st.spinner(f"Analysing {len(files)} statements…"):
            run_network_batch(files, concurrency)

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
        render_network_comparison(summaries)

    with tab_detail:
        render_network_detail(summaries)

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
