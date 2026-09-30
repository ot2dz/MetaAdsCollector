.DEFAULT_GOAL := help

# Use the local virtualenv by default; override with `make VENV=... PYTHON=...`
VENV ?= venv
PYTHON ?= python3
PY := $(VENV)/bin/python
PIP := $(PY) -m pip

.PHONY: help venv install install-dev dev run run-dev test test-cov lint typecheck format check build clean

## help: Show this help message
help:
	@echo 'Available targets:'
	@echo '  make venv         - Create the local virtual environment'
	@echo '  make install      - Install the package in editable mode'
	@echo '  make install-dev  - Install with dev dependencies'
	@echo '  make dev          - Install dev deps and run the web dashboard'
	@echo '  make run          - Run the web dashboard (http://127.0.0.1:5001)'
	@echo '  make run-dev      - Run with auto-reload (watch files) for development'
	@echo '  make test         - Run tests'
	@echo '  make test-cov     - Run tests with coverage report'
	@echo '  make lint         - Run ruff linter'
	@echo '  make typecheck    - Run mypy type checker'
	@echo '  make format       - Format code with ruff'
	@echo '  make check        - Run lint + typecheck + tests'
	@echo '  make build        - Build distribution packages'
	@echo '  make clean        - Remove build artifacts'

## venv: Create the local virtual environment
venv: $(VENV)/bin/activate

$(VENV)/bin/activate: pyproject.toml requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip

## install: Install the package in editable mode
install: venv
	$(PIP) install -e .

## install-dev: Install with all development dependencies
install-dev: venv
	$(PIP) install -e ".[dev]"
	$(PIP) install -r requirements.txt

## dev: Install dev dependencies and run the web dashboard locally
dev: install-dev run

## run: Run the web dashboard locally
run: venv
	$(PY) web_app.py

## run-dev: Run with auto-reload (watch files) for local development
run-dev: venv
	$(PY) -m flask --app web_app run --debug --host 127.0.0.1 --port 5001

## test: Run the test suite
test: venv
	$(PY) -m pytest

## test-cov: Run tests with coverage report
test-cov: venv
	$(PY) -m pytest --cov=meta_ads_collector --cov-report=term-missing --cov-report=html

## lint: Run ruff linter
lint: venv
	$(PY) -m ruff check .

## typecheck: Run mypy type checker
typecheck: venv
	$(PY) -m mypy meta_ads_collector/ --ignore-missing-imports

## format: Format code with ruff
format: venv
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

## check: Run all checks (lint, typecheck, tests)
check: lint typecheck test

## build: Build source and wheel distributions
build: venv
	$(PY) -m build

## clean: Remove build artifacts and caches
clean:
	rm -rf build/ dist/ *.egg-info meta_ads_collector.egg-info/
	rm -rf .pytest_cache/ .mypy_cache/ .ruff_cache/
	rm -rf htmlcov/ .coverage
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete 2>/dev/null || true
