"""Risk-flag, compliance, and reconciliation tabs."""
from __future__ import annotations

import streamlit as st

from analysis.risk import Severity
from demo.components.formatting import badge
from demo.components.theme import SEV_COLOURS
from demo.pipeline_runner import DemoResult

# ─────────────────────────────────────────────────────────────────────────────
# Tab 2: Risk flags
# ─────────────────────────────────────────────────────────────────────────────

def render_tab_risk(result: DemoResult) -> None:
    r = result.risk_report

    high = sum(1 for f in r.flags if str(f.severity) == "HIGH")
    med  = sum(1 for f in r.flags if str(f.severity) == "MEDIUM")
    low  = sum(1 for f in r.flags if str(f.severity) == "LOW")
    st.markdown(
        f'<div style="display:inline-block;background:#F3F4F6;border:1.5px solid #1A2B4A;'
        f'border-radius:8px;padding:10px 20px;margin-bottom:16px;">'
        f'<span style="font-size:1.6rem;font-weight:700;color:#1A2B4A;">{len(r.flags)}</span>'
        f'<span style="color:#6B7280;font-size:0.9rem;"> risk flag(s) &nbsp;·&nbsp; '
        f'{high} HIGH &nbsp;/&nbsp; {med} MEDIUM &nbsp;/&nbsp; {low} LOW</span>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if not r.flags:
        st.success("No flags detected in scope across the 6 automated detectors.")
        _render_ml_flags(result)
        return

    st.markdown(f"**{len(r.flags)} flag(s) detected:**")
    for flag in r.flags:
        fg, bg = SEV_COLOURS.get(str(flag.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{badge(str(flag.severity), fg, bg)} &nbsp; **{flag.detector_name}**",
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

    _render_ml_flags(result)


def _render_ml_flags(result: DemoResult) -> None:
    """Phase 5: kind=ml flags, shown apart from the rule flags (only when present)."""
    from reports.workbench_lens import ML_CAVEAT, ML_SECTION_TITLE

    ar = getattr(result, "anomaly_report", None)
    if ar is None or not ar.flags:
        return
    st.divider()
    st.markdown(f"**{ML_SECTION_TITLE}: {len(ar.flags)} transaction(s)**")
    st.caption(ML_CAVEAT)
    for flag in ar.flags:
        fg, bg = SEV_COLOURS.get(str(flag.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{badge(str(flag.severity), fg, bg)} &nbsp; `{', '.join(flag.triggering_transaction_ids)}`",
            expanded=False,
        ):
            st.markdown(flag.description)
            st.json(flag.evidence)


# ─────────────────────────────────────────────────────────────────────────────
# Tab 6: Compliance (Sprint 3)
# ─────────────────────────────────────────────────────────────────────────────

def render_tab_compliance(result: DemoResult) -> None:
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
        fg, bg = SEV_COLOURS.get(str(exc.severity), ("#374151", "#F3F4F6"))
        with st.expander(
            f"{badge(str(exc.severity), fg, bg)} &nbsp; **{exc.rule_name}**",
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

def render_tab_reconciliation(result: DemoResult) -> None:
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
        fg, bg = SEV_COLOURS.get(f.severity, ("#374151", "#F3F4F6"))
        delta_str = f"{f.delta_pct:+.0f}%" if f.delta_pct is not None else ""
        with st.expander(
            f"{badge(f.severity, fg, bg)} &nbsp; **{f.check_name}** "
            f"<span style='color:#6B7280;font-size:0.85rem;'>claimed {f.claimed_value} · "
            f"actual {f.actual_value} {delta_str}</span>",
            expanded=(f.severity == "HIGH"),
        ):
            st.markdown(f"**Direction:** {str(f.direction).replace('_', ' ').title()}")
            st.markdown(f"**Finding:** {f.description}")
            st.markdown(f"**Investor impact:** {f.investor_framing}")
