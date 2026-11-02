"""Pure display helpers (strings/HTML snippets, no widgets)."""
from __future__ import annotations

from demo.components.theme import SEV_COLOURS
from schema.canonical import StatementDocument

# ─────────────────────────────────────────────────────────────────────────────
# Display helpers
# ─────────────────────────────────────────────────────────────────────────────

def period_str(doc: StatementDocument) -> str:
    s = doc.statement_period_start.strftime("%b %Y")
    e = doc.statement_period_end.strftime("%b %Y")
    return s if s == e else f"{s} – {e}"


def month_label(ym: str) -> str:
    from datetime import datetime
    return datetime.strptime(ym, "%Y-%m").strftime("%b %Y")


def badge(text: str, fg: str, bg: str, bold: bool = False) -> str:
    weight = "700" if bold else "600"
    return (
        f'<span style="background:{bg};color:{fg};padding:3px 10px;'
        f'border-radius:4px;font-weight:{weight};font-size:0.82rem;">'
        f"{text}</span>"
    )


def inr(v: float) -> str:
    if v >= 1e7:
        return f"₹{v/1e7:.1f}Cr"
    if v >= 1e5:
        return f"₹{v/1e5:.1f}L"
    if v >= 1e3:
        return f"₹{v/1e3:.0f}K"
    return f"₹{v:.0f}"


def flag_count_badge(label: str, items: list, severity_attr: str = "severity") -> str:
    from analysis.report_narrative import severity_counts

    counts = severity_counts(items, severity_attr)
    total = sum(counts.values())
    if total == 0:
        return badge(f"{label}: 0", "#166534", "#DCFCE7")
    if counts["HIGH"]:
        fg, bg = SEV_COLOURS["HIGH"]
    elif counts["MEDIUM"]:
        fg, bg = SEV_COLOURS["MEDIUM"]
    else:
        fg, bg = SEV_COLOURS["LOW"]
    parts = [f"{counts[s]}{s[0]}" for s in ("HIGH", "MEDIUM", "LOW") if counts[s]]
    return badge(f"{label}: {total} ({', '.join(parts)})", fg, bg)
