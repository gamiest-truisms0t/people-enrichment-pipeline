# People-enrichment pipeline: developer entry points.
# Every target is safe to run repeatedly. AWS targets use the profile below.

SHELL := /bin/bash
.DEFAULT_GOAL := help

# The named profile is for laptops; CI runners get credentials from OIDC (GitHub sets CI=true).
ifeq ($(CI),)
export AWS_PROFILE ?= enrich-dev
endif
export AWS_REGION  ?= ap-southeast-1
export AWS_PAGER   :=

UV  ?= uv
ENV ?= dev

.PHONY: help setup lint fmt test coverage check precommit run query login whoami clean \
        package bootstrap init plan apply destroy tf-lint set-api-key upload smoke \
        e2e executions rebuild asl-validate report record-fixtures glue-columns athena-verify \
        idempotency-proof iam-check branch-protection quarantine quarantine-get redrive ci-config \
        rebuild-all validate input-contract repo-settings release demo tf-docs tf-docs-check

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

test: ## Unit, provider-contract and handler tests (no AWS account, no network)
	$(UV) run pytest -m "not integration"

coverage: ## The same tests with line coverage (floor 90 %, as in CI); HTML report in htmlcov/
	$(UV) run pytest -m "not integration" --cov=enrich_pipeline --cov-report=term-missing \
	  --cov-report=html --cov-fail-under=90

check: lint test ## Lint + test

precommit: ## Run every pre-commit hook on the whole tree
	pre-commit run --all-files

INPUT    ?= data/sample/names.csv
OUT      ?= out
PROVIDER ?= mock
SANDBOX  ?= 0
# The AWS targets (upload, smoke, e2e) default to the cached public-figure demo file, which
# costs no credits, instead of the mock sample whose name-only rows would spend identify
# credits against the live provider. INPUT=... still overrides.
AWS_INPUT = $(if $(filter data/sample/names.csv,$(INPUT)),data/demo/idempotency.csv,$(INPUT))

run: ## Run the pipeline locally (PROVIDER=mock|pdl, SANDBOX=1 for PDL's free sandbox, INPUT=..., OUT=...)
	$(UV) run enrich run --input $(INPUT) --provider $(PROVIDER) $(if $(filter 1,$(SANDBOX)),--sandbox,) --out $(OUT)

record-fixtures: ## Record synthetic PDL sandbox responses into tests/fixtures/pdl (zero credits, ~90 s)
	$(UV) run python scripts/record_pdl_fixtures.py

glue-columns: ## Regenerate infra/modules/catalog/columns.json from src/enrich_pipeline/schema.py
	$(UV) run python scripts/glue_columns.py

ROWS ?= 12

athena-verify: ## Run the saved Athena queries in the pipeline workgroup and print the first ROWS rows
	scripts/athena_verify.sh $(ROWS)

IDEMPOTENCY_INPUT ?= data/demo/idempotency.csv

idempotency-proof: ## Run IDEMPOTENCY_INPUT twice through AWS; the second run must be all cached at zero credits
	scripts/idempotency_proof.sh $(IDEMPOTENCY_INPUT)

iam-check: ## List wildcard-resource statements and run IAM Access Analyzer over every project role
	scripts/iam_check.sh people-enrichment-$(ENV)

branch-protection: ## Require the CI checks on main (.github/branch-protection.json) via the GitHub API
	gh api -X PUT repos/{owner}/{repo}/branches/main/protection --input .github/branch-protection.json \
	  --jq '"required checks: " + (.required_status_checks.contexts | join(", "))'

repo-settings: ## Apply the repository's GitHub settings (description, topics, security features, CodeQL) via the API
	scripts/repo_settings.sh

release: ## Tag TAG=vX.Y.Z if needed, push it and publish the GitHub Release from its CHANGELOG section
	scripts/release.sh $(TAG)

quarantine: ## List quarantined uploads (with their rejection reason) and rejected-row exports
	scripts/quarantine.sh list

quarantine-get: ## Download a quarantined object: KEY=quarantine/files/<name> [DEST=.]
	@test -n "$(KEY)" || { echo "usage: make quarantine-get KEY=quarantine/files/<name> [DEST=.]"; exit 1; }
	scripts/quarantine.sh get "$(KEY)" "$(or $(DEST),.)"

