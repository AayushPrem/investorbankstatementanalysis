"""Display labels for synthetic-generator profiles and injectable flags."""
from __future__ import annotations

ALL_FLAGS = [
    "structuring",
    "round_tripping",
    "founder_extraction",
    "customer_churn",
    "revenue_concentration",
    "related_party_leakage",
]

PROFILE_LABELS = {
    "healthy_saas":    "Healthy SaaS",
    "burning_startup": "Burning Startup",
    "services_firm":   "Services Firm (B2G)",
    "ecommerce":       "E-Commerce",
    "restaurant":      "Restaurant / F&B",
}

FLAG_LABELS = {
    "structuring":           "Cash structuring (just under ₹2L)",
    "round_tripping":        "Round-tripping (money out and back)",
    "founder_extraction":    "Founder over-extraction",
    "customer_churn":        "Significant customer churn",
    "revenue_concentration": "Revenue concentration (whale customer)",
    "related_party_leakage": "Related-party leakage",
}
