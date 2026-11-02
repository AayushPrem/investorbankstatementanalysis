"""Single-company flow: source → two-pass cached pipeline run → lens view."""
from __future__ import annotations

import hashlib

import streamlit as st

from demo.components.sidebar import affiliate_selector, claims_panel, render_statement_source
from demo.components.summary import render_flag_summary_banner, render_kpi_row, render_status_badges
from demo.components.theme import hero
from demo.pipeline_runner import DemoResult, run_pipeline_on_bytes
from demo.views.landing import render_landing
from demo.views.lenses import MODE_HERO, render_angel_mode, render_vc_mode, render_workbench_mode


def render_single_company(analysis_mode: str) -> None:
    """Angel / VC / Workbench modes (everything except Network)."""
    pdf_bytes, source_name, company_name = render_statement_source()

    hero_title, hero_subtitle = MODE_HERO[analysis_mode]
    hero(hero_title, hero_subtitle)

    if pdf_bytes is None:
        render_landing()
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
    affiliates = affiliate_selector(initial_result)
    claims = claims_panel()
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
    render_flag_summary_banner(result)
    render_kpi_row(result)
    render_status_badges(result)
    st.divider()

    if analysis_mode == "👼 Angel":
        render_angel_mode(result)
    elif analysis_mode == "💼 VC":
        render_vc_mode(result)
    else:
        render_workbench_mode(result)
