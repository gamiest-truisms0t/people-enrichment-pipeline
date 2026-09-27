# Runbook

What to do when the pipeline talks to you. Every AWS alert arrives by email from one SNS
topic (`people-enrichment-dev-alerts`, subscriber `alert_email`); GitHub sends its own.
Commands assume the repository root, a fresh `make login` (12-hour session) and the `dev`
stack. `<batch_id>` is the identifier that appears in every email, manifest and curated
row.

## Where to look first

| Question | Where |
|---|---|
| Which executions ran, and did they succeed? | `make executions` (the last five); Step Functions console, state machine `people-enrichment-dev-pipeline`, each execution's graph and event history |
| What happened to each row of a batch? | `make report BATCH=<batch_id>`: status, method, likelihood, attempts and the provider's credit headers, read from `results/batch_id=<batch_id>/` |
| What did the batch look like as a whole? | `manifests/<batch_id>.json` in the data bucket: counts, the quality report, file references; the same row in `fact_batch_quality` (saved query 6) |
| Is it healthy over time? | CloudWatch dashboard `people-enrichment-dev-pipeline`: executions, execution time against the objective, rows by status, credits against the ceilings, Lambda errors and duration, queue depth |
| What did a function log? | Log groups `/aws/lambda/people-enrichment-dev-validate-input`, `…-enrich`, `…-build-curated` (structured JSON with batch id and row number, 14 days) and `/aws/vendedlogs/states/people-enrichment-dev-pipeline` |
| What is in quarantine? | `make quarantine` |
| How many credits this month? | The dashboard's credits widget; items `budget#pdl#<YYYY-MM>#enrich` and `…#identify` in the table `people-enrichment-dev-state`; the provider's own dashboard |
| Which code produced a row? | `pipeline_version` on every curated row |
| Is the account still what `main` describes? | The daily `terraform drift` workflow, or `make plan` |

## Alerts

### Email "People-enrichment: pipeline execution FAILED"

**Meaning.** `ValidateInput` or `BuildCurated` failed after the Lambda-level retries. The
message carries the execution id, the source object, the error and the cause. The alarm
`people-enrichment-dev-pipeline-executions-failed` fires as well and clears by itself.

**Do.**

1. The cause names an input problem (size, encoding, delimiter, header, no rows, too many
   rejected rows): the file was copied to `quarantine/files/` with the reason as object
   metadata. `make quarantine` lists it; `make quarantine-get KEY=quarantine/files/<name>`
   downloads it. Fix it at the source and upload again, or
   `make redrive KEY=quarantine/files/<name>` to run it as-is under `incoming/redrive/`
   once the cause was elsewhere (a guard threshold you have since changed, for example).
   A quarantined upload keeps its batch claim, so redrive or a new upload is the way back,
   not re-triggering the same object.
2. `BuildCurated` failed: the rows' results are already in `results/batch_id=<batch_id>/`.
   Fix the cause and run `make rebuild BATCH=<batch_id>`, which rebuilds the curated tables
   without provider calls.
3. Infrastructure causes (`Lambda.ServiceException`, an access-denied error): read the
   function's log group, `make iam-check` for a policy regression, `make plan` for drift.
   An execution that failed unexpectedly releases its claim, so uploading the same file
   again works once the cause is fixed.

**Back to normal.** The next successful execution; the alarm returns to OK on its own.

### Email "People-enrichment: batch completed with warnings"

**Meaning.** The batch finished and its tables exist, but rows crashed (recorded as
`error` in `fact_lookup`) or the quality report has warnings: a low match rate
(`min_match_rate`), a high share of rejected rows (`max_invalid_fraction`), matched rows
carrying `match.*` flags, an encoding fallback, or rows skipped while the circuit breaker
was open (`provider_unavailable`). The message lists the failed row numbers, the warnings,
the rejected-rows file and the manifest.

**Do.** `make report BATCH=<batch_id>`. Rejected rows are in `quarantine/rows/<batch_id>.csv`
with a reason each; fix them at the source. `error` and `provider_unavailable` rows are not
cached, so uploading the same file again retries only them; matched, not-found and
ambiguous rows come from the cache at zero credits. For flagged matches, look at
`quality_flags` and `match_likelihood` in `dim_person`; the threshold is
`enrich_min_likelihood`. If the file is legitimately unusual, adjust `min_match_rate` or
`max_invalid_fraction` in `terraform.tfvars` and apply.

**Back to normal.** Nothing to reset.

### Alarms `…-enrich-credits-90pct` and `…-identify-credits-90pct`

**Meaning.** The month-to-date counter reached `budget_alarm_fraction` of the ceiling
(`max_enrich_credits`, 70 of the plan's 100; `max_identify_credits`, 2 of the plan's 5).
At the ceiling rows become `budget_deferred`: no call, no cache entry, nothing spent.

**Do.** Nothing urgent. The dashboard's credits widget shows the position, `make report`
shows what was deferred. To process deferred rows, upload the file again after the monthly
reset (counters are per calendar month), or raise the ceiling in `terraform.tfvars` if the
plan has room. The identify alarm sits in ALARM most months by design: two credits go
quickly.

**Back to normal.** The first batch of the new month publishes a low value and the alarm
clears. Missing data is ignored, so it holds its state between batches instead of flapping.

### Alarm `…-pipeline-executions-timed-out`

