"""Per-lens report download sections."""
from __future__ import annotations

import streamlit as st

from demo.pipeline_runner import DemoResult

# ─────────────────────────────────────────────────────────────────────────────
# Tab 8: Downloads
# ─────────────────────────────────────────────────────────────────────────────

def render_download_angel(result: DemoResult) -> None:
    acc = result.doc.account_id
    st.markdown("### Angel Lens — 1-page Investor PDF")
    st.caption("Concise summary + KPIs + health signals, formatted for angel investors.")
    st.download_button(
        label="⬇ Download Angel Lens PDF",
        data=result.angel_report_bytes,
        file_name=f"angel_lens_{acc}.pdf",
        mime="application/pdf",
        type="primary",
        key="dl_angel",
    )


def render_download_vc(result: DemoResult) -> None:
    acc = result.doc.account_id
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


def render_download_workbench(result: DemoResult) -> None:
    acc = result.doc.account_id
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
