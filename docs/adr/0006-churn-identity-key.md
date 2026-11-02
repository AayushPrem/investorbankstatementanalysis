# ADR-0006: The churn model uses the narration counterparty key, not the resolver's customer_id

- **Status:** accepted (Phase 6, 2026-11-02)
- **Context:**
  - A churn unit needs a customer with ≥ 2 payments.
  - On corpus v1 narrations, `CustomerIdentityResolver` (embeddings off, as the pipeline runs it) keeps UTR/reference tokens in the cleaned name. Its completeness against the generator's true customers is 0.195, and 95% of its ids have a single row.
  - The Phase 5 narration key (`ml.features.transaction_features.counterparty_from_narration`) has completeness 0.871 at purity 0.998 (`ml/churn/identity_audit.py`, evaluation only).
- **Decision:**
  - Churn units and labels are built on the narration key of predicted-REVENUE, non-aggregator credits, with the same code at train and serve time.
  - At serving, each active resolver `customer_id` takes the risk of the key that covers most of its rows.
  - The resolver itself is unchanged.
- **Rejected alternatives:**
  1. *Units on resolver ids.* Almost no units exist.
  2. *Units on `labels/true_customer_id`.* Serving never has clean identity, so train and serve would differ and the numbers would look better than they are. It is kept only as an evaluation-only "oracle" row.
  3. *Fix the resolver now.* That would move gold `customer_id_accuracy` (0.6662) and every rule churn event, which Phase 6 must leave unchanged.
- **Consequences:**
  - Two identity notions coexist. Rule analytics (churn events, NRR, cohorts) use resolver ids; `churn_risk` uses narration keys.
  - Key splits add label noise: label agreement with true-id labels is 0.840 on test.
  - Unifying identity, for example by making the resolver use the narration key, is a follow-up. It needs its own gold-regression review.
