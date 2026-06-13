"""Tests for the synthetic bank statement generator."""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from tools.synthetic_gen import generate_statement

# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def tmp_root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("synthetic")


def _gen(tmp_root: Path, bank: str, profile: str, flags: list[str] | None = None) -> tuple[Path, Path]:
    return generate_statement(
        bank=bank,
        profile=profile,
        output_dir=tmp_root,
        statement_id=f"test_{bank}_{profile}",
        flags=flags or [],
        seed=42,
    )


# ─── basic existence + validity ───────────────────────────────────────────────

class TestPdfValidity:
    def test_hdfc_healthy_saas_pdf_exists(self, tmp_root: Path) -> None:
        pdf, _ = _gen(tmp_root, "hdfc", "healthy_saas")
        assert pdf.exists()
        assert pdf.stat().st_size > 0

    def test_icici_burning_startup_pdf_exists(self, tmp_root: Path) -> None:
        pdf, _ = _gen(tmp_root, "icici", "burning_startup")
        assert pdf.exists()
        assert pdf.stat().st_size > 0

    def test_pdf_magic_bytes_hdfc(self, tmp_root: Path) -> None:
        pdf, _ = _gen(tmp_root, "hdfc", "services_firm")
        assert pdf.read_bytes()[:4] == b"%PDF"

    def test_pdf_magic_bytes_icici(self, tmp_root: Path) -> None:
        pdf, _ = _gen(tmp_root, "icici", "ecommerce")
        assert pdf.read_bytes()[:4] == b"%PDF"

    def test_restaurant_pdf_exists(self, tmp_root: Path) -> None:
        pdf, _ = _gen(tmp_root, "hdfc", "restaurant")
        assert pdf.read_bytes()[:4] == b"%PDF"


# ─── ground truth JSON ────────────────────────────────────────────────────────

class TestGroundTruth:
    def _load(self, tmp_root: Path, bank: str, profile: str) -> dict:  # type: ignore[type-arg]
        _, truth = _gen(tmp_root, bank, profile)
        return json.loads(truth.read_text())

    def test_required_top_level_fields(self, tmp_root: Path) -> None:
        data = self._load(tmp_root, "hdfc", "healthy_saas")
        for key in ("statement_id", "bank", "profile", "account_id",
                    "period_start", "period_end", "opening_balance",
                    "closing_balance", "metrics", "transactions", "injected_flags"):
            assert key in data, f"missing field: {key}"

    def test_metrics_fields(self, tmp_root: Path) -> None:
        data = self._load(tmp_root, "hdfc", "healthy_saas")
        m = data["metrics"]
        for key in ("true_monthly_burn_rate", "true_runway_months",
                    "true_customer_count", "true_total_revenue", "true_total_expenses"):
            assert key in m, f"missing metric: {key}"

    def test_each_transaction_has_required_fields(self, tmp_root: Path) -> None:
        data = self._load(tmp_root, "icici", "burning_startup")
        for txn in data["transactions"]:
            for key in ("transaction_id", "date", "description",
                        "debit", "credit", "balance", "category"):
                assert key in txn, f"transaction missing field: {key}"

    def test_healthy_saas_has_multiple_customers(self, tmp_root: Path) -> None:
        data = self._load(tmp_root, "hdfc", "healthy_saas")
        assert data["metrics"]["true_customer_count"] >= 10

    def test_burning_startup_has_low_runway(self, tmp_root: Path) -> None:
        data = self._load(tmp_root, "icici", "burning_startup")
        # Burning startup should have short runway
        runway = data["metrics"]["true_runway_months"]
        assert runway < 12, f"burning_startup runway should be <12 months, got {runway}"

    def test_healthy_saas_has_positive_revenue(self, tmp_root: Path) -> None:
        data = self._load(tmp_root, "hdfc", "healthy_saas")
        assert Decimal(data["metrics"]["true_total_revenue"]) > 0


# ─── balance continuity ───────────────────────────────────────────────────────

