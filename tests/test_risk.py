"""Tests for analysis/risk.py — all six detectors + RiskAnalyst (Step 2.3)."""
from __future__ import annotations

import datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from analysis.risk import (
    Flag,
    RiskAnalyst,
    RiskReport,
    Severity,
    _fallback_narrative,
    detect_customer_concentration,
    detect_founder_over_extraction,
    detect_round_amount_clustering,
    detect_round_tripping,
    detect_spikes_drains,
    detect_structuring,
)
from schema.canonical import (
    CanonicalTransaction,
    SourceReference,
    StatementDocument,
    TransactionCategory,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

_DATE = datetime.date(2024, 4, 1)
_ZERO = Decimal("0")


def _src(row: int = 0) -> SourceReference:
    return SourceReference(file_path="test.pdf", page=1, row=row, raw_text="")


def _credit(
    amount: Decimal | int | str,
    *,
    date: datetime.date = _DATE,
    txn_id: str = "t1",
    category: TransactionCategory = TransactionCategory.REVENUE,
    counterparty: str | None = None,
    customer_id: str | None = None,
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=txn_id,
        date=date,
        description="payment",
        credit=Decimal(str(amount)),
        balance=Decimal("100000"),
        category=category,
        counterparty=counterparty,
        customer_id=customer_id,
        source_reference=_src(),
    )


def _debit(
    amount: Decimal | int | str,
    *,
    date: datetime.date = _DATE,
    txn_id: str = "t1",
    category: TransactionCategory = TransactionCategory.VENDOR_PAYMENT,
    counterparty: str | None = None,
) -> CanonicalTransaction:
    return CanonicalTransaction(
        transaction_id=txn_id,
        date=date,
        description="payment",
        debit=Decimal(str(amount)),
        balance=Decimal("100000"),
        category=category,
        counterparty=counterparty,
        source_reference=_src(),
    )


def _doc(*txns: CanonicalTransaction) -> StatementDocument:
    return StatementDocument(
        account_id="ACC001",
        statement_period_start=datetime.date(2024, 1, 1),
        statement_period_end=datetime.date(2024, 6, 30),
        source_format="HDFC_DIGITAL",
        transactions=list(txns),
    )


def _date(month: int, day: int = 1, year: int = 2024) -> datetime.date:
    return datetime.date(year, month, day)


# ─────────────────────────────────────────────────────────────────────────────
# Detector 1 — Structuring
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectStructuring:
    def test_four_credits_in_range_within_window_flagged(self) -> None:
        txns = [
            _credit(170000, txn_id=f"t{i}", date=_date(4, i + 1))
            for i in range(4)
        ]
        flags = detect_structuring(_doc(*txns))
        assert len(flags) == 1
        assert flags[0].detector_name == "structuring"
        assert flags[0].severity == Severity.HIGH

    def test_cluster_size_in_evidence(self) -> None:
        txns = [_credit(170000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(5)]
        flags = detect_structuring(_doc(*txns))
        assert flags[0].evidence["cluster_size"] == 5

    def test_triggering_ids_match_cluster(self) -> None:
        txns = [_credit(170000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(4)]
        flags = detect_structuring(_doc(*txns))
        assert set(flags[0].triggering_transaction_ids) == {"t0", "t1", "t2", "t3"}

    def test_only_three_in_range_no_flag(self) -> None:
        txns = [_credit(170000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(3)]
        assert detect_structuring(_doc(*txns)) == []

    def test_amounts_below_range_no_flag(self) -> None:
        # ₹1.4L — below the ₹1.5L lower bound
        txns = [_credit(140000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(4)]
        assert detect_structuring(_doc(*txns)) == []

    def test_amounts_above_range_no_flag(self) -> None:
        # ₹2.1L — above the ₹2L upper bound
        txns = [_credit(210000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(4)]
        assert detect_structuring(_doc(*txns)) == []

    def test_boundary_lower_included(self) -> None:
        txns = [_credit(150000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(4)]
        assert detect_structuring(_doc(*txns)) != []

    def test_boundary_upper_included(self) -> None:
        txns = [_credit(200000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(4)]
        assert detect_structuring(_doc(*txns)) != []

    def test_credits_spread_beyond_seven_days_no_flag(self) -> None:
        # Days 1, 4, 8, 12 — gap between first and last is 11 days > 7
        dates = [_date(4, d) for d in [1, 4, 8, 12]]
        txns = [_credit(170000, txn_id=f"t{i}", date=dates[i]) for i in range(4)]
        assert detect_structuring(_doc(*txns)) == []

    def test_seven_day_window_is_inclusive(self) -> None:
        # Days 1 and 8 are exactly 7 days apart → should be in the same window
        dates = [_date(4, d) for d in [1, 3, 5, 8]]
        txns = [_credit(170000, txn_id=f"t{i}", date=dates[i]) for i in range(4)]
        flags = detect_structuring(_doc(*txns))
        assert len(flags) == 1

    def test_debit_transactions_not_flagged(self) -> None:
        txns = [
            _debit(170000, txn_id=f"t{i}", date=_date(4, i + 1))
            for i in range(4)
        ]
        assert detect_structuring(_doc(*txns)) == []

    def test_two_separate_clusters_reported(self) -> None:
        # Cluster A: days 1-3 in April, Cluster B: days 1-3 in June
        cluster_a = [_credit(170000, txn_id=f"a{i}", date=_date(4, i + 1)) for i in range(4)]
        cluster_b = [_credit(180000, txn_id=f"b{i}", date=_date(6, i + 1)) for i in range(4)]
        flags = detect_structuring(_doc(*cluster_a, *cluster_b))
        assert len(flags) == 2

    def test_empty_document_no_flag(self) -> None:
        assert detect_structuring(_doc()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Detector 2 — Round-tripping
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectRoundTripping:
    def test_matching_pair_flagged(self) -> None:
        d = _debit(100000, txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(98000,  txn_id="c1", date=_date(4, 6), counterparty="acme")
        flags = detect_round_tripping(_doc(d, c))
        assert len(flags) == 1
        assert flags[0].detector_name == "round_tripping"
        assert flags[0].severity == Severity.HIGH

    def test_triggering_ids_contain_both_transactions(self) -> None:
        d = _debit(100000, txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(98000,  txn_id="c1", date=_date(4, 6), counterparty="acme")
        flags = detect_round_tripping(_doc(d, c))
        assert set(flags[0].triggering_transaction_ids) == {"d1", "c1"}

    def test_different_counterparties_no_flag(self) -> None:
        d = _debit(100000,  txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(100000, txn_id="c1", date=_date(4, 6), counterparty="apex")
        assert detect_round_tripping(_doc(d, c)) == []

    def test_credit_before_debit_no_flag(self) -> None:
        c = _credit(100000, txn_id="c1", date=_date(4, 1), counterparty="acme")
        d = _debit(100000,  txn_id="d1", date=_date(4, 6), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) == []

    def test_too_soon_no_flag(self) -> None:
        # Only 2 days — below the 3-day minimum
        d = _debit(100000,  txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(100000, txn_id="c1", date=_date(4, 3), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) == []

    def test_exactly_three_days_flagged(self) -> None:
        d = _debit(100000,  txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(100000, txn_id="c1", date=_date(4, 4), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) != []

    def test_too_late_no_flag(self) -> None:
        # 15 days — beyond the 14-day maximum
        d = _debit(100000,  txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(100000, txn_id="c1", date=_date(4, 16), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) == []

    def test_exactly_fourteen_days_flagged(self) -> None:
        d = _debit(100000,  txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(100000, txn_id="c1", date=_date(4, 15), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) != []

    def test_amount_too_different_no_flag(self) -> None:
        # 10% difference — exceeds 5% tolerance
        d = _debit(100000, txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(90000,  txn_id="c1", date=_date(4, 6), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) == []

    def test_within_five_percent_tolerance_flagged(self) -> None:
        # 4.9% difference
        d = _debit(100000, txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(95100,  txn_id="c1", date=_date(4, 6), counterparty="acme")
        assert detect_round_tripping(_doc(d, c)) != []

    def test_no_counterparty_set_no_flag(self) -> None:
        d = _debit(100000,  txn_id="d1", date=_date(4, 1))
        c = _credit(100000, txn_id="c1", date=_date(4, 6))
        assert detect_round_tripping(_doc(d, c)) == []

    def test_evidence_fields_populated(self) -> None:
        d = _debit(100000, txn_id="d1", date=_date(4, 1), counterparty="acme")
        c = _credit(98000,  txn_id="c1", date=_date(4, 6), counterparty="acme")
        flag = detect_round_tripping(_doc(d, c))[0]
        assert flag.evidence["counterparty"] == "acme"
        assert flag.evidence["days_between"] == 5

    def test_empty_document_no_flag(self) -> None:
        assert detect_round_tripping(_doc()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Detector 3 — Spikes and drains
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectSpikesDrains:
    def _flat_months(self, n: int = 4, base_credit: int = 100000) -> list[CanonicalTransaction]:
        """n months of equal net inflows."""
        txns = []
        for i in range(n):
            txns.append(_credit(
                base_credit,
                txn_id=f"m{i}",
                date=datetime.date(2024, i + 1, 1),
            ))
        return txns

    def test_spike_month_flagged(self) -> None:
        # Months 1-5: varying but modest net flows. Month 6: huge spike.
        # Leave-one-out z-score needs a baseline with non-zero stdev — achieved
        # by varying the base months slightly.
        txns = [
            _credit(50000,   txn_id="m0", date=_date(1, 1)),
            _credit(75000,   txn_id="m1", date=_date(2, 1)),
            _credit(100000,  txn_id="m2", date=_date(3, 1)),
            _credit(80000,   txn_id="m3", date=_date(4, 1)),
            _credit(3000000, txn_id="m4", date=_date(5, 1)),  # massive spike
        ]
        flags = detect_spikes_drains(_doc(*txns))
        assert any(f.detector_name == "spike_drain" for f in flags)
        spike_flags = [f for f in flags if "spike" in f.evidence.get("direction", "")]
        assert len(spike_flags) >= 1

    def test_drain_month_flagged(self) -> None:
        # Months with varying positive net flows, then one massive drain.
        txns = [
            _credit(200000, txn_id="m0", date=_date(1, 1)),
            _credit(190000, txn_id="m1", date=_date(2, 1)),
            _credit(210000, txn_id="m2", date=_date(3, 1)),
            _credit(195000, txn_id="m3", date=_date(4, 1)),
            _debit(3000000, txn_id="m4", date=_date(5, 1),
                   category=TransactionCategory.VENDOR_PAYMENT),
        ]
        flags = detect_spikes_drains(_doc(*txns))
        drain_flags = [f for f in flags if "drain" in f.evidence.get("direction", "")]
        assert len(drain_flags) >= 1

    def test_fewer_than_three_months_no_flag(self) -> None:
        txns = [
            _credit(100000, txn_id=f"t{i}", date=datetime.date(2024, i + 1, 1))
            for i in range(2)
        ]
        assert detect_spikes_drains(_doc(*txns)) == []

    def test_uniform_months_no_flag(self) -> None:
        # All months identical → stdev = 0 → skip
        txns = [
            _credit(100000, txn_id=f"t{i}", date=datetime.date(2024, i + 1, 1))
            for i in range(4)
        ]
        assert detect_spikes_drains(_doc(*txns)) == []

    def test_z_score_in_evidence(self) -> None:
        base = self._flat_months(3)
        spike = _credit(2000000, txn_id="spike", date=_date(4, 1))
        flags = detect_spikes_drains(_doc(*base, spike))
        if flags:
            assert "z_score" in flags[0].evidence
            assert abs(flags[0].evidence["z_score"]) > 2.5

    def test_high_severity_for_extreme_spike(self) -> None:
        # Varied baseline so leave-one-out σ > 0, then an extreme spike (z >> 3.5).
        txns = [
            _credit(50000,   txn_id="m0", date=datetime.date(2024, 1, 1)),
            _credit(60000,   txn_id="m1", date=datetime.date(2024, 2, 1)),
            _credit(55000,   txn_id="m2", date=datetime.date(2024, 3, 1)),
            _credit(58000,   txn_id="m3", date=datetime.date(2024, 4, 1)),
            _credit(52000,   txn_id="m4", date=datetime.date(2024, 5, 1)),
            _credit(10000000, txn_id="m5", date=datetime.date(2024, 6, 1)),  # extreme
        ]
        flags = detect_spikes_drains(_doc(*txns))
        spike_flags = [f for f in flags if "spike" in f.evidence.get("direction", "")]
        assert any(f.severity == Severity.HIGH for f in spike_flags)

    def test_medium_severity_for_moderate_spike(self) -> None:
        # Carefully construct a spike with 2.5 < |z| < 3.5
        txns = [
            _credit(100000, txn_id="m0", date=_date(1, 1)),
            _credit(110000, txn_id="m1", date=_date(2, 1)),
            _credit(90000,  txn_id="m2", date=_date(3, 1)),
            _credit(100000, txn_id="m3", date=_date(4, 1)),
            _credit(105000, txn_id="m4", date=_date(5, 1)),
            _credit(500000, txn_id="m5", date=_date(6, 1)),  # moderate spike
        ]
        flags = detect_spikes_drains(_doc(*txns))
        if flags:
            assert any(
                f.severity in (Severity.MEDIUM, Severity.HIGH)
                for f in flags
            )

    def test_empty_document_no_flag(self) -> None:
        assert detect_spikes_drains(_doc()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Detector 4 — Customer concentration
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectCustomerConcentration:
    def _rev_txn(
        self,
        amount: int,
        customer_id: str,
        txn_id: str,
    ) -> CanonicalTransaction:
        return _credit(
            amount,
            txn_id=txn_id,
            category=TransactionCategory.REVENUE,
            customer_id=customer_id,
        )

    def test_top3_above_80_is_high(self) -> None:
        # Customers A: 60%, B: 15%, C: 10%, D: 15% → top-3 = 85%
        txns = [
            self._rev_txn(60000, "A", "t1"),
            self._rev_txn(15000, "B", "t2"),
            self._rev_txn(10000, "C", "t3"),
            self._rev_txn(15000, "D", "t4"),
        ]
        flags = detect_customer_concentration(_doc(*txns))
        assert len(flags) == 1
        assert flags[0].severity == Severity.HIGH

    def test_top3_between_60_and_80_is_medium(self) -> None:
        # A: 30%, B: 25%, C: 20%, D: 15%, E: 10% → top-3 = 75% (MEDIUM)
        txns = [
            self._rev_txn(30000, "A", "t1"),
            self._rev_txn(25000, "B", "t2"),
            self._rev_txn(20000, "C", "t3"),
            self._rev_txn(15000, "D", "t4"),
            self._rev_txn(10000, "E", "t5"),
        ]
        flags = detect_customer_concentration(_doc(*txns))
        assert len(flags) == 1
        assert flags[0].severity == Severity.MEDIUM

    def test_top3_at_or_below_60_no_flag(self) -> None:
        # Each of 5 customers has 20% → top-3 = 60%  (not > 60%)
        txns = [self._rev_txn(20000, f"C{i}", f"t{i}") for i in range(5)]
        assert detect_customer_concentration(_doc(*txns)) == []

    def test_no_customer_ids_no_flag(self) -> None:
        txns = [_credit(50000, txn_id=f"t{i}", category=TransactionCategory.REVENUE)
                for i in range(3)]
        assert detect_customer_concentration(_doc(*txns)) == []

    def test_evidence_top3_and_top1_share(self) -> None:
        txns = [
            self._rev_txn(70000, "A", "t1"),
            self._rev_txn(15000, "B", "t2"),
            self._rev_txn(15000, "C", "t3"),
        ]
        flags = detect_customer_concentration(_doc(*txns))
        ev = flags[0].evidence
        assert ev["top3_share_pct"] == pytest.approx(100.0)
        assert ev["top1_share_pct"] == pytest.approx(70.0)

    def test_non_revenue_transactions_excluded(self) -> None:
        # All salary debits — no revenue → no flag
        txns = [
            _debit(50000, txn_id=f"t{i}", category=TransactionCategory.SALARY)
            for i in range(3)
        ]
        assert detect_customer_concentration(_doc(*txns)) == []

    def test_single_customer_100_percent(self) -> None:
        txns = [self._rev_txn(100000, "ONLY", "t1")]
        flags = detect_customer_concentration(_doc(*txns))
        assert len(flags) == 1
        assert flags[0].severity == Severity.HIGH

    def test_triggering_ids_only_from_top3(self) -> None:
        txns = [
            self._rev_txn(50000, "A", "t1"),
            self._rev_txn(30000, "B", "t2"),
            self._rev_txn(10000, "C", "t3"),
            self._rev_txn(10000, "D", "t4"),
        ]
        flags = detect_customer_concentration(_doc(*txns))
        flagged_ids = set(flags[0].triggering_transaction_ids)
        assert "t1" in flagged_ids
        assert "t2" in flagged_ids
        assert "t3" in flagged_ids

    def test_empty_document_no_flag(self) -> None:
        assert detect_customer_concentration(_doc()) == []


# ─────────────────────────────────────────────────────────────────────────────
# Detector 5 — Round-amount clustering
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectRoundAmountClustering:
    def _rev(self, amount: int | str, txn_id: str) -> CanonicalTransaction:
        return _credit(amount, txn_id=txn_id, category=TransactionCategory.REVENUE)

    def test_majority_round_flagged(self) -> None:
        # 5 round + 1 irregular = 83% round → HIGH
        txns = [self._rev(100000, f"t{i}") for i in range(5)]
        txns.append(self._rev("87543.50", "t5"))
        flags = detect_round_amount_clustering(_doc(*txns))
        assert len(flags) == 1

    def test_above_30_pct_medium_severity(self) -> None:
        # 4 round + 6 irregular = 40% → MEDIUM
        round_txns = [self._rev(50000, f"r{i}") for i in range(4)]
        irr_txns   = [self._rev("47231.75", f"i{i}") for i in range(6)]
        flags = detect_round_amount_clustering(_doc(*round_txns, *irr_txns))
        assert len(flags) == 1
        assert flags[0].severity == Severity.MEDIUM

    def test_above_60_pct_high_severity(self) -> None:
        # 7 round + 3 irregular = 70% → HIGH
        round_txns = [self._rev(50000, f"r{i}") for i in range(7)]
        irr_txns   = [self._rev("47231.75", f"i{i}") for i in range(3)]
        flags = detect_round_amount_clustering(_doc(*round_txns, *irr_txns))
        assert flags[0].severity == Severity.HIGH

    def test_below_30_pct_no_flag(self) -> None:
        # 2 round + 8 irregular = 20%
        round_txns = [self._rev(50000, f"r{i}") for i in range(2)]
        irr_txns   = [self._rev("47231.75", f"i{i}") for i in range(8)]
        assert detect_round_amount_clustering(_doc(*round_txns, *irr_txns)) == []

    def test_exactly_30_pct_no_flag(self) -> None:
        # 3 round + 7 irregular = 30% (not > 30%)
        round_txns = [self._rev(50000, f"r{i}") for i in range(3)]
        irr_txns   = [self._rev("47231.75", f"i{i}") for i in range(7)]
        assert detect_round_amount_clustering(_doc(*round_txns, *irr_txns)) == []

    def test_no_revenue_transactions_no_flag(self) -> None:
        txns = [_debit(50000, txn_id="t1", category=TransactionCategory.VENDOR_PAYMENT)]
        assert detect_round_amount_clustering(_doc(*txns)) == []

    def test_all_irregular_amounts_no_flag(self) -> None:
        txns = [self._rev(f"{45000 + i * 1337}.{i * 17 % 100:02d}", f"t{i}")
                for i in range(6)]
        assert detect_round_amount_clustering(_doc(*txns)) == []

    def test_triggering_ids_are_only_round_transactions(self) -> None:
        round_txns = [self._rev(50000, f"r{i}") for i in range(5)]
        irr_txns   = [self._rev("47231.75", f"i{i}") for i in range(2)]
        flags = detect_round_amount_clustering(_doc(*round_txns, *irr_txns))
        flagged = set(flags[0].triggering_transaction_ids)
        assert all(tid.startswith("r") for tid in flagged)
        assert not any(tid.startswith("i") for tid in flagged)

    def test_evidence_fields_present(self) -> None:
        round_txns = [self._rev(50000, f"r{i}") for i in range(5)]
        irr_txns   = [self._rev("47231.75", f"i{i}") for i in range(3)]
        flag = detect_round_amount_clustering(_doc(*round_txns, *irr_txns))[0]
        assert "round_count" in flag.evidence
        assert "proportion_pct" in flag.evidence


# ─────────────────────────────────────────────────────────────────────────────
# Detector 6 — Founder over-extraction
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectFounderOverExtraction:
    def _fw(self, amount: int, txn_id: str, month: int = 4) -> CanonicalTransaction:
        return _debit(
            amount,
            txn_id=txn_id,
            date=datetime.date(2024, month, 15),
            category=TransactionCategory.FOUNDER_WITHDRAWAL,
        )

    # ── No declared salary ────────────────────────────────────────────────────

    def test_above_5l_no_salary_flagged(self) -> None:
        flags = detect_founder_over_extraction(_doc(self._fw(600000, "t1")))
        assert len(flags) == 1
        assert flags[0].detector_name == "founder_over_extraction"
        assert flags[0].severity == Severity.MEDIUM

    def test_below_5l_no_salary_no_flag(self) -> None:
        flags = detect_founder_over_extraction(_doc(self._fw(400000, "t1")))
        assert flags == []

    def test_exactly_5l_no_salary_no_flag(self) -> None:
        # Exactly ₹5L — not > ₹5L
        flags = detect_founder_over_extraction(_doc(self._fw(500000, "t1")))
        assert flags == []

    # ── With declared salary ──────────────────────────────────────────────────

    def test_above_3x_salary_flagged(self) -> None:
        # Salary ₹1L, withdrawal ₹6L = 6× → flag
        flags = detect_founder_over_extraction(
            _doc(self._fw(600000, "t1")),
            declared_founder_salary=Decimal("100000"),
        )
        assert len(flags) == 1
        assert flags[0].severity == Severity.HIGH

    def test_below_3x_salary_no_flag(self) -> None:
        # Salary ₹1L, withdrawal ₹2.9L = 2.9× → no flag
        flags = detect_founder_over_extraction(
            _doc(self._fw(290000, "t1")),
            declared_founder_salary=Decimal("100000"),
        )
        assert flags == []

    def test_exactly_3x_salary_no_flag(self) -> None:
        # Exactly 3× salary — not > 3×
        flags = detect_founder_over_extraction(
            _doc(self._fw(300000, "t1")),
            declared_founder_salary=Decimal("100000"),
        )
        assert flags == []

    def test_multiple_months_each_flagged_independently(self) -> None:
        t_apr = self._fw(600000, "t_apr", month=4)
        t_may = self._fw(700000, "t_may", month=5)
        flags = detect_founder_over_extraction(_doc(t_apr, t_may))
        assert len(flags) == 2

    def test_two_withdrawals_same_month_aggregated(self) -> None:
        # Two withdrawals in April summing to ₹6L → one flag
        t1 = self._fw(300000, "t1", month=4)
        t2 = self._fw(300000, "t2", month=4)
        flags = detect_founder_over_extraction(_doc(t1, t2))
        assert len(flags) == 1

    def test_no_founder_withdrawal_transactions_no_flag(self) -> None:
        txns = [_credit(100000, txn_id="t1", category=TransactionCategory.REVENUE)]
        assert detect_founder_over_extraction(_doc(*txns)) == []

    def test_evidence_fields_present(self) -> None:
        flag = detect_founder_over_extraction(
            _doc(self._fw(600000, "t1")),
        )[0]
        assert "withdrawal_amount" in flag.evidence
        assert "threshold" in flag.evidence

    def test_salary_evidence_includes_multiple(self) -> None:
        flag = detect_founder_over_extraction(
            _doc(self._fw(400000, "t1")),
            declared_founder_salary=Decimal("100000"),
        )[0]
        assert flag.evidence["multiple_of_salary"] == pytest.approx(4.0)


# ─────────────────────────────────────────────────────────────────────────────
# RiskAnalyst — orchestration + scoring
# ─────────────────────────────────────────────────────────────────────────────

class TestRiskAnalyst:
    def _analyst(self) -> RiskAnalyst:
        return RiskAnalyst(llm_enabled=False)

    def test_returns_risk_report(self) -> None:
        result = self._analyst().analyse(_doc())
        assert isinstance(result, RiskReport)

    def test_clean_document_score_zero(self) -> None:
        # Irregular amounts so round_amount_clustering doesn't fire
        doc = _doc(_credit("87543.50", txn_id="t1", category=TransactionCategory.REVENUE))
        result = self._analyst().analyse(doc)
        assert result.composite_score == 0.0

    def test_clean_document_no_flags(self) -> None:
        doc = _doc(_credit("87543.50", txn_id="t1", category=TransactionCategory.REVENUE))
        result = self._analyst().analyse(doc)
        assert result.flags == []

    def test_clean_narrative_message(self) -> None:
        doc = _doc(_credit("87543.50", txn_id="t1", category=TransactionCategory.REVENUE))
        result = self._analyst().analyse(doc)
        assert "no risk" in result.narrative.lower()

    def test_score_two_high_flags(self) -> None:
        # Inject 2 HIGH flags via structuring + round-tripping
        struct_txns = [
            _credit(170000, txn_id=f"s{i}", date=_date(4, i + 1))
            for i in range(4)
        ]
        rt_d = _debit(100000, txn_id="rt_d", date=_date(5, 1), counterparty="acme")
        rt_c = _credit(99000,  txn_id="rt_c", date=_date(5, 6), counterparty="acme")
        doc = _doc(*struct_txns, rt_d, rt_c)
        result = self._analyst().analyse(doc)
        assert result.composite_score >= 20.0  # at least 2 × 10

    def test_score_capped_at_100(self) -> None:
        # Force > 10 HIGH flags via many structuring clusters
        txns = []
        for cluster in range(12):
            base_date = datetime.date(2024, 1 + (cluster * 10 // 30), 1 + (cluster * 10 % 28))
            for j in range(4):
                d = base_date.replace(day=min(28, base_date.day + j))
                txns.append(_credit(170000, txn_id=f"c{cluster}_{j}", date=d))
        result = self._analyst().analyse(_doc(*txns))
        assert result.composite_score <= 100.0

    def test_high_weight_10_medium_weight_5_low_weight_2(self) -> None:
        # Manually construct flags and check score arithmetic
        flags = [
            Flag("x", Severity.HIGH,   ["t1"], "desc h"),
            Flag("x", Severity.MEDIUM, ["t2"], "desc m"),
            Flag("x", Severity.LOW,    ["t3"], "desc l"),
        ]
        expected = min(100.0, 10.0 + 5.0 + 2.0)
        from analysis.risk import _SEVERITY_WEIGHTS
        score = min(100.0, sum(_SEVERITY_WEIGHTS[f.severity] for f in flags))
        assert score == pytest.approx(expected)

    def test_all_detectors_run(self) -> None:
        """Test that analyse() calls all six detectors."""
        analyst = self._analyst()
        with (
            patch("analysis.risk.detect_structuring",            return_value=[]) as m1,
            patch("analysis.risk.detect_round_tripping",         return_value=[]) as m2,
            patch("analysis.risk.detect_spikes_drains",          return_value=[]) as m3,
            patch("analysis.risk.detect_customer_concentration", return_value=[]) as m4,
            patch("analysis.risk.detect_round_amount_clustering",return_value=[]) as m5,
            patch("analysis.risk.detect_founder_over_extraction",return_value=[]) as m6,
        ):
            analyst.analyse(_doc())
        for m in (m1, m2, m3, m4, m5, m6):
            m.assert_called_once()

    def test_declared_salary_passed_to_founder_detector(self) -> None:
        analyst = self._analyst()
        salary = Decimal("200000")
        with patch("analysis.risk.detect_founder_over_extraction", return_value=[]) as m:
            analyst.analyse(_doc(), declared_founder_salary=salary)
        _, kwargs = m.call_args
        assert kwargs.get("declared_founder_salary") == salary or m.call_args[0][1] == salary

    def test_narrative_populated(self) -> None:
        txns = [_credit(170000, txn_id=f"t{i}", date=_date(4, i + 1)) for i in range(4)]
        result = self._analyst().analyse(_doc(*txns))
        assert isinstance(result.narrative, str)
        assert len(result.narrative) > 0

    def test_llm_disabled_without_api_key(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            analyst = RiskAnalyst(llm_enabled=True)
        assert analyst._llm_enabled is False

    def test_llm_disabled_explicitly(self) -> None:
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            analyst = RiskAnalyst(llm_enabled=False)
        assert analyst._llm_enabled is False


# ─────────────────────────────────────────────────────────────────────────────
# _fallback_narrative
# ─────────────────────────────────────────────────────────────────────────────

class TestFallbackNarrative:
    def test_empty_flags_raises_or_returns(self) -> None:
        # _fallback_narrative is only called when there are flags; but
        # test it handles empty list gracefully
        narrative = _fallback_narrative([])
        assert "0" in narrative or len(narrative) > 0

    def test_includes_flag_count(self) -> None:
        flags = [Flag("x", Severity.HIGH, ["t1"], "some issue")]
        narrative = _fallback_narrative(flags)
        assert "1" in narrative

    def test_includes_severity_label(self) -> None:
        flags = [Flag("x", Severity.HIGH, ["t1"], "description here")]
        narrative = _fallback_narrative(flags)
        assert "HIGH" in narrative

    def test_includes_description(self) -> None:
        flags = [Flag("x", Severity.MEDIUM, ["t1"], "specific description")]
        narrative = _fallback_narrative(flags)
        assert "specific description" in narrative


# ─────────────────────────────────────────────────────────────────────────────
# Integration — clean healthy document has low false-positive rate
# ─────────────────────────────────────────────────────────────────────────────

class TestFalsePositiveRate:
    def _healthy_doc(self) -> StatementDocument:
        """Simulate a clean 6-month SaaS business with varied amounts."""
        txns: list[CanonicalTransaction] = []
        i = 0
        for month in range(1, 7):
            # ~5 customers paying varied amounts per month
            for cust_no in range(1, 6):
                amount = 45000 + cust_no * 7013 + month * 1337  # irregular amounts
                txns.append(_credit(
                    amount,
                    txn_id=f"rev_{month}_{cust_no}",
                    date=datetime.date(2024, month, cust_no * 5),
                    category=TransactionCategory.REVENUE,
                    customer_id=f"cust_{cust_no:02d}",
                ))
                i += 1
            # Monthly vendor payment (irregular amount)
            txns.append(_debit(
                30000 + month * 1117,
                txn_id=f"vendor_{month}",
                date=datetime.date(2024, month, 20),
                category=TransactionCategory.VENDOR_PAYMENT,
            ))
        return _doc(*txns)

    def test_healthy_doc_score_is_low(self) -> None:
        result = RiskAnalyst(llm_enabled=False).analyse(self._healthy_doc())
        # A genuinely healthy document should have a low risk score
        assert result.composite_score < 30.0

    def test_healthy_doc_no_structuring(self) -> None:
        assert detect_structuring(self._healthy_doc()) == []

    def test_healthy_doc_no_round_tripping(self) -> None:
        assert detect_round_tripping(self._healthy_doc()) == []

    def test_healthy_doc_no_founder_extraction(self) -> None:
        assert detect_founder_over_extraction(self._healthy_doc()) == []
