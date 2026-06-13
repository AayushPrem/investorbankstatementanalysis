PYTHON := python
VENV := .venv
PIP := $(VENV)/Scripts/pip
PYTEST := $(VENV)/Scripts/pytest
RUFF := $(VENV)/Scripts/ruff
MYPY := $(VENV)/Scripts/mypy

.PHONY: install test lint typecheck regression all demo

install:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip
	$(PIP) install -e ".[dev]"

test:
	$(PYTEST) tests/ -v --ignore=tests/regression

lint:
	$(RUFF) check .

typecheck:
	$(MYPY) schema/ pipeline/ analysis/ adapters/ reports/ tools/ jurisdictions/

regression:
	$(PYTEST) tests/regression/ -v

all: lint typecheck test regression

demo:
	$(VENV)/Scripts/streamlit run demo/streamlit_app.py
