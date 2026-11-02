"""Landing page shown when no statement is loaded."""
from __future__ import annotations

import streamlit as st

# ─────────────────────────────────────────────────────────────────────────────
# Landing page (shown when no statement is loaded)
# ─────────────────────────────────────────────────────────────────────────────

LENS_CARDS = [
    ("👼", "Angel Lens", "1-page PDF", "Summary, KPIs, runway gauge, and the top health "
     "signals — the quick read for an individual angel investor."),
    ("💼", "VC Lens", "6-sheet XLSX + 3-page PDF", "Financial health, risk flags, customer "
     "analytics, full transaction ledger, and related parties, with flag counts by severity."),
    ("🏛️", "Workbench Lens", "10-sheet XLSX + 4-page PDF", "The deepest view: everything in "
     "VC Lens plus compliance, reconciliation, customer master, raw data export, and "
     "fund-specific custom detectors."),
    ("🌐", "Network Lens", "Comparison XLSX + PDF", "Compare many portfolio companies side "
     "by side — sortable table, the actual risk flags and compliance issues behind each "
     "company's numbers, and an LLM-generated portfolio narrative for the investment committee."),
]


def render_landing() -> None:
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
    for col, (icon, name, fmt, desc) in zip(cols, LENS_CARDS):
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
        ("3 · Review", "Summary paragraph, KPIs, six automated red-flag detectors, Indian "
         "regulatory compliance checks, and pitch-deck reconciliation."),
        ("4 · Export", "Download the Angel, VC, and Workbench reports for this company — or "
         "switch to Network mode to compare a whole cohort at once."),
    ]
    for col, (title, desc) in zip(step_cols, steps):
        with col:
            st.markdown(f"**{title}**")
            st.caption(desc)
