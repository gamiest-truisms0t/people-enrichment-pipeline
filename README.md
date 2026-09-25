# people-enrichment-pipeline

Serverless ETL on AWS that takes a CSV of event registrants (first name, last name,
optional email/company), enriches each person through a free people-profile API, and
lands analyst-ready Parquet tables (people, employment history, lookup log) queryable
in Athena. Provisioned entirely with Terraform. Designed to run on free-tier credits.

**Status:** Phase 3 complete. Dropping a CSV into the landing bucket starts a Step
Functions execution that validates the file, enriches each row through a Lambda
function, and builds the three curated Parquet tables, with failures reported to an SNS
email topic. Everything is provisioned by Terraform and runs against the mock provider;
the real provider is Phase 4 and the Athena layer is Phase 5. See [PLAN.md](PLAN.md)
for the architecture, data model, failure handling, and build phases. This README is
filled in fully at Phase 7.

## Quick start (developer)

```bash
make setup    # uv sync + git hooks
make check    # lint + unit tests (unit, provider contract, mocked-AWS handler tests)
make run      # enrich data/sample/names.csv with the mock provider -> ./out
make query    # answer the brief's three questions from ./out with DuckDB
make tf-lint  # terraform fmt/validate, tflint, checkov over infra/
```

## Deploying to AWS

```bash
make login       # browser sign-in for a 12-hour AWS CLI session (no access keys)
make bootstrap   # once: the Terraform state bucket (versioned, encrypted, TLS-only)
make init        # point infra/envs/dev at that bucket (native S3 state locking)
make apply       # package the Lambda code (arm64 wheels from uv.lock) and apply
make smoke       # upload the sample CSV and drive validate -> enrich -> build-curated
make destroy     # tear everything down; dev buckets are force_destroy
```

What `make apply` creates, all inside always-free allowances:

| Resource | Purpose |
|---|---|
| S3 `landing` bucket | `incoming/<folder>/<file>.csv` uploads; EventBridge notifications on (Phase 3 trigger) |
| S3 `data` bucket | `input/`, `raw/`, `results/`, `curated/`, `manifests/` prefixes; lifecycle rules per prefix |
| DynamoDB `state` table | idempotency cache (`lookup#…`, TTL) and monthly credit budget (`budget#…`), provisioned 5/5 |
| SSM SecureString | provider API key, placeholder until `make set-api-key`; Terraform ignores the value |
| 3 Lambda functions | `validate-input`, `enrich`, `build-curated`; Python 3.13 arm64, one shared 5 MB package |
| Per-function IAM role | least privilege: only the prefixes and table actions each function touches |
| Log groups, alarms, DLQ | 14-day retention, Errors ≥ 1 alarms, SQS dead-letter queue for async invokes |

| Step Functions state machine | `validate-input` → Map over rows (`enrich`, `MaxConcurrency` 1) → `build-curated` → summary; retries, per-row catch, SNS on failure |
| EventBridge rule | S3 `Object Created` on `incoming/*.csv` in the landing bucket starts an execution; undeliverable events go to the DLQ |
| SNS topic + email subscription | pipeline failures, batches with row errors, and the Lambda error alarms |

Both buckets block public access, enforce TLS, are versioned and SSE-S3 encrypted. No
public endpoint exists: functions are invoked only by Step Functions or by an IAM
principal, and the only way to start a run is to write to the landing bucket. Every
checkov skip is listed with a reason in `.checkov.yaml` or inline next to the resource.

## Running a batch on AWS

```bash
make e2e          # upload data/sample/names.csv and follow the execution it triggers
make upload INPUT=path/to/registrants.csv   # just upload; EventBridge does the rest
make executions   # the five most recent executions
make rebuild BATCH=<batch_id>               # rebuild curated tables from stored results
```

A run looks like this in the state machine:

1. **ValidateInput** parses the CSV. A bad header fails the batch (and emails you); bad
   rows are recorded and the batch continues.
2. **EnrichRows** is a Map state over the rows. Each row is one `enrich` invocation, which
   already retries provider 429/5xx internally; the Map retries Lambda-level throttles
   and catches crashes into an `error` row so the batch still completes.
3. **BuildCurated** writes the Parquet tables and manifest.
4. **Summarize** counts row errors. Zero: the execution succeeds. Otherwise an SNS
   notification lists the batch, the counts and the manifest, and the execution still
   succeeds because the data is complete apart from the flagged rows.
5. Any failure in steps 1 or 3 publishes to SNS and ends the execution as FAILED with the
   original error and cause.

Execution history is logged to CloudWatch at level ALL without state payloads, so names
and emails never appear in logs. X-Ray tracing is on for the state machine and functions.

`make run` prints a per-status summary (matched, ambiguous, not_found, cached,
budget_deferred, invalid_input, error) and writes:

```
out/raw/provider=mock/batch_date=YYYY-MM-DD/batch_id=<id>/<lookup_key>.json
out/curated/{dim_person,fact_employment,fact_lookup}/batch_date=YYYY-MM-DD/<id>.parquet
out/manifests/<id>.json
```

The same layout is what lands in S3 from Phase 2 onwards.

## How a row is enriched

1. **Matching ladder.** Email, then LinkedIn URL, then name plus company/location, then
   name only. Strong identifiers use the provider's enrich call (billed only on a match);
   name-only rows use identify (billed on every call) behind a confidence gate: top
   candidate score at least 70 and 20 points clear of the runner-up, otherwise `ambiguous`.
2. **Idempotency.** A normalised lookup key (NFKC, casefold, whitespace and punctuation
   folded, plus the identifiers used) means a repeated name is served from cache at zero
   credits. On AWS the cache lives in DynamoDB for 90 days, so re-uploading a file costs
   nothing and still produces complete curated tables for that batch; the operational
   table records those rows as `cached`.
3. **Protection.** Retries with header-driven waits on 429 and backoff on 5xx; per-run
   credit ceilings; an HTTP 402 marks the provider exhausted and defers the rest of the
   batch instead of failing it.
4. **Raw first.** Every provider response is written to the raw layer before it is
   interpreted, so the curated tables can be rebuilt without spending credits.

## Layout

```
src/enrich_pipeline/   Python package (models, providers, transform, Lambda handlers)
tests/                 unit + handler tests, fixtures (synthetic data only)
infra/                 Terraform: bootstrap (state), envs/dev, modules/
docs/                  ADRs, architecture notes, Athena queries
data/sample/           sample input CSV
scripts/               local setup helpers
```
