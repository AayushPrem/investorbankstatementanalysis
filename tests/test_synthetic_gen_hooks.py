"""Phase 3: the corpus-mode hooks in tools/synthetic_gen.py are inert by default.

The pinned hashes below were produced by the pre-Phase-3 generator (commit
37dd801) and re-checked against the hooked generator in the same session; any
change to the default-argument RNG stream or output breaks them.
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from scripts.build_gold_set import _deterministic
from tools import synthetic_gen as sg

# (bank, profile, risk_mode, flags, n_months, seed, sha256 of the truth JSON)
PINNED = [
    ("hdfc", "healthy_saas", "realistic", (), 6, 7001, "b20cc9fa560e85a49b00d37e1ddcda5cf858801b257d540612685288211b93b0"),
    ("icici", "healthy_saas", "manual", ("customer_churn", "revenue_concentration"), 6, 7002, "6740055f7c6c1a13a0626272f1ec39427ed9aba6958b66e392ef6d683387e00a"),
    ("hdfc", "burning_startup", "realistic", (), 12, 7003, "ad0d0eb2fabc97ab3a5b487308d5d099ca81f7365c98794aadce09cc26936235"),
    ("icici", "burning_startup", "manual", ("founder_extraction", "compliance_gst_gap"), 6, 7004, "85156e417bdadf1535e48ab938e35ae4849654f11aa5e3afa33342108e1fd048"),
    ("hdfc", "services_firm", "realistic", (), 12, 7005, "b01ca5cd4013550a26f25fe03f584976ed86585b961419e3cea85f0adcd2d457"),
    ("icici", "services_firm", "manual", ("round_tripping", "missing_month_gap"), 6, 7006, "4f945ec461bb7ba45d00e24a63a9242d9822b2c190f6e884a57c55177ebe81d6"),
    ("hdfc", "ecommerce", "manual", ("structuring", "aggregator_dominated_revenue"), 6, 7007, "3e3333e8af35e91513460e66bf765eefc222c93065dcf71f263daecab9236829"),
    ("icici", "ecommerce", "clean", (), 3, 7008, "c8024c883f46f007f2b8d09e8d4a38a13d7c9dd4ed96e01eb63dfea4e81e21f4"),
    ("hdfc", "restaurant", "manual", ("compliance_pmla_cash", "fx_inflow"), 6, 7009, "693c573557d4ec58e2e5a07a89037c68c7d80a2606e0eee515f73240c0c259be"),
    ("icici", "restaurant", "realistic", (), 6, 7010, "6c162d6588584aa1378b6f5cbc592d55f4776f24c5bd5ee56762af5f4082cf94"),
]


@pytest.mark.parametrize("bank,profile,mode,flags,months,seed,expected", PINNED,
                         ids=[f"{c[1]}-{c[0]}-{c[2]}" for c in PINNED])
def test_default_output_matches_pre_phase3_generator(
    tmp_path: Path, bank, profile, mode, flags, months, seed, expected
) -> None:
    assert sg._HOOKS is None
    with _deterministic(seed, profile):
        _, truth = sg.generate_statement(
            bank=bank, profile=profile, output_dir=tmp_path, statement_id=f"pin_{seed}",
            flags=list(flags), risk_mode=mode, n_months=months, seed=seed)
    assert hashlib.sha256(truth.read_bytes()).hexdigest() == expected


def test_corpus_mode_restores_hooks_on_exception() -> None:
    sentinel = object()
    with pytest.raises(RuntimeError):
        with sg.corpus_mode(sentinel):
            assert sg._HOOKS is sentinel
            raise RuntimeError("boom")
    assert sg._HOOKS is None


def test_default_today_is_wall_clock() -> None:
    assert sg._today() == date.today()


def test_new_txn_fields_default_to_none() -> None:
    t = sg._make_txn(date(2024, 1, 1), "X", debit=None, credit=None)
    assert (t.template_id, t.counterparty_key, t.txn_time) == (None, None, None)


def test_ml_only_injectors_are_not_in_the_default_registry() -> None:
    from tools.synthetic_corpus import ML_ONLY_INJECTORS

    assert not set(ML_ONLY_INJECTORS) & set(sg._FLAG_INJECTORS)
