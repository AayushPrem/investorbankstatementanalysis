# Cross-platform Makefile (Linux/macOS, and Windows via Git Bash/MSYS make).
# Every tool is invoked as `python -m <tool>` from the venv interpreter, so
# only the interpreter path differs between platforms.

VENV := .venv
ifeq ($(OS),Windows_NT)
    VENV_BIN := $(VENV)/Scripts
    BOOTSTRAP_PY ?= python
else
    VENV_BIN := $(VENV)/bin
    BOOTSTRAP_PY ?= python3
endif
PY := $(VENV_BIN)/python

# Packages gated by mypy in CI (must be clean). Widen as errors are fixed;
# see docs/CHANGELOG_UPGRADE.md for the current remaining scope.
TYPECHECK_GATED := schema/ pipeline/ config/ llm/ db/ api/ worker/ datasets/ ml/
TYPECHECK_ALL   := schema/ pipeline/ config/ llm/ db/ api/ worker/ datasets/ ml/ analysis/ adapters/ reports/ tools/ jurisdictions/

.PHONY: install gold test lint typecheck typecheck-all regression all demo migrate api worker corpus corpus-small categoriser-audit categoriser-train categoriser-eval

install:
	$(BOOTSTRAP_PY) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev,ocr,embeddings,platform,ml]"

gold:
	$(PY) -m scripts.build_gold_set

test:
	$(PY) -m pytest tests/ -q --ignore=tests/regression

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy --follow-imports=silent $(TYPECHECK_GATED)

# Informational: full-scope type check (not yet clean, not gated).
typecheck-all:
	-$(PY) -m mypy $(TYPECHECK_ALL)

regression:
	$(PY) -m pytest tests/regression/ -q

all: lint typecheck test regression

demo:
	$(PY) -m streamlit run demo/app.py

# ── Data platform (Phase 2). Needs DATABASE_URL; see .env.example / docker-compose.yml.
migrate:
	$(PY) -m alembic upgrade head

api:
	$(PY) -m uvicorn api.main:app --reload

worker:
	$(PY) -m worker

# ── Synthetic corpus (Phase 3). SYNTHETIC data; see docs/ml/corpus_card.md.
corpus:
	$(PY) -m datasets.build_corpus --n-companies 1000 --seed 42
	$(PY) -m datasets.splits data/corpus/v1 --seed 42

corpus-small:
	$(PY) -m datasets.build_corpus --small --seed 42
	$(PY) -m datasets.splits data/corpus/v1-small --seed 42

# ── Hybrid categoriser (Phase 4). SYNTHETIC data; see docs/ml/categoriser.md.
categoriser-audit:
	$(PY) -m ml.categoriser.rule_audit data/corpus/v1

categoriser-train:
	$(PY) -m ml.categoriser.train data/corpus/v1 --split all

categoriser-eval:
	$(PY) -m ml.categoriser.evaluate data/corpus/v1 --write-thresholds