**Meaning.** An execution exceeded the state machine's `TimeoutSeconds` (3,600). One row at
a time with header-driven waits means a large file of unknown names, or a rate-limited
provider, can take that long.

**Do.** `make executions`, then `make report BATCH=<batch_id>`: the rows that finished have
results. `make rebuild BATCH=<batch_id>` builds the curated tables for them; upload the
file again for the rest (cached rows cost nothing). Split files above a few hundred rows.

### Alarm `…-pipeline-execution-time`

**Meaning.** An execution took longer than `max_execution_seconds` (600) from upload to
curated tables: the freshness objective was missed, but the batch finished.

**Do.** `make report BATCH=<batch_id>`: `attempts` above 1 and HTTP 429 mean rate-limit
waits; otherwise provider latency or file size. No data action.

### Alarm `people-enrichment-dev-<function>-errors`

**Meaning.** The function raised. For `validate-input` the execution fails (see the FAILED
email). For `enrich` the Map's `Catch` records the row as `error` and the batch continues
(see the warnings email). For `build-curated` the execution fails; `make rebuild` after
the fix.

**Do.** The function's log group; every log line carries the batch id and, for `enrich`,
the row number.

### Alarm `…-dead-letter-queue-not-empty`

**Meaning.** EventBridge could not start an execution for an upload after its retries (a
permission regression or throttling), or an asynchronous invocation failed. The event sits
on `people-enrichment-dev-lambda-dlq`.

**Do.** Read it:

```bash
aws sqs receive-message --queue-url "$(terraform -chdir=infra/envs/dev output -raw lambda_dlq_url)"
```

The body names the bucket, key and version. Fix the cause (`make iam-check`, `make plan`),
re-trigger by copying the object to a new key under `incoming/`, then purge the queue
(`aws sqs purge-queue --queue-url …`).

**Back to normal.** The alarm returns to OK when the queue is empty.

### Email "IAM Access Analyzer: external access finding on …"

**Meaning.** A resource in the account can be reached by a principal outside it, public or
cross-account. The three GitHub OIDC roles are expected and archived by rule; anything else
is new.

**Do.** IAM console, Access Analyzer, the finding. If a policy changed by hand, `make plan`
shows the drift and `make apply` reverts it. Archive findings that are expected.

### Email from AWS Budgets (actual above 20 % of $5, or forecast above $5)

**Meaning.** Gross usage before credits crossed the threshold. The Free plan cannot be
charged; the credits are what is being spent.

**Do.** Billing, Cost Explorer, group by service with record type "Usage". The usual
suspects: Athena scans, S3 requests from repeated rebuilds, more than ten custom metrics,
or something outside this project. `make destroy` stops everything except the state bucket.

### Email from Cost Anomaly Detection

The same investigation. Anomalies arrive up to a day after the usage.

### GitHub: the `terraform drift` workflow failed

**Meaning.** The account differs from `main`; the plan in the run log shows how.

**Do.** If someone changed the account by hand, re-run the last `terraform apply` workflow
(`gh run rerun <run id>`) or push a fix. If the change is wanted, codify it in a pull
request.

### GitHub: Dependabot pull request, CodeQL or secret-scanning alert

Dependabot pull requests get CI and the plan comment like any other; check that the pinned
SHA comment still matches the version. CodeQL and secret-scanning alerts are on the
Security tab; a real secret is rotated (below) before its alert is closed.

## Routine operations

| Task | How |
|---|---|
| Rotate the provider key | Save the new key to `~/.config/people-enrichment/pdl_api_key` (mode 600) and run `make set-api-key`. The enrich function caches the parameter for five minutes, so batches after that use the new key. Revoke the old key at the provider. |
| Reprocess after a code change | Deploy, then `make rebuild BATCH=<batch_id>` for one batch or `make rebuild-all` for every batch: the curated tables are rebuilt from stored results with no provider calls, and `pipeline_version` shows which rows were rebuilt. |
| Retry rows that failed or were deferred | Upload the same file again: cached rows cost nothing, the others are looked up. |
| Handle a quarantined file | `make quarantine`, `make quarantine-get KEY=…`, fix at the source and upload again, or `make redrive KEY=…` to run it as-is. |
| Check spend | `aws budgets describe-budgets --account-id <account id>` for the budget's actual (gross) spend; Billing, Free Tier for the always-free meters; the dashboard's credits widget for the provider. |
| Verify a deployment | `make smoke` (direct invocations), `make e2e` (upload, follow the execution, count the batch in Athena), `make athena-verify`, `make iam-check`, `make idempotency-proof`. All at zero credits. |
| Rebuild the stack | `make destroy` removes everything in `infra/envs/dev`, data included, so back up the data bucket and the state table first if the history matters. Then `make apply`, confirm the SNS email, `make set-api-key`, `make e2e`. |
| Erase a person (right to erasure) | `make erase EMAIL=… DRY_RUN=1` (or `NAME="First Last"`, `PERSON_ID=…`) lists the batches, rows and objects involved; without `DRY_RUN` it deletes the results and raw responses, rewrites the input document and the rejected-rows export, drops the cache entries, rebuilds each affected batch and purges the old object versions, then writes `erasure#<request id>` to the state table. Run it while no execution is in flight (`make executions`), so a batch is not rebuilt under a running Map. It prints the operator uploads in the landing bucket that still hold the person; delete those by hand if the request covers them. |
| Monthly | Nothing. Credit counters reset by calendar month on their own and the credit alarms clear at the first batch. |
