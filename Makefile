SHELL := /usr/bin/env bash

PYTHON ?= python3
PYTHON_ENV ?= .venv/bin/python
GO ?= go
GO_DIR := go
PYTHON_DIR := python
VERSION := $(shell tr -d '[:space:]' < VERSION)

.PHONY: help contracts contracts-check acceptance python-install python-test python-lint python-typecheck \
	python-coverage go-format go-lint go-test go-race go-coverage build test lint typecheck \
	coverage check bench clean

help:
	@sed -n 's/^## //p' Makefile

## Regenerate the canonical contract from Python schemas and sync the Go embed.
contracts:
	PROJECT_ROOT=$(CURDIR) PYTHONPATH=$(PYTHON_DIR)/src $(PYTHON_ENV) contracts/acceptance/export_python_contract.py
	cp contracts/tool-schemas/tools.json $(GO_DIR)/internal/contracts/tools.json

## Verify contract copies, versions, and live Python schema without changing files.
contracts-check:
	PROJECT_ROOT=$(CURDIR) PYTHONPATH=$(PYTHON_DIR)/src $(PYTHON_ENV) scripts/check_contracts.py

acceptance: build
	$(PYTHON_ENV) contracts/acceptance/run_dual_server.py

## Install the Python implementation with all development dependencies.
python-install:
	$(PYTHON_ENV) -m pip install -e './$(PYTHON_DIR)[dev]'

python-test:
	PROJECT_ROOT=$(CURDIR) PYTHONPATH=$(PYTHON_DIR)/src $(PYTHON_ENV) -m pytest -q $(PYTHON_DIR)/tests

python-lint:
	ruff check $(PYTHON_DIR) contracts/acceptance scripts

python-typecheck:
	MYPYPATH=$(PYTHON_DIR)/src mypy --config-file $(PYTHON_DIR)/pyproject.toml $(PYTHON_DIR)/src

python-coverage:
	PROJECT_ROOT=$(CURDIR) PYTHONPATH=$(PYTHON_DIR)/src $(PYTHON_ENV) -m coverage run --source=$(PYTHON_DIR)/src/chatrepo_mcp -m pytest -q $(PYTHON_DIR)/tests
	$(PYTHON_ENV) -m coverage report --fail-under=80
	$(PYTHON_ENV) -m coverage json -o python-coverage.json
	$(PYTHON_ENV) scripts/check_python_coverage.py python-coverage.json --overall 80

go-format:
	@test -z "$$(gofmt -l $(GO_DIR))"

go-lint:
	$(GO) -C $(GO_DIR) vet ./...
	@if command -v staticcheck >/dev/null 2>&1; then cd $(GO_DIR) && staticcheck ./...; else echo 'staticcheck not installed; skipped locally'; fi
	@if command -v govulncheck >/dev/null 2>&1; then cd $(GO_DIR) && govulncheck ./...; else echo 'govulncheck not installed; skipped locally'; fi

go-test:
	$(GO) -C $(GO_DIR) test ./...

go-race:
	$(GO) -C $(GO_DIR) test -race ./...

go-coverage:
	$(GO) -C $(GO_DIR) test -coverprofile=coverage.out ./...
	$(GO) -C $(GO_DIR) tool cover -func=coverage.out | $(PYTHON) scripts/check_go_coverage.py --minimum 80
	$(GO) -C $(GO_DIR) test -coverprofile=coverage-security.out ./internal/security
	$(GO) -C $(GO_DIR) tool cover -func=coverage-security.out | $(PYTHON) scripts/check_go_coverage.py --minimum 90

## Build the Go binary into bin/chatrepo-mcp.
build:
	mkdir -p bin
	CGO_ENABLED=0 $(GO) -C $(GO_DIR) build -trimpath -ldflags "-s -w -X github.com/nssanta/ChatGPT-Repo-MCP/go/internal/app.Version=$(VERSION)" -o ../bin/chatrepo-mcp ./cmd/chatrepo-mcp

test: python-test go-test contracts-check

lint: python-lint go-format go-lint

typecheck: python-typecheck

coverage: python-coverage go-coverage

## Full local quality gate used by CI.
check: contracts-check lint typecheck test coverage build acceptance

bench:
	$(GO) -C $(GO_DIR) test -run '^$$' -bench . -benchmem ./internal/tools

clean:
	rm -rf bin .coverage htmlcov python-coverage.json $(GO_DIR)/coverage.out \
		$(GO_DIR)/coverage-security.out $(PYTHON_DIR)/build $(PYTHON_DIR)/dist \
		$(PYTHON_DIR)/src/*.egg-info
