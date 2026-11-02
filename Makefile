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
TYPECHECK_GATED := schema/ pipeline/ config/ llm/
TYPECHECK_ALL   := schema/ pipeline/ config/ llm/ analysis/ adapters/ reports/ tools/ jurisdictions/

.PHONY: install gold test lint typecheck typecheck-all regression all demo

install:
	$(BOOTSTRAP_PY) -m venv $(VENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -e ".[dev,ocr,embeddings]"

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