class TestBalanceContinuity:
    def _check_continuity(self, tmp_root: Path, bank: str, profile: str) -> None:
        _, truth = _gen(tmp_root, bank, profile)
        data = json.loads(truth.read_text())
        opening = Decimal(data["opening_balance"])
        txns = data["transactions"]

        running = opening
        for i, t in enumerate(txns):
            credit = Decimal(t["credit"]) if t["credit"] else Decimal("0")
            debit = Decimal(t["debit"]) if t["debit"] else Decimal("0")
            running = running + credit - debit
            actual = Decimal(t["balance"])
            diff = abs(running - actual)
            assert diff <= Decimal("1"), (
                f"Balance break at txn {i} ({t['transaction_id']}): "
                f"expected {running}, got {actual}, diff {diff}"
            )

    def test_hdfc_healthy_saas_balance_continuity(self, tmp_root: Path) -> None:
        self._check_continuity(tmp_root, "hdfc", "healthy_saas")

    def test_icici_burning_startup_balance_continuity(self, tmp_root: Path) -> None:
        self._check_continuity(tmp_root, "icici", "burning_startup")

    def test_hdfc_services_firm_balance_continuity(self, tmp_root: Path) -> None:
        self._check_continuity(tmp_root, "hdfc", "services_firm")

    def test_icici_ecommerce_balance_continuity(self, tmp_root: Path) -> None:
        self._check_continuity(tmp_root, "icici", "ecommerce")

    def test_hdfc_restaurant_balance_continuity(self, tmp_root: Path) -> None:
        self._check_continuity(tmp_root, "hdfc", "restaurant")


# ─── red flag scenarios ───────────────────────────────────────────────────────

class TestRedFlags:
    def test_structuring_flag_recorded(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "hdfc", "healthy_saas", tmp_root,
            statement_id="test_structuring",
            flags=["structuring"],
            seed=99,
        )
        data = json.loads(truth.read_text())
        flag_types = [f["flag_type"] for f in data["injected_flags"]]
        assert "structuring" in flag_types

    def test_structuring_amounts_near_threshold(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "hdfc", "healthy_saas", tmp_root,
            statement_id="test_structuring_amt",
            flags=["structuring"],
            seed=100,
        )
        data = json.loads(truth.read_text())
        struct_flag = next(f for f in data["injected_flags"] if f["flag_type"] == "structuring")
        ids = set(struct_flag["triggering_transaction_ids"])
        flagged_txns = [t for t in data["transactions"] if t["transaction_id"] in ids]
        assert len(flagged_txns) >= 4
        for t in flagged_txns:
            amt = Decimal(t["credit"])
            assert Decimal("150000") <= amt < Decimal("200000"), (
                f"Structuring amount {amt} not in expected range ₹1.5L–₹2L"
            )

    def test_round_tripping_has_matching_pair(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "icici", "burning_startup", tmp_root,
            statement_id="test_roundtrip",
            flags=["round_tripping"],
            seed=101,
        )
        data = json.loads(truth.read_text())
        rt_flag = next(f for f in data["injected_flags"] if f["flag_type"] == "round_tripping")
        assert len(rt_flag["triggering_transaction_ids"]) == 2
        ids = set(rt_flag["triggering_transaction_ids"])
        flagged = [t for t in data["transactions"] if t["transaction_id"] in ids]
        has_debit = any(t["debit"] for t in flagged)
        has_credit = any(t["credit"] for t in flagged)
        assert has_debit and has_credit

    def test_founder_extraction_recorded(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "hdfc", "burning_startup", tmp_root,
            statement_id="test_founder",
            flags=["founder_extraction"],
            seed=102,
        )
        data = json.loads(truth.read_text())
        assert any(f["flag_type"] == "founder_extraction" for f in data["injected_flags"])

    def test_customer_churn_reduces_payments(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "hdfc", "healthy_saas", tmp_root,
            statement_id="test_churn",
            flags=["customer_churn"],
            seed=103,
        )
        data = json.loads(truth.read_text())
        assert any(f["flag_type"] == "customer_churn" for f in data["injected_flags"])
        churn_flag = next(f for f in data["injected_flags"] if f["flag_type"] == "customer_churn")
        assert len(churn_flag["churned_customer_ids"]) >= 1

    def test_related_party_leakage_flag(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "hdfc", "healthy_saas", tmp_root,
            statement_id="test_rp",
            flags=["related_party_leakage"],
            seed=104,
        )
        data = json.loads(truth.read_text())
        assert any(f["flag_type"] == "related_party_leakage" for f in data["injected_flags"])
        rp_txns = [t for t in data["transactions"] if t.get("is_related_party")]
        assert len(rp_txns) >= 1

    def test_multiple_flags_combined(self, tmp_root: Path) -> None:
        _, truth = generate_statement(
            "icici", "healthy_saas", tmp_root,
            statement_id="test_multi_flag",
            flags=["structuring", "round_tripping"],
            seed=105,
        )
        data = json.loads(truth.read_text())
        flag_types = {f["flag_type"] for f in data["injected_flags"]}
        assert "structuring" in flag_types
        assert "round_tripping" in flag_types
