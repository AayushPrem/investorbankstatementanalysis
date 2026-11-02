"""Global CSS, hero banner, and colour palettes."""
from __future__ import annotations

import streamlit as st


def inject_theme_css() -> None:
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


def hero(title: str, subtitle: str, eyebrow: str = "BSAA") -> None:
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


# ─────────────────────────────────────────────────────────────────────────────
# Colour palette
# ─────────────────────────────────────────────────────────────────────────────

SEV_COLOURS = {
    "HIGH":   ("#991B1B", "#FEE2E2"),
    "MEDIUM": ("#92400E", "#FEF3C7"),
    "LOW":    ("#166534", "#DCFCE7"),
}

CATEGORY_COLOURS = {
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
