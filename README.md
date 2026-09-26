# people-enrichment-pipeline

Serverless ETL on AWS that takes a CSV of event registrants (first name, last name,
optional email/company), enriches each person through a free people-profile API, and
lands analyst-ready Parquet tables (people, employment history, lookup log) queryable
in Athena. Provisioned entirely with Terraform. Designed to run on free-tier credits.

**Status:** Phase 4 complete. Dropping a CSV into the landing bucket starts a Step
Functions execution that validates the file, enriches each row through **People Data
Labs** (or the offline mock provider), and builds the three curated Parquet tables, with
failures reported to an SNS email topic. Everything is provisioned by Terraform and has
run end to end on live data within the free plan. The Athena layer is Phase 5. See
[PLAN.md](PLAN.md) for the architecture, data model, failure handling, and build phases.
This README is filled in fully at Phase 7.

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

## Querying the results in Athena

Terraform registers the three curated tables in a Glue database
(`people_enrichment_dev`) and creates the Athena workgroup
`people-enrichment-dev-analytics`. Column definitions are generated from
`src/enrich_pipeline/schema.py` (`make glue-columns`; a unit test fails if the committed
file drifts), so the transform, the Parquet writer and the catalog can never disagree.

- **Partition projection** over `batch_date`: Athena derives the partitions from a date
  range instead of the catalog, so a new batch is queryable the moment its Parquet file
  lands. No crawler, no `MSCK REPAIR`, no per-batch catalog writes.
- **Workgroup guardrails**: results encrypted (SSE-S3) under `athena-results/` in the data
  bucket, expired after 7 days by lifecycle rule; settings enforced for every query; a
  per-query scan cutoff of 100 MB, which is thousands of batches of this size.
- **Saved queries**: the brief's three questions plus an operational per-batch view are
  saved in the workgroup, so they appear in the console's Saved queries tab.
  `docs/athena_queries.sql` has the same SQL with date filters.

```bash
make athena-verify   # run the saved queries from the CLI and print the first rows
```

Verified on 2026-09-26 against the batches in the dev account: all four saved queries
succeed, each scanning 5 to 16 KB in 0.6 to 1.7 seconds. Two things to know when reading
results: `dim_person` holds one row per person **per batch**, so a person enriched in two
uploads appears twice with two `batch_id`s (join on both `person_id` and `batch_id`, as the
saved queries do); and the dev bucket still contains the mock-provider batches from earlier
phases alongside the live ones, distinguishable by the `provider` column.

## Operations, security and CI

**Alarms** (eight, inside the always-free ten), all notifying the alerts topic:

| Alarm | Signal |
|---|---|
| `<fn>-errors` ×3 | Lambda `Errors` ≥ 1 in 5 minutes, per function |
| `pipeline-executions-failed` / `-timed-out` | Step Functions `ExecutionsFailed` / `ExecutionsTimedOut` |
| `enrich-credits-90pct` / `identify-credits-90pct` | month-to-date credit counters published by the enrich function reach 90 % of each ceiling |
| `dead-letter-queue-not-empty` | any message on the pipeline DLQ |

**Least privilege.** Every function and the state machine have their own role with inline
policies scoped to the exact bucket prefixes, table, parameter and functions they touch.
`make iam-check` lists every statement that still uses a wildcard resource and runs IAM
Access Analyzer's policy validation over each policy. Result on 2026-09-26: **no ERROR or
SECURITY_WARNING findings**; the only wildcard-resource statements are X-Ray trace
uploads, `cloudwatch:PutMetricData` (restricted by a namespace condition) and Step
Functions log-delivery management, none of which support resource-level permissions.
checkov passes 299 checks with one inline, documented skip.

**Idempotency proof.** `make idempotency-proof` pushes `data/demo/idempotency.csv` through
the deployed pipeline twice and asserts the second run is served entirely from the cache.
Result on 2026-09-26: first run 4 matched, 1 not found, 1 invalid, **4 credits**; second
run 5 `cached`, 1 invalid, **0 credits**, identical persons and positions. Re-uploading a
registration list therefore costs nothing and still produces a complete batch.

