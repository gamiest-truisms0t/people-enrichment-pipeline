# People-enrichment pipeline: developer entry points.
# Every target is safe to run repeatedly. AWS targets use the profile below.

SHELL := /bin/bash
.DEFAULT_GOAL := help

export AWS_PROFILE ?= enrich-dev
export AWS_REGION  ?= ap-southeast-1
export AWS_PAGER   :=

UV  ?= uv
ENV ?= dev

.PHONY: help setup lint fmt test check precommit run query login whoami clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python deps and git hooks
	$(UV) sync
	pre-commit install
	@echo "ok: run 'make check' to lint and test"

lint: ## Ruff lint + format check
	$(UV) run ruff check src tests
	$(UV) run ruff format --check src tests

fmt: ## Auto-format and fix lint
	$(UV) run ruff format src tests
	$(UV) run ruff check --fix src tests

test: ## Unit tests (no AWS, no network)
	$(UV) run pytest -m "not integration"

check: lint test ## Lint + test

precommit: ## Run every pre-commit hook on the whole tree
	pre-commit run --all-files

INPUT ?= data/sample/names.csv
OUT   ?= out

run: ## Run the pipeline locally with the mock provider (INPUT=..., OUT=...)
	$(UV) run enrich run --input $(INPUT) --provider mock --out $(OUT)

query: ## Answer the brief's three questions against local Parquet with DuckDB
	$(UV) run enrich query --out $(OUT)

login: ## Refresh the 12-hour AWS CLI session in the browser
	aws login --profile $(AWS_PROFILE)

whoami: ## Show which AWS identity the tools will use
	aws sts get-caller-identity

clean: ## Remove local build and test artefacts
	rm -rf build dist out .pytest_cache .ruff_cache .coverage htmlcov

# --- Added in later phases (see PLAN.md) --------------------------------------
# package      Build Lambda zips for arm64 with uv (Phase 2)
# bootstrap    Create the Terraform state bucket (Phase 2)
# plan/apply   terraform plan/apply for infra/envs/$(ENV) (Phase 2)
# destroy      terraform destroy for infra/envs/$(ENV) (Phase 2)
# set-api-key  Push ~/.config/people-enrichment/pdl_api_key into SSM (Phase 2)
# upload       Upload a CSV to the landing bucket to trigger a run (Phase 3)
# rebuild      Rebuild curated tables from raw/ without spending credits (Phase 3)
# e2e          Upload the sample file and wait for the execution to finish (Phase 6)