redrive: ## Re-run a quarantined upload as-is (new object under incoming/redrive/): KEY=quarantine/files/<name>
	@test -n "$(KEY)" || { echo "usage: make redrive KEY=quarantine/files/<name>"; exit 1; }
	scripts/quarantine.sh redrive "$(KEY)"

# GitHub Actions deploys through OIDC roles created by the bootstrap stack. This target
# copies what the workflows need into the repository's Actions variables (role ARNs, region,
# state bucket, the tfvars that are not secrets) and one secret (the alert email). Run it in
# your own terminal after `make bootstrap`; it reads infra/envs/dev/terraform.tfvars.
ci-config: ## Set the GitHub Actions variables and the alert-email secret for the Terraform workflows
	@tfvar() { awk -v k="$$1" -F= '$$1 ~ "^[[:space:]]*"k"[[:space:]]*$$" { v=$$2; gsub(/^[[:space:]"]+|[[:space:]"]+$$/, "", v); print v; exit }' infra/envs/dev/terraform.tfvars; }; \
	set -e; \
	gh variable set AWS_REGION --body "$(AWS_REGION)"; \
	gh variable set TF_STATE_BUCKET --body "$$($(TF_BOOTSTRAP) output -raw state_bucket)"; \
	gh variable set AWS_PLAN_ROLE_ARN --body "$$($(TF_BOOTSTRAP) output -json github_role_arns | jq -r .plan)"; \
	gh variable set AWS_READONLY_ROLE_ARN --body "$$($(TF_BOOTSTRAP) output -json github_role_arns | jq -r .readonly)"; \
	gh variable set AWS_APPLY_ROLE_ARN --body "$$($(TF_BOOTSTRAP) output -json github_role_arns | jq -r .apply)"; \
	for k in provider_name owner pdl_sandbox; do v="$$(tfvar $$k)"; [ -n "$$v" ] && gh variable set "TF_VAR_$$(echo $$k | tr a-z A-Z)" --body "$$v" || true; done; \
	email="$$(tfvar alert_email)"; test -n "$$email" || { echo "alert_email missing from terraform.tfvars"; exit 1; }; \
	gh secret set TF_VAR_ALERT_EMAIL --body "$$email"; \
	echo "ok: variables and the alert-email secret are set (gh variable list / gh secret list)"

query: ## Answer the brief's three questions against local Parquet with DuckDB
	$(UV) run enrich query --out $(OUT)

demo: ## Offline demo: the sample file through the mock provider, the guards' dry run on a messy export, the three questions
	rm -rf out/demo
	@echo "== data/sample/names.csv through the mock provider: match, ambiguity, a rate limit, a cache hit, an invalid row"
	$(UV) run enrich run --input data/sample/names.csv --provider mock --out out/demo
	@echo; echo "== data/sample/dirty.csv through the input guards only: what a messy export would get"
	-$(UV) run enrich validate --input data/sample/dirty.csv
	@echo; echo "== the brief's three questions, answered from out/demo with DuckDB"
	$(UV) run enrich query --out out/demo

login: ## Refresh the 12-hour AWS CLI session in the browser
	aws login --profile $(AWS_PROFILE)

whoami: ## Show which AWS identity the tools will use
	aws sts get-caller-identity

clean: ## Remove local build and test artefacts
	rm -rf build dist out .pytest_cache .ruff_cache .coverage htmlcov

# --- Packaging ----------------------------------------------------------------
# One zip serves all three functions. Dependencies come from uv.lock as arm64
# manylinux wheels (no local compilation). boto3 is excluded because the Lambda runtime
# ships it; everything else, including Powertools, is vendored.
#
# The build is reproducible across machines: Terraform's archive_file already fixes the
# zip timestamps, and the only machine-specific content, the console scripts whose shebang
# names the installing interpreter's path, is removed together with the RECORD lines that
# hash them. A laptop `make plan` after a CI deploy of the same commit shows no function
# changes.

PYTHON_PLATFORM ?= aarch64-manylinux2014
PYTHON_VERSION  ?= 3.13

