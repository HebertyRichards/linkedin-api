# Requires GNU Make (Windows: "winget install ezwinports.make" or "choco install make").
ifeq ($(OS),Windows_NT)
    VENV_PY := venv/Scripts/python.exe
    SYS_PY  := python
else
    VENV_PY := venv/bin/python
    SYS_PY  := python3
endif

PY := $(VENV_PY)

.PHONY: help venv install run lint format typecheck test check lock

help:
	@echo "make install     Create the venv if needed and install dependencies"
	@echo "make run         Run the worker (reads .env)"
	@echo "make lint        ruff check"
	@echo "make format      ruff format"
	@echo "make typecheck   mypy (strict)"
	@echo "make test        pytest"
	@echo "make check       lint + typecheck + test"
	@echo "make lock        Regenerate requirements.txt from requirements.in"

$(VENV_PY):
	$(SYS_PY) -m venv venv

venv: $(VENV_PY)

install: $(VENV_PY)
	$(PY) -m pip install -r requirements-dev.txt

run:
	$(PY) -m app.main

lint:
	$(PY) -m ruff check app/ tests/
	$(PY) -m ruff format --check app/ tests/

format:
	$(PY) -m ruff format app/ tests/

typecheck:
	$(PY) -m mypy app/ tests/

test:
	$(PY) -m pytest -q

check: lint typecheck test

lock:
	$(PY) -m pip install -q uv
	$(PY) -m uv pip compile requirements.in -o requirements.txt --universal --python-version 3.13
