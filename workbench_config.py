"""Fund-specific customisation for the Workbench lens (Sprint 3, Step 3.4).

Two knobs, both optional:

1. BUILTIN_DETECTOR_TOGGLES — turn any of the six analysis.risk detectors off
   for this fund's reports (e.g. a fund that never trades in cash might not
   care about `structuring`).

2. CUSTOM_DETECTORS — add fund-specific rules as plain functions of
   (StatementDocument) -> list[Flag], without touching analysis/risk.py.

Call apply() right before handing a RiskReport to WorkbenchLensReport:

    from analysis.risk import RiskAnalyst
    import workbench_config

    risk_report = RiskAnalyst().analyse(doc)
    risk_report = workbench_config.apply(doc, risk_report)

Example custom detector — flag any single transaction over ₹50 lakh:

    from decimal import Decimal
    from analysis.risk import Flag, Severity

    def large_single_transaction(doc):
        flags = []
        for t in doc.transactions:
            amount = t.debit or t.credit or Decimal("0")
            if amount > Decimal("5000000"):
                flags.append(Flag(
                    detector_name="large_single_transaction",
                    severity=Severity.MEDIUM,
                    triggering_transaction_ids=[t.transaction_id],
                    description=f"Single transaction of Rs.{amount:,.0f} on {t.date}",
                ))
        return flags

    CUSTOM_DETECTORS = [
        CustomDetector(name="large_single_transaction", fn=large_single_transaction),
    ]
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from analysis.risk import Flag, RiskReport, compute_composite_score
from schema.canonical import StatementDocument


@dataclass(frozen=True)
class CustomDetector:
    name: str
    fn: Callable[[StatementDocument], list[Flag]]
    enabled: bool = True


# Turn any built-in detector off for this fund. All on by default.
BUILTIN_DETECTOR_TOGGLES: dict[str, bool] = {
    "structuring": True,
    "round_tripping": True,
    "spike_drain": True,
    "customer_concentration": True,
    "round_amount_clustering": True,
    "founder_over_extraction": True,
}

# Add fund-specific detectors here (see module docstring for an example).
CUSTOM_DETECTORS: list[CustomDetector] = []


def apply(doc: StatementDocument, risk_report: RiskReport) -> RiskReport:
    """Apply this fund's detector toggles and custom detectors to a
    RiskReport already produced by analysis.risk.RiskAnalyst.
    """
    kept = [
        flag for flag in risk_report.flags
        if BUILTIN_DETECTOR_TOGGLES.get(flag.detector_name, True)
    ]
    for detector in CUSTOM_DETECTORS:
        if detector.enabled:
            kept.extend(detector.fn(doc))

    return RiskReport(
        flags=kept,
        composite_score=compute_composite_score(kept),
        narrative=risk_report.narrative,
    )
