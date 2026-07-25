from jurisdictions.india.rules import (
    INDIA_RULES,
    CashTransactionLimit269ST,
    GSTPaymentConsistency,
    TDSDisbursementPattern,
    PMLAHighValueCash,
    RelatedPartyConcentration,
    CashLoanProhibition269SS_269T,
)


class IndiaJurisdictionModule:
    """Concrete implementation of the JurisdictionModule protocol for India.

    Pass this to ComplianceAnalyst.analyse() instead of the raw INDIA_RULES list
    when you want the jurisdiction name to travel with the rules.
    """
    name: str = "india"
    rules = INDIA_RULES


__all__ = [
    'INDIA_RULES',
    'IndiaJurisdictionModule',
    'CashTransactionLimit269ST',
    'GSTPaymentConsistency',
    'TDSDisbursementPattern',
    'PMLAHighValueCash',
    'RelatedPartyConcentration',
    'CashLoanProhibition269SS_269T',
]