package: ## Build build/lambda (arm64 wheels pinned from uv.lock, reproducible) for Terraform to zip
	rm -rf build/lambda build/lambda.zip && mkdir -p build/lambda
	$(UV) export --no-dev --no-hashes --no-emit-project --no-header --format requirements-txt \
	  | grep -viE '^(boto3|botocore|s3transfer|jmespath)==' > build/requirements.txt
	$(UV) pip install --quiet --no-deps --only-binary :all: --python $(PYTHON_VERSION) \
	  --python-platform $(PYTHON_PLATFORM) --python-version $(PYTHON_VERSION) \
	  --target build/lambda -r build/requirements.txt
	cp -R src/enrich_pipeline build/lambda/enrich_pipeline
	find build/lambda -type d -name '__pycache__' -prune -exec rm -rf {} +
	rm -rf build/lambda/bin build/lambda/.lock
	find build/lambda -name RECORD -exec sed -i.bak '/^bin\//d' {} + && find build/lambda -name RECORD.bak -delete
	@echo "package: $$(du -sh build/lambda | cut -f1) unzipped in build/lambda"

# --- Terraform ----------------------------------------------------------------
TF_BOOTSTRAP := terraform -chdir=infra/bootstrap
TF_ENV       := terraform -chdir=infra/envs/$(ENV)
# Locally from the bootstrap stack's output; in CI from the STATE_BUCKET environment variable.
STATE_BUCKET ?= $(shell terraform -chdir=infra/bootstrap output -raw state_bucket 2>/dev/null)
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

TF_DOC_DIRS := infra/bootstrap infra/envs/dev $(wildcard infra/modules/*)

tf-docs: ## Regenerate the requirements/resources/inputs/outputs tables in every stack's and module's README
	@for d in $(TF_DOC_DIRS); do terraform-docs --config .terraform-docs.yml "$$d" >/dev/null && echo "  $$d/README.md"; done

tf-docs-check: ## Fail if any Terraform README is out of date with its variables and outputs (CI)
	@for d in $(TF_DOC_DIRS); do terraform-docs --config .terraform-docs.yml --output-check "$$d" || exit 1; done

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

upload: ## Copy INPUT (default: the cached demo file) to the landing bucket under incoming/<timestamp>/
	aws s3 cp $(AWS_INPUT) "s3://$$($(TF_ENV) output -raw landing_bucket)/incoming/$$(date -u +%Y%m%dT%H%M%SZ)/$$(basename $(AWS_INPUT))"

smoke: ## Smoke test: invoke validate -> enrich -> build-curated directly in AWS on INPUT (default: the cached demo file)
	scripts/smoke.sh $(AWS_INPUT)

e2e: ## End-to-end test: upload INPUT (default: the cached demo file), follow the execution, count the batch in Athena
	scripts/e2e.sh $(AWS_INPUT)

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

rebuild-all: ## Rebuild every batch's curated tables from stored results (PREFIX=<batch_id prefix> to narrow)
	scripts/rebuild_all.sh $(PREFIX)

validate: ## Dry-run the input guards on INPUT without enriching: what would be accepted, salvaged, rejected
	$(UV) run enrich validate --input $(INPUT)

input-contract: ## Regenerate docs/input-contract.json from the ingestion and guard code
	$(UV) run python scripts/input_contract.py

asl-validate: ## Validate the state machine definition with the Step Functions API (renders the template with dummy ARNs)
	@mkdir -p build
	@sed -e 's/$${validate_input_arn}/arn:aws:lambda:ap-southeast-1:123456789012:function:validate/' \
	     -e 's/$${enrich_arn}/arn:aws:lambda:ap-southeast-1:123456789012:function:enrich/' \
	     -e 's/$${build_curated_arn}/arn:aws:lambda:ap-southeast-1:123456789012:function:build/' \
	     -e 's/$${alerts_topic_arn}/arn:aws:sns:ap-southeast-1:123456789012:alerts/' \
	     -e 's/$${max_concurrency}/1/' \
	     infra/modules/orchestration/pipeline.asl.tftpl > build/pipeline.asl.tftpl
	aws stepfunctions validate-state-machine-definition --type STANDARD --definition file://build/pipeline.asl.tftpl
