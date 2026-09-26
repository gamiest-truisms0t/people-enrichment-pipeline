# ADR 0004: Idempotency cache and credit guard in DynamoDB

**Status:** accepted, 2026-09-25 (Phase 2); amended 2026-09-25 (per-pool budgets after the
live findings in Phase 4)

## Context

Free credits are the scarcest resource in this project: 100 enrichment credits and 5
identify credits a month. Registration lists get re-uploaded, contain duplicates, and
retries happen. The provider's own headers (`x-call-credits-spent`,
`x-totallimit-remaining`) report spend after the fact, which is too late to prevent it, and
they include usage from outside this pipeline.

## Options considered

| Option | For | Against |
|---|---|---|
| **DynamoDB table (provisioned 5/5) with cache items and atomic counters** | Inside the always-free 25 RCU/WCU; strongly consistent reads; atomic `ADD`; TTL for free expiry; shared by every invocation | One more service; provisioned capacity must be sized (trivially, here) |
| Trust the provider's headers | No state | Detects overspend only after it happened; blind to other clients of the same key |
| S3 marker objects | No new service | Eventually consistent listing, no atomic counter |
| DynamoDB on-demand | No capacity to size | No always-free allowance |

## Decision

One table with two kinds of items:

- **Cache**: `lookup#<key>` holds the full lookup result (matched, not found or ambiguous
  outcomes are cacheable) with a 90-day TTL. The key is a SHA-256 over the *normalised
  identifiers actually sent to the provider* (name, email, company, location, LinkedIn URL
  after NFKC, casefold, punctuation and whitespace folding) **plus any parameter that
  changes the provider's answer** (the enrich likelihood threshold, the identify score and
  margin). Tuning a threshold therefore re-queries instead of replaying a stale outcome.
- **Budget**: `budget#<provider>#<YYYY-MM>#<kind>` is an atomic counter per credit pool
  (`enrich`, `identify`). Every billable call checks the counter against its ceiling
  first (`max_enrich_credits` 70, `max_identify_credits` 2) and adds the credits the
  provider reports afterwards (assumed 1 when the header is missing). Rows past a ceiling
  become `budget_deferred` and the batch completes. An HTTP 402 writes an
  `exhausted` marker for that pool so the rest of the batch skips the call.

The same protocols have in-memory implementations for the CLI and tests.

## Consequences

- A person is paid for at most once per key. The proof (`make idempotency-proof`) uploads
  the same file twice; the second run is entirely `cached` at zero credits.
- Because the ceilings are self-imposed and below the plan's allowance, the pipeline
  never reaches the provider's 402 in normal operation; the alarms on the month-to-date
  counters fire at 90 % of each ceiling.
- Identify credits are so scarce (5 a month) that name-only rows are effectively rationed
  to two per month; inputs should carry a company or location column, which moves rows
  to the enrichment pool (billed only on a match).
- Dropping a malformed email from a row (a data guard) changes its key, so such a row is
  looked up again once.
