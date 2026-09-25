# People-enrichment pipeline: developer entry points.
# Every target is safe to run repeatedly. AWS targets use the profile below.

SHELL := /bin/bash
.DEFAULT_GOAL := help

export AWS_PROFILE ?= enrich-dev
export AWS_REGION  ?= ap-southeast-1
export AWS_PAGER   :=

UV  ?= uv
ENV ?= dev

.PHONY: help setup lint fmt test check precommit run query login whoami clean \
        package bootstrap init plan apply destroy tf-lint set-api-key upload smoke \
        e2e executions rebuild asl-validate

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

INPUT    ?= data/sample/names.csv
OUT      ?= out
PROVIDER ?= mock
SANDBOX  ?= 0

run: ## Run the pipeline locally (PROVIDER=mock|pdl, SANDBOX=1 for PDL's free sandbox, INPUT=..., OUT=...)
	$(UV) run enrich run --input $(INPUT) --provider $(PROVIDER) $(if $(filter 1,$(SANDBOX)),--sandbox,) --out $(OUT)

record-fixtures: ## Record synthetic PDL sandbox responses into tests/fixtures/pdl (zero credits, ~90 s)
	$(UV) run python scripts/record_pdl_fixtures.py

query: ## Answer the brief's three questions against local Parquet with DuckDB
	$(UV) run enrich query --out $(OUT)

login: ## Refresh the 12-hour AWS CLI session in the browser
	aws login --profile $(AWS_PROFILE)

whoami: ## Show which AWS identity the tools will use
	aws sts get-caller-identity

clean: ## Remove local build and test artefacts
	rm -rf build dist out .pytest_cache .ruff_cache .coverage htmlcov

# --- Packaging ----------------------------------------------------------------
# One zip serves all three functions. Dependencies come from uv.lock as arm64
# manylinux wheels (no local compilation, reproducible). boto3 is excluded because
# the Lambda runtime ships it; everything else, including Powertools, is vendored.

PYTHON_PLATFORM ?= aarch64-manylinux2014
PYTHON_VERSION  ?= 3.13

package: ## Build build/lambda (arm64 wheels pinned from uv.lock) for Terraform to zip
	rm -rf build/lambda build/lambda.zip && mkdir -p build/lambda
	$(UV) export --no-dev --no-hashes --no-emit-project --no-header --format requirements-txt \
	  | grep -viE '^(boto3|botocore|s3transfer|jmespath)==' > build/requirements.txt
	$(UV) pip install --quiet --no-deps --only-binary :all: --python $(PYTHON_VERSION) \
	  --python-platform $(PYTHON_PLATFORM) --python-version $(PYTHON_VERSION) \
	  --target build/lambda -r build/requirements.txt
	cp -R src/enrich_pipeline build/lambda/enrich_pipeline
	find build/lambda -type d -name '__pycache__' -prune -exec rm -rf {} +
	@echo "package: $$(du -sh build/lambda | cut -f1) unzipped in build/lambda"

# --- Terraform ----------------------------------------------------------------
TF_BOOTSTRAP := terraform -chdir=infra/bootstrap
TF_ENV       := terraform -chdir=infra/envs/$(ENV)
STATE_BUCKET  = $(shell terraform -chdir=infra/bootstrap output -raw state_bucket 2>/dev/null)
export TF_PLUGIN_CACHE_DIR ?= $(HOME)/.terraform.d/plugin-cache

bootstrap: ## Create the Terraform state bucket (once; this stack keeps local state)
	@mkdir -p $(TF_PLUGIN_CACHE_DIR)
	$(TF_BOOTSTRAP) init -input=false
	$(TF_BOOTSTRAP) apply -input=false -auto-approve

init: ## Initialise infra/envs/$(ENV) against the state bucket (native S3 locking)
	@test -n "$(STATE_BUCKET)" || { echo "no state bucket yet: run 'make bootstrap' first"; exit 1; }
	@mkdir -p $(TF_PLUGIN_CACHE_DIR)
	$(TF_ENV) init -input=false -reconfigure \
	  -backend-config="bucket=$(STATE_BUCKET)" \
	  -backend-config="key=$(ENV)/terraform.tfstate" \
	  -backend-config="region=$(AWS_REGION)" \
	  -backend-config="use_lockfile=true" \
	  -backend-config="encrypt=true"