**Repository controls.** `main` is protected: the three CI checks (lint + unit tests,
Terraform fmt/validate/tflint/checkov, gitleaks secret scan) must pass and the branch must
be up to date before a merge; force-pushes and deletion are blocked. Dependabot watches
GitHub Actions, the Python lock file and the Terraform providers weekly. All actions are
pinned to commit SHAs. Server-side branch protection needs a public repository or GitHub
Pro, which is why this repository is public.

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

## Provider: People Data Labs

`PdlProvider` talks to two endpoints over `httpx`, chosen by the matching ladder:
`GET /v5/person/enrich` for email, LinkedIn URL, or name plus company/location, and
`GET /v5/person/identify` for name-only rows. The same class serves the free **sandbox**
host (`--sandbox` locally, `pdl_sandbox = true` in Terraform), which returns synthetic
people at zero cost and is what the recorded test fixtures come from.

What was learned running it against the live API, all of which shaped the defaults:

| Finding | Consequence |
|---|---|
| Enrichment and Identify are **separate credit pools**: the free plan grants 100 enrichment credits a month but only **5 identify credits** | Separate monthly ceilings (`max_enrich_credits` 70, `max_identify_credits` 2) and a per-pool HTTP 402 marker, so an exhausted identify pool never stops enrichment |
| Enrichment bills only on a 200; a 404 costs nothing. Identify bills every call, matched or not | Name-only rows are the expensive path; give the input a company or location column whenever the registration system has one |
| Correct name-plus-company matches for well-known people score a **likelihood of about 4** on PDL's 1-10 scale; a threshold of 6 kept 1 match in 8 | `enrich_min_likelihood` defaults to 4. Every person row carries `match_likelihood`, so analysts can filter more strictly downstream |
| The threshold is sent to the provider and changes its answer | It is part of the cache key, so tuning it re-queries instead of replaying cached not-found results |
| The sandbox resolves strong identifiers (profile URL, `pdl_id`, email) but answers 404 to every name-based lookup | Sandbox test data uses LinkedIn URLs; name-based behaviour is verified with recorded live responses |
| The free plan returns contact and fine-grained location fields as `true`/`false` | The models coerce those to null and the pipeline never stores contact data anyway |
| `x-ratelimit-reset` is a UTC timestamp, not a number of seconds | The retry logic parses it and sleeps until the window reopens, capped at `max_wait_seconds` |

The API key lives only in an SSM SecureString (`make set-api-key`) and, locally, in
`~/.config/people-enrichment/pdl_api_key` or `PDL_API_KEY`. Neither the repo nor
Terraform state ever holds it.

### Live runs on 2026-09-25

The demo input (`data/demo/registrants.csv`) is 28 rows of publicly known company
executives with their employer, plus one name-only row, one deliberately wrong employer,
one duplicate and one invalid row. Public figures were used because their professional
history is public information; the pipeline still stores no contact fields.

| Run | Rows | Outcome | Enrichment credits | Identify credits |
|---|---|---|---|---|
| Shakedown, threshold 6 | 9 valid | 1 matched, 8 not found | 1 | 1 |
| Threshold diagnostic (local CLI, threshold 2) | 3 | 2 matched at likelihood 4, 1 not found | 2 | 0 |
| Demo batch, threshold 4 | 27 valid | **17 matched**, 9 not found, 1 cached | 17 | 1 |
| **Month to date** | | | **21 of 100 used, 79 left** | **3 of 5 used, 2 left** |

Match likelihoods on the demo batch ranged from 4 to 9 (median 6). The nine misses were
true 404s at the provider, which cost nothing, including the deliberate "Mary Barra @
Microsoft" mismatch. Two data-quality caveats worth knowing when reading `dim_person`:
PDL's "current job" fields sometimes point at a secondary record (a board seat, a plant
role), while `fact_employment` carries the full dated history; and `location_country`
is occasionally wrong at the provider. Both are provider data, kept as delivered, with
`match_likelihood` available to filter.

## Layout

```
src/enrich_pipeline/   Python package (models, providers, transform, Lambda handlers)
tests/                 unit + handler tests, fixtures (synthetic data only)
infra/                 Terraform: bootstrap (state), envs/dev, modules/
docs/                  ADRs, architecture notes, Athena queries
data/sample/           sample input CSV
scripts/               local setup helpers
```
