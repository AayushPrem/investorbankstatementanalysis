"""Shared pytest setup.

Gold set: data/gold/ is gitignored, so it is regenerated deterministically by
scripts/build_gold_set.py when any expected file is missing (a no-op when the
set is already present). This runs in pytest_configure rather than a fixture
because tests/test_demo.py parametrizes over data/gold/*.pdf at *collection*
time, before any fixture would execute.
"""
from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    # Only the controller process builds (avoids races under pytest-xdist).
    if hasattr(config, "workerinput"):
        return
    from scripts.build_gold_set import ensure

    ensure()