plan: package ## terraform plan for infra/envs/$(ENV)
	$(TF_ENV) plan -input=false

apply: package ## terraform apply for infra/envs/$(ENV)
	$(TF_ENV) apply -input=false -auto-approve

destroy: ## terraform destroy for infra/envs/$(ENV) (dev buckets are force_destroy)
	$(TF_ENV) destroy -input=false -auto-approve

tf-lint: ## terraform fmt -check, validate, tflint and checkov over infra/
	terraform fmt -check -recursive infra
	@for d in infra/bootstrap infra/envs/dev; do \
	  terraform -chdir=$$d init -backend=false -input=false >/dev/null && terraform -chdir=$$d validate || exit 1; \
	done
	@tflint --init >/dev/null
	@for d in infra/bootstrap infra/envs/dev infra/modules/*; do tflint --chdir=$$d --config "$(CURDIR)/.tflint.hcl" || exit 1; done
	checkov --config-file .checkov.yaml -d infra

# --- Operating the deployed stack -----------------------------------------------
set-api-key: ## Push ~/.config/people-enrichment/pdl_api_key into the SSM SecureString
	@aws ssm put-parameter --name "$$($(TF_ENV) output -raw pdl_api_key_parameter)" \
	  --type SecureString --overwrite \
	  --value "$$(cat $(HOME)/.config/people-enrichment/pdl_api_key)" >/dev/null && echo "api key stored in SSM"

upload: ## Copy INPUT to the landing bucket under incoming/<timestamp>/
	aws s3 cp $(INPUT) "s3://$$($(TF_ENV) output -raw landing_bucket)/incoming/$$(date -u +%Y%m%dT%H%M%SZ)/$$(basename $(INPUT))"

smoke: ## Drive validate -> enrich -> build-curated by hand in AWS on INPUT
	scripts/smoke.sh $(INPUT)

e2e: ## Upload INPUT and follow the Step Functions execution the upload triggers
	scripts/e2e.sh $(INPUT)

executions: ## List the five most recent pipeline executions
	aws stepfunctions list-executions --state-machine-arn "$$($(TF_ENV) output -raw state_machine_arn)" \
	  --max-results 5 --no-paginate --query 'executions[].[status,startDate,name]' --output table

report: ## Per-row outcomes, scores and provider credit headers for BATCH=<batch_id>, read from S3
	@test -n "$(BATCH)" || { echo "usage: make report BATCH=<batch_id>"; exit 1; }
	$(UV) run python scripts/batch_report.py --bucket "$$($(TF_ENV) output -raw data_bucket)" --batch $(BATCH)

rebuild: ## Rebuild the curated tables for BATCH=<batch_id> from stored results (no provider calls)
	@test -n "$(BATCH)" || { echo "usage: make rebuild BATCH=<batch_id>"; exit 1; }
	aws lambda invoke --function-name "$$($(TF_ENV) output -json function_names | jq -r .build_curated)" \
	  --cli-binary-format raw-in-base64-out --payload '{"batch_id":"$(BATCH)"}' /dev/stdout

asl-validate: ## Validate the state machine definition with the Step Functions API (renders the template with dummy ARNs)
	@mkdir -p build
	@sed -e 's/$${validate_input_arn}/arn:aws:lambda:ap-southeast-1:123456789012:function:validate/' \
	     -e 's/$${enrich_arn}/arn:aws:lambda:ap-southeast-1:123456789012:function:enrich/' \
	     -e 's/$${build_curated_arn}/arn:aws:lambda:ap-southeast-1:123456789012:function:build/' \
	     -e 's/$${alerts_topic_arn}/arn:aws:sns:ap-southeast-1:123456789012:alerts/' \
	     -e 's/$${max_concurrency}/1/' \
	     infra/modules/orchestration/pipeline.asl.tftpl > build/pipeline.asl.tftpl
	aws stepfunctions validate-state-machine-definition --type STANDARD --definition file://build/pipeline.asl.tftpl
