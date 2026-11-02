# BPO Adriene — developer entry points.
#
#   make install   create .venv and install from the lockfile
#   make test      run both suites (tests/ and backend/tests/)
#   make lint      ruff
#   make type      mypy (agent, server, backend/app)
#   make build     build the React console the backend serves
#   make run       start the bot on :8055
#   make check     readiness report, no server
#   make lock      re-freeze requirements-lock.txt
#
# Every target works from a fresh clone.

PY ?= python3
VENV := .venv
PY_BIN := $(VENV)/bin/python
PIP := $(PY_BIN) -m pip

.DEFAULT_GOAL := help
.PHONY: help install build test test-frontend lint type fmt verify check run lock clean env

help:
	@grep -E '^#   make' Makefile | sed 's/^#   //'

## install: create the virtualenv, install the pinned set plus dev tooling
install:
	@test -x $(PY_BIN) || $(PY) -m venv $(VENV)
	@$(PIP) install --upgrade pip
	@$(PIP) install -r requirements-dev.txt
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example — set SECRET_KEY before starting")

## build: build the console into frontend/build (served by the backend)
build:
	@cd frontend && (test -d node_modules || npm install) && npm run build

## test: the full suite — both directories, verbose
test:
	@$(PY_BIN) -m pytest tests/ backend/tests/ -v

## test-frontend: the console's Vitest suite
test-frontend:
	@cd frontend && (test -d node_modules || npm install) && npm test --silent

## verify: every gate CI runs, in one command (check is the readiness report)
verify: lint type test build test-frontend

## lint: the same gate CI enforces
lint:
	@$(PY_BIN) -m ruff check .

## type: static type check (the same scope CI enforces)
type:
	@$(PY_BIN) -m mypy

## fmt: format the tree
fmt:
	@$(PY_BIN) -m ruff format .

## check: readiness report (keys, voices, audio) without starting a server
check:
	@$(PY_BIN) scripts/healthcheck.py

## run: start the bot on :8055 (bootstraps if needed)
run:
	@./start.sh

## lock: re-freeze the lockfile from requirements.txt in a scratch venv
lock:
	@rm -rf .lock-venv && $(PY) -m venv .lock-venv
	@.lock-venv/bin/pip install --upgrade pip >/dev/null
	@.lock-venv/bin/pip install -r requirements.txt
	@.lock-venv/bin/pip freeze --exclude-editable > requirements-lock.txt
	@rm -rf .lock-venv
	@echo "requirements-lock.txt now pins $$(wc -l < requirements-lock.txt) packages"

## env: create .env from the example
env:
	@test -f .env || cp .env.example .env

## clean: remove build artefacts and caches (keeps .venv and .env)
clean:
	@rm -rf frontend/build .pytest_cache .ruff_cache
	@find . -name __pycache__ -type d -prune -exec rm -rf {} +
