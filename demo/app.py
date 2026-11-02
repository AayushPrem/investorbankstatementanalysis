"""BSAA — full-feature investor workbench demo (Streamlit).

Run:
    streamlit run demo/app.py

Four analysis modes — one per report lens, same underlying pipeline:

Angel mode: the quick read — summary, KPIs, top health signals, 1-page PDF.
VC mode: financial health, risk flags, customer analytics, transaction
    ledger, related parties — matches the VC Lens XLSX/PDF exactly.
Workbench mode: everything above plus Indian compliance and pitch-deck
    reconciliation — the deepest view, matching the Workbench Lens.
Network mode: upload multiple statements, or generate a synthetic cohort, to
    compare portfolio companies side by side via the Network Lens.
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from config.logging import configure_logging
from demo.components.sidebar import render_sidebar_header
from demo.components.theme import inject_theme_css
from demo.views.network import render_network_mode
from demo.views.single_company import render_single_company

# JSON logs with run_id/stage (config/logging.py). Idempotent across Streamlit reruns.
configure_logging()

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="BSAA · Investor Workbench",
    page_icon="🏦",
    layout="wide",
    initial_sidebar_state="expanded",
)


inject_theme_css()


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    analysis_mode = render_sidebar_header()
    if analysis_mode == "🌐 Network":
        render_network_mode()
        return
    render_single_company(analysis_mode)


if __name__ == "__main__":
    main()
