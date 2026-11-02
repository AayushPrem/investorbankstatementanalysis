"""Deterministically (re)build the synthetic gold set in data/gold/.

The original 22-statement gold set was never committed (data/gold/** is
gitignored) and no record of its composition survived, so this script defines
the gold set explicitly in GOLD_MANIFEST and regenerates it from fixed seeds.

Determinism, without modifying tools/synthetic_gen.py:
  * generate_statement(seed=...) already seeds the `random` module.
  * The generator also calls uuid.uuid4() (narration hex refs, txn_ids), which
    `random.seed` does not control. During each call the generator's `uuid`
    module reference is swapped for a shim whose uuid4() draws from a
    random.Random seeded from the manifest entry.
  * reportlab embeds creation timestamps / document IDs in PDFs unless
    rl_config.invariant is set; it is set for the duration of the build.

Everything here is SYNTHETIC data from this repo's own generator.

Usage:
    python -m scripts.build_gold_set            # build missing files only
    python -m scripts.build_gold_set --force    # rebuild everything
    python -m scripts.build_gold_set --out DIR  # build into another directory
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import random
import shutil
import tempfile
import types
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

GOLD_DIR = Path(__file__).resolve().parents[1] / "data" / "gold"
N_MONTHS = 6


@dataclass(frozen=True)
class GoldEntry:
    bank: str
    profile: str
    seed: int
    flags: tuple[str, ...] = field(default_factory=tuple)
    # Only set for filenames that tests reference by name; everything else
    # gets a suffix derived from (bank, profile, seed).
    suffix: str | None = None

    @property
    def statement_id(self) -> str:
        sfx = self.suffix or hashlib.sha256(
            f"{self.bank}|{self.profile}|{self.seed}".encode()
        ).hexdigest()[:6]
        return f"{self.bank}_{self.profile}_{sfx}"


# Composition rules: every profile x both banks, every injector in
# synthetic_gen._FLAG_INJECTORS at least once, flags plausible for the profile
# (cf. _REALISTIC_FLAG_PROBS), and a clean statement per profile. Chosen once,
# up front; NOT tuned against regression results.
GOLD_MANIFEST: tuple[GoldEntry, ...] = (
    # Pinned names — referenced by tests/test_demo.py and test_validation_gating.py
    GoldEntry("hdfc",  "healthy_saas",    1001, (), suffix="5d2a8b"),
    GoldEntry("icici", "burning_startup", 1002, ("customer_churn", "founder_extraction"), suffix="4eb7a0"),
    # healthy_saas
    GoldEntry("hdfc",  "healthy_saas",    1003, ("revenue_concentration", "compliance_tds_gap")),
    GoldEntry("icici", "healthy_saas",    1004, ("related_party_leakage", "customer_churn")),
    GoldEntry("icici", "healthy_saas",    1005, ()),
    GoldEntry("icici", "healthy_saas",    1022, ("compliance_cash_loan", "compliance_rpt_concentration")),
    # burning_startup
    GoldEntry("hdfc",  "burning_startup", 1006, ("compliance_gst_gap", "compliance_cash_loan")),
    GoldEntry("hdfc",  "burning_startup", 1007, ("round_tripping",)),
    GoldEntry("icici", "burning_startup", 1008, ()),
    GoldEntry("hdfc",  "burning_startup", 1021, ("compliance_tds_gap", "customer_churn")),
    # services_firm
    GoldEntry("hdfc",  "services_firm",   1009, ("revenue_concentration", "compliance_rpt_concentration")),
    GoldEntry("icici", "services_firm",   1010, ("founder_extraction", "inter_account_transfer")),
    GoldEntry("hdfc",  "services_firm",   1011, ()),
    GoldEntry("icici", "services_firm",   1012, ("missing_month_gap",)),
    # ecommerce
    GoldEntry("hdfc",  "ecommerce",       1013, ("structuring", "compliance_pmla_cash")),
    GoldEntry("icici", "ecommerce",       1014, ("aggregator_dominated_revenue",)),
    GoldEntry("hdfc",  "ecommerce",       1015, ("fx_inflow",)),
    GoldEntry("icici", "ecommerce",       1016, ("compliance_269st", "related_party_leakage")),
    # restaurant
    GoldEntry("hdfc",  "restaurant",      1017, ("structuring", "compliance_269st")),
    GoldEntry("icici", "restaurant",      1018, ("compliance_pmla_cash", "founder_extraction")),
    GoldEntry("hdfc",  "restaurant",      1019, ()),
    GoldEntry("icici", "restaurant",      1020, ("round_tripping", "compliance_gst_gap")),
)


def expected_paths(out_dir: Path = GOLD_DIR) -> list[Path]:
    paths: list[Path] = []
    for e in GOLD_MANIFEST:
        paths.append(out_dir / f"{e.statement_id}.pdf")
        paths.append(out_dir / "labels" / f"{e.statement_id}.truth.json")
    return paths


def is_complete(out_dir: Path = GOLD_DIR) -> bool:
    return all(p.exists() for p in expected_paths(out_dir))


def _pinned_account_no(profile: str) -> str:
    """Stable per-profile account number.

    synthetic_gen._COMPANY_CONFIGS draws account_no from the *unseeded* global
    RNG at import time, so it differs per process. Within one process every
    statement of a profile shares it (one company per profile); deriving it
    from the profile name preserves exactly that semantics, reproducibly.
    """
    digest = int(hashlib.sha256(f"account|{profile}".encode()).hexdigest(), 16)
    return "50100" + str(100_000_000 + digest % 900_000_000)


@contextmanager
def _deterministic(seed: int, profile: str) -> Iterator[None]:
    """Make every non-seeded input to the generator reproducible for one call:
    uuid4() values, the import-time account number, and reportlab PDF metadata."""
    from reportlab import rl_config

    from tools import synthetic_gen

    rng = random.Random(seed * 7919 + 17)
    shim = types.SimpleNamespace(
        uuid4=lambda: uuid.UUID(int=rng.getrandbits(128), version=4)
    )
    saved_uuid = synthetic_gen.uuid
    saved_invariant = rl_config.invariant
    saved_company = synthetic_gen._COMPANY_CONFIGS[profile]
    synthetic_gen.uuid = shim  # type: ignore[assignment]
    rl_config.invariant = 1
    synthetic_gen._COMPANY_CONFIGS[profile] = dataclasses.replace(
        saved_company, account_no=_pinned_account_no(profile)
    )
    try:
        yield
    finally:
        synthetic_gen.uuid = saved_uuid
        rl_config.invariant = saved_invariant
        synthetic_gen._COMPANY_CONFIGS[profile] = saved_company


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(out_dir: Path = GOLD_DIR, *, force: bool = False, quiet: bool = False) -> list[Path]:
    """Build every manifest entry not already present (all of them if force).

    Returns the list of PDF paths written.
    """
    from tools.synthetic_gen import _FLAG_INJECTORS, generate_statement

    unknown = {f for e in GOLD_MANIFEST for f in e.flags} - set(_FLAG_INJECTORS)
    if unknown:
        raise ValueError(f"GOLD_MANIFEST references unknown flags: {sorted(unknown)}")

    labels_dir = out_dir / "labels"
    labels_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    with tempfile.TemporaryDirectory(prefix="gold_build_") as tmp:
        tmp_dir = Path(tmp)
        for e in GOLD_MANIFEST:
            sid = e.statement_id
            pdf_dst = out_dir / f"{sid}.pdf"
            truth_dst = labels_dir / f"{sid}.truth.json"
            if not force and pdf_dst.exists() and truth_dst.exists():
                continue
            with _deterministic(e.seed, e.profile):
                pdf_src, truth_src = generate_statement(
                    bank=e.bank,
                    profile=e.profile,
                    output_dir=tmp_dir,
                    statement_id=sid,
                    flags=list(e.flags),
                    risk_mode="manual",
                    n_months=N_MONTHS,
                    seed=e.seed,
                )
            shutil.move(str(pdf_src), pdf_dst)
            shutil.move(str(truth_src), truth_dst)
            written.append(pdf_dst)
            if not quiet:
                print(f"  {_sha256(pdf_dst)[:16]}  {pdf_dst.name}  flags={list(e.flags)}")

    if not quiet:
        print(f"Gold set: {len(written)} statement(s) written, "
              f"{len(GOLD_MANIFEST) - len(written)} already present -> {out_dir}")
    return written


def ensure(out_dir: Path = GOLD_DIR) -> None:
    """Build the gold set only if any expected file is missing (used by conftest)."""
    if not is_complete(out_dir):
        build(out_dir, quiet=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=GOLD_DIR)
    parser.add_argument("--force", action="store_true", help="rebuild even if files exist")
    args = parser.parse_args()
    build(args.out, force=args.force)


if __name__ == "__main__":
    main()
