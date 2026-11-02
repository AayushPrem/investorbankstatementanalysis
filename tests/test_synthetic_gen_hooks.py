"""Phase 3: the corpus-mode hooks in tools/synthetic_gen.py are inert by default.

The pinned hashes below were produced by the pre-Phase-3 generator (commit
37dd801) and re-checked against the hooked generator in the same session; any
change to the default-argument RNG stream or output breaks them.

The truth JSON is written with Path.write_text (CRLF on Windows, LF on
Linux), so the hash is taken over the LF-normalised bytes.
"""
from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from scripts.build_gold_set import _deterministic
from tools import synthetic_gen as sg

# (bank, profile, risk_mode, flags, n_months, seed, sha256 of the LF-normalised truth JSON)
PINNED = [
    ("hdfc", "healthy_saas", "realistic", (), 6, 7001, "c89b4132e35ecdcbb914545d0a99cf4002d5b0061c6ccb057b97e98dcffb8364"),
    ("icici", "healthy_saas", "manual", ("customer_churn", "revenue_concentration"), 6, 7002, "e2bd6bd1a09513c71ffcff623d5bace03b1ec8a06e25aade5d6763ac53f2b556"),
    ("hdfc", "burning_startup", "realistic", (), 12, 7003, "c196f98e1dc10c2cd6bcab8b231945d8adefdf033bb910bc8b9061399cb9e9f9"),
    ("icici", "burning_startup", "manual", ("founder_extraction", "compliance_gst_gap"), 6, 7004, "30b1b27b5a1e3b45cb699c79d95018338ea9c8541296fcb21a638f8c13ea2e1d"),
    ("hdfc", "services_firm", "realistic", (), 12, 7005, "b5db8a3815d9f3c10456cdde7f293a846735b1668e7e39c1d95d300851e5a4ff"),
    ("icici", "services_firm", "manual", ("round_tripping", "missing_month_gap"), 6, 7006, "e002c148a9750dcdd0993f20e49af1e566f23aa8060e5e1241b008e198db7a98"),
    ("hdfc", "ecommerce", "manual", ("structuring", "aggregator_dominated_revenue"), 6, 7007, "a6cb25148f4696fa91acfbc84a231ba0f541a6d65ec73d438424678d410a2e5a"),
    ("icici", "ecommerce", "clean", (), 3, 7008, "de42a89cb79b0e66c81b1d562e84b2159c767f5e714683c3d2db9cbe2d7e01eb"),
    ("hdfc", "restaurant", "manual", ("compliance_pmla_cash", "fx_inflow"), 6, 7009, "3295ec7202c87103bd805d2fe9e9d5b01580d0761ff3f57ca2926381f4363cc6"),
    ("icici", "restaurant", "realistic", (), 6, 7010, "0eebf26bab9cc88c1f04849d222c2f70401da8219967fdbab03b5c4eb1c5e6a2"),
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
    lf_bytes = truth.read_bytes().replace(b"\r\n", b"\n")
    assert hashlib.sha256(lf_bytes).hexdigest() == expected


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
