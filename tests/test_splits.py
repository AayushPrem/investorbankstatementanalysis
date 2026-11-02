"""Phase 3 split files: determinism and leakage invariants."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pandas as pd
import pytest

from datasets.build_corpus import build
from datasets.splits import HORIZON_DAYS, build_splits, eligible_cutoffs, is_generic_key


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Path:
    out = tmp_path_factory.mktemp("splits") / "v1-test"
    build(n_companies=40, seed=7, out_dir=out, max_months=24, pdf_subset=0, workers=1,
          quiet=True)
    build_splits(out, seed=7, quiet=True)
    return out


def _read(corpus: Path, name: str) -> pd.DataFrame:
    return pd.read_parquet(corpus / "splits" / f"{name}.parquet")


def test_splits_are_deterministic(corpus: Path) -> None:
    import json

    first = json.loads((corpus / "splits" / "manifest.json").read_text(encoding="utf-8"))
    again = build_splits(corpus, seed=7, quiet=True)
    assert again["splits_hash"] == first["splits_hash"]


def test_company_partition_is_disjoint_and_complete(corpus: Path) -> None:
    df = _read(corpus, "company")
    companies = pd.read_parquet(corpus / "features" / "companies.parquet")
    assert sorted(df["company_id"]) == sorted(companies["company_id"])
    assert df["company_id"].is_unique
    assert set(df["split"]) == {"train", "val", "test"}


def test_cross_bank_tests_on_icici_only(corpus: Path) -> None:
    df = _read(corpus, "categoriser_cross_bank")
    banks = pd.read_parquet(corpus / "features" / "companies.parquet")[["company_id", "bank"]]
    m = df.merge(banks, on="company_id")
    assert set(m.loc[m["split"] == "test", "bank"]) == {"icici"}
    assert set(m.loc[m["split"] != "test", "bank"]) == {"hdfc"}


def test_unseen_vendor_test_keys_never_in_train(corpus: Path) -> None:
    df = _read(corpus, "categoriser_unseen_vendor")
    assert df["counterparty_key"].is_unique
    assert not df.loc[df["generic"], "split"].ne("train").any()
    assert (df["split"] == "test").sum() > 0
    assert not df.loc[df["split"] == "test", "counterparty_key"].map(is_generic_key).any()


def test_churn_units_are_temporal_and_company_disjoint(corpus: Path) -> None:
    df = _read(corpus, "churn_temporal")
    part = _read(corpus, "company").set_index("company_id")["split"]
    assert (df["split"] == df["company_id"].map(part)).all()
    h = timedelta(days=HORIZON_DAYS)
    tr, va, te = (df[df["split"] == s]["cutoff_date"] for s in ("train", "val", "test"))
    assert len(tr) and len(te)
    # every train label window closes before any val/test cutoff
    assert (tr + h).max() < min(va.min() if len(va) else te.min(), te.min())
    if len(va):
        assert (va + h).max() < te.min()


def test_eligible_cutoffs_respect_history_and_horizon() -> None:
    from datetime import date

    cs = eligible_cutoffs(date(2023, 1, 1), date(2023, 12, 31))
    assert cs[0] == date(2023, 6, 30)
    assert cs[-1] + timedelta(days=HORIZON_DAYS) <= date(2023, 12, 31)


def test_segmentation_holdout(corpus: Path) -> None:
    df = _read(corpus, "segmentation_holdout")
    assert set(df["split"]) == {"fit", "holdout"}
    assert df["company_id"].is_unique
