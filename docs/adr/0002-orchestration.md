# ADR 0002: Orchestration with Step Functions

**Status:** accepted, 2026-09-25 (Phase 3); amended 2026-09-26 (row-crash reconciliation)

## Context

A batch is a CSV of up to a few hundred registrants. Each row needs one provider call
(sometimes a retry), the provider rate-limits name-only lookups to 10 per minute, and the
brief asks for code that "runs when triggered". The output must be one Parquet file per
table per batch, which needs a "batch complete" point. Everything has to stay inside the
AWS Free plan.

## Options considered

| Option | For | Against |
|---|---|---|
| **Step Functions Standard**, inline Map over rows | One execution per batch with a visual history; built-in Retry/Catch; `MaxConcurrency` caps provider calls per minute; a natural batch-complete step; 4,000 free state transitions a month | Inline Map carries the rows in state (256 KB, ~500 rows); every transition counts |
| SQS queue → Lambda per row | Simple, scales wide | No batch-complete point without extra bookkeeping; small-files problem for Parquet; rate limiting needs reserved concurrency the new account's quota (10) cannot spare |
| Glue job / Glue workflow | Native Parquet, catalogue integration | Billed per DPU-hour with a 10-minute minimum; heavy for a few hundred HTTP calls |
| Step Functions Distributed Map with S3 `ItemReader` | Millions of rows, no inline payload limit | Overkill for the POC; child executions multiply transitions; kept as the scale path |

## Decision

Step Functions **Standard** workflow, started by an EventBridge rule on `Object Created`
in the landing bucket (input transformer → `{bucket, key}`):

1. `ValidateInput` (Lambda) parses and guards the file, writes the parsed input to S3,
   returns the valid rows inline.
2. `EnrichRows` is an inline **Map**, `MaxConcurrency = 1` by default (a Terraform
   variable), one `enrich` invocation per row. Retries for provider 429/5xx live **inside
   the function** (waits until `x-ratelimit-reset`, backoff, three attempts, 20 s cap),
   so a retry costs no state transition; the Map itself retries only Lambda service
   errors. A crashed row is caught into an `error` record instead of failing the batch.
3. `BuildCurated` (Lambda) reconciles the parsed input against the result objects,
   records crashed rows as `error`, runs the output guards and writes the Parquet tables
   and a manifest.
4. `Summarize` → `AnyRowErrors` (Choice): row errors or data-quality warnings publish a
   "completed with warnings" SNS message; the execution still succeeds because the data
   is complete. Any failure in steps 1 or 3 publishes to SNS and ends the execution FAILED
   with the original error and cause.

Execution logging is at level ALL **without** state payloads, so registrant data never
reaches CloudWatch Logs. X-Ray tracing is on.

## Consequences

- A 28-row batch is about 90 transitions; the free 4,000 a month covers roughly 40 such
  batches. `MaxConcurrency = 1` makes a 100-row batch take about three minutes, well
  under the 10-per-minute identify limit even if every row were name-only.
- The inline Map's payload limit caps a file at `max_rows` (500). Larger files are
  rejected with a clear message; Distributed Map with an S3 `ItemReader` is the planned
  path beyond that.
- Because retries and the credit guard live in the function, the same code path runs
  locally (`make run`) and in AWS, and the state machine stays small enough to read.
- Alarms on `ExecutionsFailed` and `ExecutionsTimedOut` complement the SNS messages the
  execution publishes itself.
