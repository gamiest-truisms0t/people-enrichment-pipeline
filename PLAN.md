# Plan — Serverless people-enrichment ETL on AWS (take-home)

Plan written 2026-09-25 from `project-testing.pdf`. Target: a proof of concept that
runs entirely on free API credits and the AWS free tier, provisioned with Terraform,
with a README covering architecture, assumptions, and failure/API-limit handling.

---

## 1. What the brief actually asks for

| Requirement (from the brief) | What it means for the build |
|---|---|
| Serverless pipeline on AWS, any services | Lambda + Step Functions + S3 + DynamoDB + Glue Catalog + Athena. No servers, no containers to run. |
| Enrich people via a **free** profile API (PDL / FullContact given as examples) | Provider adapter with one real provider plus a mock. Hard budget guard so free credits are never exceeded. |
| Extract individuals + **employment history** | Output must model many past positions per person, not just current title. |
| Store in a structured format easy for a DE/DA | Parquet in S3, Hive-partitioned, registered in Glue, queryable in Athena with SQL. Raw API JSON kept alongside. |
| Terraform for **all** infra, code "runs when triggered" | Everything in `infra/`. Trigger = CSV upload to S3. Nothing created by hand except the API key value. |
| Assume VPCs exist; focus on service config + security | Lambdas outside a VPC by default (egress only, no inbound). Variables to attach to existing private subnets if reviewer insists. Documented as an assumption. |
| Version control with best practices | Git from commit one, protected main, PRs, CI, pre-commit, no secrets in history. |
| Basic security: no public access | No API Gateway / Function URLs, S3 Block Public Access, least-privilege IAM per function, encryption at rest, TLS-only bucket policies. |
| README: architecture, assumptions, failure + API-limit handling | Drafted from section 11 below; written as the last phase but its assumptions list is kept live from day one. |

### Ambiguities in the brief (decide early, state in README)

1. **Objective says "email addresses", Problem says "names".** Input schema will be
   `first_name, last_name` required, plus optional `email, company, location, linkedin_url`.
   The pipeline uses the strongest identifier available per row.
2. **Name alone is a weak identifier.** People-profile APIs generally refuse or
   mis-match on a bare name. The plan has an explicit matching ladder (section 5) and
   an `ambiguous` outcome instead of guessing.
3. **"Any free API"** means the provider must be swappable. Adapter interface +
   mock provider + fixtures make the pipeline demonstrable even if free credits run
   out or a provider changes its plan.
4. **"When triggered"** is read as event-driven (S3 object created), with a manual
   trigger path (`make run`) for demos.

---

## 2. Constraints and resources

### 2.1 Enrichment API free tiers (verified against vendor docs on 2026-09-25)

| Provider | Free allowance | Card? | Bare name accepted? | Work history? | Sandbox? | Verdict |
|---|---|---|---|---|---|---|
| **People Data Labs** | 100 credits/month per product (Enrichment, Search, Identify); Enrichment bills only a 200 match, 404 = free; Identify bills every call | Not stated; **work email required** to sign up | Enrichment **no** (name must be paired with company/school/location…); Identify **yes** | Yes, `experience[]` | **Yes**: free, synthetic data, same API key | **Primary** |
| **Diffbot** (Enhance API) | 10,000 credits/month; 25 credits per person record ≈ 400 lookups | No | Yes (`type=Person&name=…`, optional employer/location) | Yes, `employments[]` with dates | No | **Fallback / name-only path** |
| Coresignal | 2,000 trial credits for 7 days, once per domain; search free, 20 credits per record | No | Yes (search on `full_name`) | Yes | No | Reserve option |
| Apollo.io | ~75 credits/month (900 per seat per year) | No | No (needs domain or email) | Yes | No | Only if inputs carry company domains |
| Hunter.io | 50 credits/month | No | No (name + domain) | No (current job only) | No | Email-finding step only |
| FullContact | No public free tier; API access is sales-gated; assets bought by Ziff Davis in 2024 | ? | No (name needs a postal address) | Yes | No | Skip |
| Clearbit / Proxycurl | Free tools sunset Apr 2025 / service shut down Jul 2025 | – | – | – | – | Skip |
| RocketReach, Lusha, Kaspr, ContactOut, Enrich Layer, Crustdata | API paid, LinkedIn-URL/domain-gated, or short trial only | – | Mostly no | Some | No | Skip |

**People Data Labs specifics that shape the design**

- Minimum input for Person Enrichment, quoted from the docs: `profile OR email OR phone OR
  email_hash OR lid OR pdl_id OR (((first_name AND last_name) OR name) AND (company OR
  school OR location OR street_address OR locality OR region OR country OR postal_code OR
  birth_date))`. A bare name returns HTTP 400.
- Person Identify accepts `first_name + last_name` alone and returns up to 20 candidates,
  each with a `match_score` (1–99) and the full profile. 10 calls/min. **1 credit per call
  even when nothing matches.**
- `min_likelihood` (default 2, which the docs equate to a 10–30 % chance of the right
  person) and `required` filter Enrichment matches. A filtered-out result comes back as
  404, which the docs describe as unbilled. Verify on the first real run via the
  `x-call-credits-spent` header.
- Free-plan rate limits: Enrichment 100/min, Identify and Search 10/min, Sandbox 5/min.
  Headers: `x-ratelimit-remaining.minute`, `x-ratelimit-reset`, `x-call-credits-spent`,
  `x-totallimit-remaining`. 429 when the minute window is exhausted, **402 when credits
  are gone**. No `Retry-After` header is documented.
- The free plan obscures contact data and every location field except `location_country`
  (values arrive as true/false). The data model stores none of the obscured fields, so this
  only trims `location_name`.
- Sandbox: `https://sandbox.api.peopledatalabs.com/v5/...`, synthetic data, zero credits,
  normal API key (`PDLPY(api_key, sandbox=True)`). Ideal for test fixtures with no real PII.
- Python SDK: `peopledatalabs` 6.4.13 on PyPI, Python ≥ 3.8.
- **Sign-up needs a work email; personal addresses are rejected.** If you would rather not
  use your employer address for a personal take-home, make Diffbot the primary (no card,
  any email) and PDL the stretch. The provider adapter makes that a configuration change.

### 2.2 AWS free tier (verified against AWS docs/pricing on 2026-09-25)

**Program structure.** Accounts created on or after 15 July 2025 choose a **Free plan**
or a **Paid plan** at signup. Both receive $100 in credits (plus up to $100 more for
five onboarding activities); credits expire 12 months after the account opens. The Free
plan ends at the earlier of 6 months or credit exhaustion, and the account then **closes
automatically** (90-day window to upgrade and recover). Always-free allowances apply to
both plans. Accounts created before 15 July 2025 stay on the legacy program (12-month
offers + always-free).

Recommendation: a **new personal account on the Free plan** is the safest way to
guarantee a $0 bill (it cannot be charged); finish the take-home well inside the 6-month
window and still set a Budget alert on day one. An existing legacy account works too and
would cost cents per month at POC volumes.

| Service | Free allowance (type) | Beyond the allowance | Plan for this POC |
|---|---|---|---|
| Lambda | 1M requests + 400k GB-s / month (always free) | $0.20/M requests; arm64 $0.0000133/GB-s, **20% below x86**; INIT phase billed since Aug 2025 | arm64, small memory, short timeouts |
| Step Functions Standard | 4,000 state transitions / month (always free) | $0.025 per 1,000; every retry is a transition | A 100-row batch ≈ 400–600 transitions |
| S3 Standard | **none always-free** (the 5 GB offer is legacy 12-month only) | $0.023/GB-month; PUT $0.005/1k; GET $0.0004/1k | POC ≈ cents, covered by credits |
| DynamoDB | 25 GB + **25 WCU/25 RCU provisioned** (always free); **on-demand has no free allowance** | on-demand $0.625/M writes, $0.125/M reads | Provisioned 5 RCU / 5 WCU, TTL on |
| EventBridge | AWS-service events and S3 notifications free | custom-bus events $1/M | Default bus only |
| CloudWatch | 10 custom metrics, 10 alarms, 5 GB logs, 3 dashboards (always free) | logs $0.50/GB ingested; metric $0.30; alarm $0.10 | Explicit log groups with retention; ≤10 alarms |
| Glue Data Catalog | 1M objects + 1M requests (always free) | crawler $0.44/DPU-hr with a **10-minute minimum** per run | Tables in Terraform + partition projection; **no crawlers** |
| Athena | none | $5/TB scanned, **10 MB minimum per query** (≈ $0.00005) | Dozens of demo queries ≈ cents, covered by credits |
| SSM Parameter Store | standard parameters free; SecureString uses the `aws/ssm` key | advanced tier $0.05/param-month | One standard SecureString |
| Secrets Manager | no reliable trial on new accounts | $0.40/secret-month | Not used |
| KMS | 20k requests / month; AWS-managed keys free | customer-managed key $1/key-month | AWS-managed keys only |
| X-Ray | 100k traces / month (always free) | $5/M traces | Tracing on |
| NAT Gateway | none | $0.045/hr us-east-1 (≈ $33/month idle), $0.059/hr Singapore, plus per-GB | **Avoid** → Lambdas outside the VPC |
| VPC interface endpoint | none (S3/DynamoDB gateway endpoints are free) | $0.01–0.013/hr per AZ (≈ $7/month each) | Avoid |

Cost gotchas that this design deliberately steps around: Lambda-in-VPC (NAT),
customer-managed KMS keys, Secrets Manager, Glue crawlers/jobs, on-demand or
over-provisioned DynamoDB, CloudWatch log groups left at "never expire", Express
Workflows (console tests are billed), and Free-plan credit/6-month expiry.

### 2.3 Local machine (tooling installed 2026-09-25 via `setup-tools.sh`)

| Tool | Status |
|---|---|
| git 2.54, GNU make 3.81, jq 1.7 | present (Apple CLT) |
| Homebrew 7.0.6 | installed to `/opt/homebrew`, PATH line added to `~/.zprofile` |
| Terraform 1.16.4 (hashicorp/tap), AWS CLI 2.37, gh 2.101, gitleaks 8.30 | installed via Homebrew |
| tflint 0.64 | installed from the GitHub release into `~/.local/bin` (no longer in homebrew-core) |
| uv 0.12, Python 3.13.15 (uv-managed), checkov 3.3, pre-commit 4.6 | installed via uv, executables in `~/.local/bin` |
| system python3 | 3.9.6 (Apple CLT — never develop against this) |
| Docker, node | not installed, not needed for the plan |
| AWS access | profile `enrich-dev` via `aws login` (browser sign-in as IAM user `gabe-admin`, no access keys, 12 h sessions); region `ap-southeast-1`; new account on the **Free plan**, credits valid to 2027-03-25; zero-spend budget in place |
| gh login, git identity | see Phase 0 checklist |
| Hardware / OS | Apple silicon (arm64), macOS 26.6.2 |

Implications:

- Phase 0 tooling is done; what remains is account configuration and the repo skeleton.
- This is your personal laptop (Claude is just signed in with a work account), so
  install freely: Homebrew → Terraform, AWS CLI, gh, uv, etc. If you would rather not
  install a toolchain locally, **GitHub Codespaces** (free monthly hours on a personal
  account) or **AWS CloudShell** in the target account (AWS CLI preinstalled, Terraform
  installable into the persistent home directory) both work as alternatives.
- Keep the take-home entirely under your personal GitHub and personal AWS account so
  there is no overlap with employer resources.
- Apple silicon means building Lambda packages for `arm64` is the natural fit
  (Graviton is also cheaper). Pure-Python dependencies avoid cross-compilation pain;
  anything with native code (pyarrow) comes from an AWS-managed layer, not a local build.

---

## 3. Key decisions (mini-ADRs)

| # | Decision | Choice | Why | Rejected |
|---|---|---|---|---|
| D1 | Primary enrichment provider | **People Data Labs** behind a `Provider` interface (Enrichment for rows with context, Identify for name-only rows); `MockProvider` for dev/tests; `DiffbotProvider` as the fallback when PDL returns 402 or the budget is spent (Phase 4b, if time allows) | Named in the brief, `experience[]` work history, free sandbox, Python SDK, credits billed only on matches | FullContact (sales-gated, name needs postal address); hard-wiring one vendor |
| D2 | Ingestion trigger | CSV upload to `s3://…-landing/incoming/<batch_id>/names.csv` → S3 EventBridge notification → rule → `StartExecution` | Serverless, auditable, needs only IAM creds to trigger (no public surface) | API Gateway upload (public surface); cron |
| D3 | Orchestration | **Step Functions Standard**: Validate → Map(Enrich, `MaxConcurrency` = 1 by default, a Terraform variable) → BuildCurated → Notify | One execution per batch, visual run history, built-in Retry/Catch, concurrency cap for API rate limits (PDL Identify allows 10/min), a natural "batch complete" point to write one Parquet file per table | SQS→Lambda chain (no batch-complete point, small-files problem); Glue jobs (not free, overkill) |
| D4 | Raw layer | Every API response (incl. not-found) written to S3 as JSON before any transform | Reprocess the curated layer forever without spending credits; audit trail | Transform in-flight only |
| D5 | Curated layer | Parquet (Snappy), Hive partitions `batch_date=YYYY-MM-DD`, Glue tables defined in Terraform with **partition projection** | Analyst-ready in Athena, no crawler cost, no `MSCK REPAIR` | DynamoDB as the analytical store; CSV; Glue crawler |
| D6 | Idempotency + credit guard | One DynamoDB table (provisioned 5 RCU / 5 WCU, inside the always-free 25/25; TTL enabled): `lookup#<key>` cache items + `budget#<YYYY-MM>` atomic counter; Lambda refuses API calls past `MAX_CREDITS_PER_MONTH` | Re-uploads and retries never double-spend; hard stop protects free credits; on-demand mode has no free allowance | Trusting API headers alone; on-demand billing |
| D7 | Secrets | SSM Parameter Store `SecureString` (AWS-managed key); Terraform creates the parameter with a placeholder and `ignore_changes = [value]`; real value set once via CLI | Free, never in Terraform state or git | Secrets Manager (per-secret monthly fee), env vars in Terraform |
| D8 | Runtime | **Python 3.13 on `arm64`** (runtime supported to mid-2029; both layers below ship 3.13 builds), Powertools for AWS Lambda v3 (Logger/Metrics/Tracer/Parameters) via its public layer, pydantic v2 models | Fast, 20% cheaper duration than x86, typed, structured logs | Python 3.12 (deprecates Oct 2028); Node/TS (weaker Parquet tooling) |
| D9 | Parquet in Lambda | AWS-managed **AWSSDKPandas layer** (`arn:aws:lambda:<region>:336392948345:layer:AWSSDKPandas-Python313-Arm64:<ver>`, bundles pandas + pyarrow 24) on BuildCurated only; Enrich/Validate stay pure-Python | pyarrow is too big/awkward to zip from a Mac; layer is ~55 MB zipped, so check the 250 MB unzipped limit with Powertools attached | Building pyarrow locally; container image (fallback if the size check fails) |
| D10 | Terraform layout | `infra/bootstrap` (state bucket) + `infra/envs/dev` + `infra/modules/*`; S3 backend with **`use_lockfile = true`** (native locking, GA since Terraform 1.11, no DynamoDB lock table); `required_version ~> 1.16`, `hashicorp/aws ~> 6.66`, `archive ~> 2.8` | Clean separation, reproducible `destroy`/`apply`, one fewer resource | One flat `main.tf`; local state; DynamoDB lock table (deprecated) |
| D11 | Networking | Lambdas **not** attached to a VPC by default; `vpc_config` exposed via variables | Outbound-only calls to a public API; a VPC attachment would need a NAT gateway (not free) | Forcing VPC + NAT |
| D12 | CI | GitHub Actions: ruff + pytest + `terraform fmt/validate` + tflint + checkov on every PR; no `apply` from CI for the POC | Cheap, demonstrates practice; avoids storing AWS creds in GitHub for a POC | OIDC deploy pipeline (stretch) |

---

## 4. Architecture

```
 you (IAM creds)                       AWS account (dev)
 ───────────────                       ────────────────────────────────────────────────────────────────
 make upload FILE=names.csv
        │  aws s3 cp
        ▼
 ┌─────────────────────┐   ObjectCreated    ┌──────────────┐   StartExecution   ┌──────────────────────────────┐
 │ S3 landing bucket   │ ─────────────────► │ EventBridge  │ ─────────────────► │ Step Functions (Standard)    │
 │  incoming/<batch>/  │  (EventBridge      │ rule         │                    │  enrichment-pipeline         │
 └─────────────────────┘   notifications)   └──────────────┘                    └──────────────┬───────────────┘
                                                                                                │
        ┌───────────────────────────────────────────────────────────────────────────────────────┤
        │ 1. ValidateInput (Lambda)                                                             │
        │    read CSV, validate/normalise rows, assign batch_id, return rows[] (≤500)          │
        │ 2. Map over rows, MaxConcurrency=1  ── Retry(429/5xx, backoff+jitter)  Catch→failed  │
        │      EnrichPerson (Lambda)                                                            │
        │        cache hit?  ── DynamoDB lookup#<key>  ──► reuse raw S3 key, 0 credits         │
        │        budget ok?  ── DynamoDB budget#<YYYY-MM> counter < MAX_CREDITS                 │
        │        call provider (SSM key) → write raw JSON to S3 raw/… → update cache+counter    │
        │        return {row_id, status, raw_key}                                               │
        │ 3. BuildCurated (Lambda)                                                              │
        │    read raw/ for batch → pydantic models → 3 Parquet tables → curated/… partitions   │
        │ 4. Notify (SNS) on failure; Succeed                                                   │
        └───────────────────────────────────────────────────────────────────────────────────────┘
                 │                          │                              │
                 ▼                          ▼                              ▼
   ┌──────────────────────┐   ┌──────────────────────────┐   ┌──────────────────────────────┐
   │ DynamoDB             │   │ S3 data bucket           │   │ Glue Data Catalog + Athena   │
   │ enrichment_state     │   │  raw/provider=pdl/…json  │   │  db: people_enrichment       │
   │  lookup#… / budget#… │   │  curated/<table>/        │   │  tables: dim_person,         │
   └──────────────────────┘   │    batch_date=…/*.parquet│   │   fact_employment, fact_lookup│
                              └──────────────────────────┘   └──────────────────────────────┘
   Observability: Powertools JSON logs → CloudWatch (14-day retention), EMF metrics
   (CreditsUsed, Matched, NotFound, Ambiguous, Errors), X-Ray, alarms → SNS email.
```

Components and their IAM scope (one role per principal, resource-scoped ARNs):

| Component | Needs |
|---|---|
| EventBridge rule role | `states:StartExecution` on the one state machine |
| Step Functions role | `lambda:InvokeFunction` on the 3 functions, `sns:Publish` on the topic, CloudWatch logs delivery |
| ValidateInput Lambda | `s3:GetObject` on `landing/incoming/*` |
| EnrichPerson Lambda | `ssm:GetParameter` on the key param + `kms:Decrypt` on `aws/ssm`, `dynamodb:GetItem/PutItem/UpdateItem` on the state table, `s3:PutObject` on `data/raw/*`, `cloudwatch:PutMetricData` |
| BuildCurated Lambda | `s3:GetObject/ListBucket` on `data/raw/*`, `s3:PutObject` on `data/curated/*` |
| Athena workgroup | results to `data/athena-results/*`, encryption enforced, per-query scan cutoff |

---

## 5. Enrichment logic

### 5.1 Matching ladder (per input row)

1. `email` present → enrich by email (highest precision).
2. `linkedin_url` present → enrich by profile URL.
3. `first_name + last_name + (company | location)` → enrich by name + context.
4. Name only → **PDL Person Identify** (name alone is valid input; up to 20 candidates
   with `match_score` 1–99; 1 credit per call whatever the outcome). Accept the top
   candidate only if its score ≥ threshold (start at 70, tune) **and** it beats the
   runner-up by a margin (start at 20 points); otherwise status `ambiguous` with the
   candidate count recorded and no person row. Because Identify bills on no-match too,
   it draws from its own `MAX_IDENTIFY_CREDITS_PER_MONTH` budget.
   Alternative for this rung: Diffbot Enhance (bare name accepted, 400 free lookups/month).
5. Optional `event_context.location_hint` (e.g. the event's city/country) is appended
   to every name-only lookup. Registration lists come from an event, so this is a
   realistic, cheap way to lift match rates. Off by default; documented.

### 5.2 Status vocabulary (stored per row in `fact_lookup`)

`matched | not_found | ambiguous | cached | budget_deferred | invalid_input | error`

### 5.3 Failure and API-limit handling (this is a README section — build it in, then describe it)

| Concern | Mechanism |
|---|---|
| Rate limit (HTTP 429) | Provider client reads `x-ratelimit-remaining.minute` and sleeps until `x-ratelimit-reset` when it hits zero (PDL documents no `Retry-After`); Step Functions `Retry` on 429 with exponential backoff + jitter (2 s, ×2, max 5 attempts); `MaxConcurrency=1` keeps a 100-row batch under the 10/min Identify limit (~3 min per batch). |
| Transient 5xx / network | Same Retry block; idempotent by design (cache key), so a retry never double-spends. |
| Credit exhaustion | `budget#<YYYY-MM>` counters (one per billable endpoint) checked before every call; `MAX_CREDITS_PER_MONTH` env vars set below the free allowance to keep a reserve; rows past the budget get `budget_deferred` and the batch still completes. `x-call-credits-spent` increments the counter after each call and `x-totallimit-remaining` reconciles it; an HTTP **402** flips a `provider_exhausted` flag so the remaining rows skip straight to `budget_deferred` (or to the fallback provider if configured). |
| Duplicate names / re-uploaded files | Normalised `lookup_key` (NFKC, casefold, trim, collapse whitespace, plus email/company if present) → DynamoDB cache with TTL; hit = reuse existing raw object, zero credits. |
| Bad rows (empty name, wrong header, bad encoding) | ValidateInput rejects rows individually (`invalid_input`), fails the batch only if the header is wrong or the file is empty; input size capped (rows) for the inline Map. |
| Provider payload shape changes | pydantic models with `extra="ignore"` + contract tests against sandbox fixtures; raw JSON preserved so curated can be rebuilt. |
| Partial batch failure | Map `Catch` routes a row's error into a result object; BuildCurated still runs; execution ends `SUCCEEDED_WITH_ERRORS` semantics via a summary + SNS email listing failed rows. |
| Poison inputs / repeated failure | Execution-level Catch → SNS notification; CloudWatch alarms on Lambda `Errors`, state machine `ExecutionsFailed`. |
| Reprocessing without credits | `make rebuild BATCH=<id>` invokes BuildCurated only, reading `raw/`. |
| Lambda timeouts | Enrich timeout 30s (single HTTP call), Validate 60s, BuildCurated 5 min; memory sized after a first run. |
| Cost leaks | Log retention 14 days, S3 lifecycle (expire `athena-results/` after 7 days, raw after 90 in dev), `terraform destroy` verified, AWS Budget alarm at $5. |

---

## 6. Data model (curated, Parquet)

Partition key on every table: `batch_date` (string, `YYYY-MM-DD`, Hive style).
One file per table per batch. Athena/Glue types in brackets.

**`dim_person`** — one row per matched person per batch

| column | type | source |
|---|---|---|
| person_id | string | provider id (stable key) |
| provider | string | `pdl` etc. |
| batch_id | string | pipeline |
| input_first_name, input_last_name, input_email | string | input row (kept for lineage) |
| full_name, first_name, last_name | string | profile |
| linkedin_url | string | profile |
| location_country, location_name | string | profile (PDL's free plan obscures every location field except country, so `location_name` stays nullable) |
| current_job_title, current_company_name, current_company_industry | string | profile |
| inferred_years_experience | int | profile (if provided) |
| match_likelihood | int/double | provider score |
| enriched_at | timestamp | pipeline |

**`fact_employment`** — one row per position held

| column | type |
|---|---|
| person_id, batch_id | string |
| sequence_no | int (0 = most recent) |
| company_name, company_id, company_website, company_linkedin_url | string |
| company_industry, company_size, company_location_country | string |
| title_name, title_role, title_sub_role | string |
| title_levels | array<string> (or pipe-joined string for simplicity) |
| start_date, end_date | string `YYYY-MM` or `YYYY-MM-DD` as delivered (also `start_year int`) |
| is_current | boolean |

**`fact_lookup`** — one row per input row per run (operational + audit)

| column | type |
|---|---|
| batch_id, row_number | string, int |
| input_first_name, input_last_name, input_email, input_company | string |
| lookup_key | string |
| status | string (vocabulary in 5.2) |
| person_id | string (nullable) |
| likelihood | double (nullable): Enrichment `likelihood` 1–10 or Identify `match_score` 1–99 |
| lookup_method | string: `email`, `linkedin`, `name_context`, `name_only` |
| candidates | int (for `ambiguous`) |
| http_status, error_message | int, string |
| credits_consumed | int |
| provider, raw_s3_key | string |
| requested_at | timestamp |

Optional 4th table if time allows: `dim_education`.

**The three questions in Athena** (ship these in `docs/athena_queries.sql` and the README):

```sql
-- Who are the individuals identified?
SELECT full_name, current_job_title, current_company_name, location_name,
       linkedin_url, match_likelihood
FROM people_enrichment.dim_person
WHERE batch_date = '2026-10-01';

-- What companies have they worked at?
SELECT p.full_name, e.company_name, e.company_industry, e.start_date, e.end_date, e.is_current
FROM people_enrichment.fact_employment e
JOIN people_enrichment.dim_person p USING (person_id, batch_date)
ORDER BY p.full_name, e.sequence_no;

-- What roles have they held?
SELECT p.full_name, e.title_name, e.title_role, e.title_levels, e.company_name
FROM people_enrichment.fact_employment e
JOIN people_enrichment.dim_person p USING (person_id, batch_date)
ORDER BY p.full_name, e.sequence_no;

-- Bonus: match-rate per batch (from the operational table)
SELECT batch_id, status, count(*) AS rows FROM people_enrichment.fact_lookup
GROUP BY 1, 2 ORDER BY 1, 2;
```

PII note for the README: store only what the questions need (no phone numbers,
personal emails, birth dates, gender). Mention retention (lifecycle rules) and that a
production version would need a lawful basis / consent check (PDPA/GDPR) before
enriching registrants.

---

## 7. Repository layout and version-control practice

```
people-enrichment-pipeline/
├── README.md                     # deliverable 4
├── PLAN.md                       # this file (or docs/plan.md)
├── Makefile                      # setup, test, lint, package, plan, apply, upload, run, rebuild, destroy
├── pyproject.toml / uv.lock      # uv-managed; ruff + pytest config
├── .pre-commit-config.yaml       # ruff, terraform fmt/validate, tflint, checkov, gitleaks, end-of-file
├── .gitignore                    # .terraform/, *.tfstate*, .env, build/, __pycache__/
├── .github/workflows/ci.yml      # lint + unit tests + terraform validate/tflint/checkov
├── data/sample/names.csv         # the 3 example rows + a few edge cases
├── src/enrich_pipeline/
│   ├── models.py                 # InputRow, PersonProfile, Employment, LookupResult (pydantic)
│   ├── normalize.py              # name normalisation + lookup_key
│   ├── providers/{base,pdl,mock}.py
│   ├── storage.py                # S3 raw/curated writers, DynamoDB cache + budget
│   ├── transform.py              # raw JSON → table rows
│   ├── cli.py                    # local runner: --provider mock --out ./out
│   └── handlers/{validate_input,enrich,build_curated}.py
├── tests/
│   ├── unit/                     # pure logic
│   ├── handlers/                 # moto + responses/respx
│   └── fixtures/                 # sandbox/mock provider responses (no real PII)
├── infra/
│   ├── bootstrap/                # state bucket (applied once, local state)
│   ├── envs/dev/                 # backend.tf, main.tf, variables.tf, outputs.tf, terraform.tfvars.example
│   └── modules/
│       ├── storage/              # landing + data buckets, DynamoDB table
│       ├── lambda_function/      # generic: role, log group, function, layers, alarms
│       ├── orchestration/        # state machine (ASL template), EventBridge rule, SNS
│       ├── catalog/              # Glue database + tables (partition projection), Athena workgroup
│       └── secrets/              # SSM parameter (placeholder value)
└── docs/
    ├── architecture.md           # Mermaid diagram + component table
    ├── adr/                      # 0001-provider, 0002-orchestration, 0003-storage-format, …
    └── athena_queries.sql
```

Practices to actually follow (and mention in the README):

- `main` protected; work on `feat/*`, `infra/*`, `docs/*` branches; merge via PR even solo (self-review + CI green).
- Conventional Commits (`feat:`, `fix:`, `infra:`, `docs:`, `test:`), small commits.
- Tag milestones (`v0.1.0` = end-to-end with mock, `v0.2.0` = real provider, `v1.0.0` = submission).
- Never commit secrets: gitleaks pre-commit hook, `.tfvars.example` only, SSM for the key.
- Pinned versions everywhere: `required_version`, provider `~>` constraints, `uv.lock`, pinned GitHub Action SHAs.
- Terraform: `fmt`, `validate`, `tflint`, `checkov` clean; every resource tagged (`project`, `env`, `owner`, `managed_by=terraform`).
- Note: you create the GitHub repo yourself. This Claude session runs under a company policy that only allows it to create repositories in the SemiAnalysis GitHub orgs, which is wrong for a personal take-home.

---

## 8. Build phases

Estimates assume focused evenings; total roughly **17–24 hours**. Each phase ends
with a commit/tag and a working state you could submit if you ran out of time.

### Phase 0 — Accounts, tooling, skeleton (2–3 h) — ✅ done 2026-09-25

> Deviations: the CLI signs in with the browser-based `aws login` (12-hour sessions, no
> access keys) instead of `aws configure sso`; IAM Identity Center was skipped because
> enabling Organizations would convert the Free plan to Paid.

1. **Local tooling** (or Codespaces/CloudShell, see 2.3): Homebrew → `terraform` 1.16.x
   (or `tfenv`; OpenTofu 1.12 is a drop-in if you prefer MPL licensing), `awscli`, `gh`,
   `uv`, `tflint`, `pre-commit`, `gitleaks`; `uv tool install checkov`; `uv python install 3.13`.
2. **AWS account**: new personal account on the **Free plan** (see 2.2) or an existing
   one; MFA on root, an admin IAM Identity Center user (or IAM user) for daily work,
   `aws configure sso` / profile `enrich-dev`, default region chosen (ap-southeast-1 or
   us-east-1), **AWS Budget** with email alert at $5, Cost Explorer on. Confirm in the
   console that S3, Glue, and Athena are usable on your plan before writing Terraform.
3. **Provider account**: sign up for the PDL free plan (work email required; the same
   API key works against the sandbox host) and, optionally, a Diffbot free token. Record
   allowances, rate limits, and the comparison table from 2.1 in `docs/adr/0001-provider.md`.
4. **Repo**: `gh repo create` (private), scaffold from section 7, `uv init`, pre-commit
   installed, CI workflow running `ruff` + `pytest` on an empty test.
   *Done when:* CI is green on an empty project, `terraform version` and
   `aws sts get-caller-identity` work locally.

### Phase 1 — Domain model and local pipeline with the mock provider (3–4 h) — ✅ done 2026-09-25, `v0.0.1`

1. pydantic models for input rows and the provider's person schema (fields in section 6).
2. `normalize.py` (+ tests): NFKC, casefold, whitespace, `lookup_key` hashing.
3. `MockProvider` returning fixtures (2 matched, 1 not found, 1 ambiguous, 1 rate-limited).
4. `transform.py`: profile → `dim_person`, `fact_employment`, `fact_lookup` rows (+ tests).
5. `cli.py`: `uv run enrich --input data/sample/names.csv --provider mock --out ./out`
   writes local Parquet; verify the three questions with DuckDB locally.
   *Done when:* the three SQL queries return correct answers against local Parquet. Tag `v0.0.1`.

### Phase 2 — Terraform foundation and Lambda deployment (3–4 h) — ✅ done 2026-09-25, `v0.1.0-infra`

> Deviations: Powertools is vendored into the 5 MB deployment package (pinned by
> `uv.lock`) instead of attached as a layer, so only `build-curated` carries a layer
> (AWS SDK for pandas, for pyarrow); the cache and budget are DynamoDB implementations
> of the same protocols the CLI uses in memory; each function also gets an SQS dead-letter
> queue target and X-Ray tracing. The account's Lambda concurrency quota is 10, so no
> reserved concurrency is set; Step Functions caps parallelism in Phase 3.

1. `infra/bootstrap`: state bucket (versioned, encrypted, public access blocked).
2. `envs/dev` backend (`use_lockfile = true`) + `modules/storage` (landing + data buckets
   using the split `aws_s3_bucket_*` resources: public-access block, TLS-only policy,
   versioning, lifecycle, EventBridge notifications; DynamoDB table provisioned 5/5 with TTL).
3. `modules/secrets`: SSM `SecureString` placeholder with `ignore_changes`.
4. `modules/lambda_function`: role, explicit log group (14 d retention), arm64 Python 3.13
   function, optional layers, error alarm. Packaging: `make package` exports a requirements
   file from `uv`, installs pure-Python deps into `build/`, zips with `archive_file`;
   `source_code_hash` drives updates. Layers: Powertools
   (`arn:aws:lambda:<region>:017000801446:layer:AWSLambdaPowertoolsPythonV3-python313-arm64:<ver>`)
   on all three; AWSSDKPandas on BuildCurated only. Check the unzipped total stays under
   250 MB; if not, BuildCurated becomes a container image.
5. Deploy the three functions with `PROVIDER=mock`; invoke Validate and BuildCurated manually.
   *Done when:* `terraform apply` from clean, `terraform destroy`, `apply` again all succeed;
   checkov/tflint clean. Tag `v0.1.0-infra`.

### Phase 3 — Orchestration and trigger (2–3 h) — ✅ done 2026-09-25, `v0.1.0`

> Deviations: the EventBridge target uses an input transformer so the state machine
> input is always `{bucket, key}`; row-level crashes are caught into `error` records and
> the batch completes with an SNS "completed with row errors" notice; execution logging
> excludes state payloads (PII), documented as an inline checkov skip; the alerts topic
> is unencrypted because CloudWatch alarms cannot publish to an AWS-managed-key topic.

1. ASL definition as a `templatefile()` with Retry/Catch/MaxConcurrency; state machine
   with CloudWatch logging and X-Ray.
2. S3 EventBridge notifications on the landing bucket; rule on `Object Created` with
   prefix `incoming/`; target = state machine; SNS topic + email subscription for failures.
3. `make upload FILE=data/sample/names.csv` → watch execution → curated Parquet appears.
   *Done when:* end-to-end with the mock provider from a CSV upload. Tag `v0.1.0`.

### Phase 4 — Real provider (2–3 h) — ✅ done 2026-09-25, `v0.2.0`

> Deviations: `PdlProvider` is plain `httpx` rather than the SDK (full control over
> headers and status codes). Live findings changed the defaults: the free plan bills
> Identify from a separate pool of only **5 credits/month** (enrichment has 100), so the
> budget guard and the HTTP 402 marker are per call kind and `MAX_IDENTIFY_CREDITS`
> defaults to 2; correct name+company matches score a likelihood of about 4, so
> `min_likelihood` defaults to 4 (6 kept 1 match in 8) and the threshold is part of the
> cache key; the sandbox answers 404 to every name-based lookup, so sandbox tests use
> LinkedIn URLs; `x-ratelimit-reset` is a UTC timestamp. Month-to-date spend after the
> demo: 21/100 enrichment, 3/5 identify. 4b (Diffbot fallback) deferred: the enrichment
> pool is ample; the identify pool is the constraint, so inputs should carry a company.

1. `PdlProvider` via the official `peopledatalabs` SDK (or plain `httpx`): Enrichment for
   rows with context, Identify for name-only rows; parse the rate-limit and credit headers;
   map 200/404/400/402/429 to statuses; unit tests with responses recorded from the sandbox.
2. Run end-to-end against the **sandbox** (`sandbox=True`, 5 calls/min, zero credits) and
   commit the synthetic responses as fixtures.
3. Set the real key in SSM (`make set-api-key`), `MAX_CREDITS_PER_MONTH=70` and
   `MAX_IDENTIFY_CREDITS_PER_MONTH=20`, run a 10-row batch, inspect `fact_lookup`
   statuses and scores, and confirm via `x-call-credits-spent` that 404s and
   `min_likelihood` rejections cost nothing. Tune `min_likelihood` (start at 6) and the
   Identify thresholds.
4. Run the 20–40 row demo batch. Record credits used before/after in the README.
   *Done when:* real data answers the three questions; credits used ≤ 50 % of the allowance. Tag `v0.2.0`.
5. **4b (optional, ~2 h):** `DiffbotProvider` (Enhance API, `employments[]`) wired as the
   fallback on 402/budget exhaustion. Cheap to add behind the adapter and a strong answer
   to "how does it handle API limits".

### Phase 5 — Analytics layer (1–2 h) — ✅ done 2026-09-26

> Deviations: Glue column definitions are generated from `schema.py` into
> `columns.json` (a unit test fails on drift) so transform, Parquet and catalog share one
> source of truth; the four queries are saved as Athena named queries in the workgroup
> and verified from the CLI (`make athena-verify`) rather than by console screenshots.

1. `modules/catalog`: Glue database, three tables with explicit columns and partition
   projection over `batch_date`; Athena workgroup (encrypted results, scan cutoff).
2. `docs/athena_queries.sql` verified in the console; screenshots for the README.
   *Done when:* the three queries run in Athena on the demo batch.

### Phase 6 — Hardening, observability, CI (2–3 h) — ✅ done 2026-09-26

> Deviations: the budget alarms watch month-to-date counters that the enrich function
> publishes as EMF metrics after each lookup (read back from the shared DynamoDB budget),
> one per credit pool; the least-privilege pass is automated (`make iam-check`: wildcard
> statements listed, IAM Access Analyzer validation, zero findings); CI already had the
> Terraform job, so this phase added a gitleaks secret scan and Dependabot; server-side
> branch protection required making the repository public (GitHub Free does not offer it
> on private repos), which the take-home needs anyway; the idempotency proof is a script
> (`make idempotency-proof`) that runs a fresh five-row file twice and asserts the second
> run is entirely cached at zero credits.

1. Alarms: Lambda errors, state-machine failed executions, budget counter ≥ 90 %.
2. Least-privilege pass on every IAM policy (no `*` resources); checkov high/critical = 0.
3. CI: add `terraform fmt -check`, `validate`, tflint, checkov; branch protection requires CI.
4. Idempotency proof: re-upload the same CSV → all rows `cached`, 0 credits.
   *Done when:* CI enforces everything and the re-upload test passes.

### Phase 7 — README, ADRs, demo, teardown (2 h)

1. README per section 11; Mermaid architecture diagram; assumptions list; failure table (5.3).
2. Fresh-account walkthrough: `make bootstrap && make apply && make set-api-key && make upload`.
3. Final `destroy` → `apply` → mock run to prove reproducibility; tag `v1.0.0`.
4. Leave the account destroyed (or only S3/Glue up) to avoid any charges.

---

## 9. Credit and cost budget

**API credits (PDL free plan, per calendar month):**

| Pool | Allowance | Budget guard | Planned spend |
|---|---|---|---|
| Person Enrichment (bills only on a 200 match) | 100 | `MAX_CREDITS_PER_MONTH=70` | 10-row shakedown ≤ 10, demo batch ≤ 40, ≥ 30 in reserve |
| Person Identify (bills every call) | 100 (per-product allowance; exact Identify figure not published, confirm in the dashboard) | `MAX_IDENTIFY_CREDITS_PER_MONTH=20` | name-only rows only |
| Sandbox | unlimited (5 calls/min) | – | all development and CI fixtures |
| Diffbot Enhance (fallback) | 10,000 credits ≈ 400 person lookups | `MAX_DIFFBOT_CREDITS_PER_MONTH=5000` | only after a PDL 402 |

If the demo batch needs more than ~60 real names, split it across two calendar months
or lean on Diffbot for the name-only rows.

**AWS (per month, POC volumes of a few hundred rows):**

| Item | Estimate | Covered by |
|---|---|---|
| Lambda, Step Functions, EventBridge, DynamoDB (provisioned 5/5), CloudWatch, Glue Catalog, SSM, KMS, X-Ray | $0 | always-free allowances |
| S3 (≈ 100 MB, a few thousand requests) | < $0.05 | credits (new account) or cents (legacy) |
| Athena (≈ 50 demo queries at the 10 MB minimum) | < $0.01 | credits or cents |
| **Total** | **≈ $0**, worst case a few cents | Budget alert at $5 |

Rules of thumb built into the plan:

- Development and all automated tests use the mock provider and fixtures — zero credits.
- Sandbox (if available) for integration tests — zero credits.
- First real call happens in Phase 4, after the end-to-end path already works.
- Budget guard set to allowance minus a reserve (target ≥ 30 % left for the final demo).
- Cache guarantees a name is paid for at most once.
- AWS side: everything chosen sits in always-free allowances or is covered by credits at
  POC volumes; the deliberate exclusions are NAT gateways, customer-managed KMS keys,
  Secrets Manager, Glue crawlers/jobs, on-demand DynamoDB, and unlimited log retention.
- Teardown discipline: `terraform destroy` at the end of each working session is cheap
  insurance (the state bucket and Glue tables are the only things worth keeping up).

---

## 10. Testing strategy

| Layer | Tooling | What it proves |
|---|---|---|
| Unit | pytest, hypothesis (optional) for normalisation | lookup_key stability, transform correctness, budget arithmetic |
| Provider contract | `responses`/`respx` with recorded sandbox payloads | status mapping (200/404/429/5xx), header parsing |
| Handler | moto (S3, DynamoDB, SSM), Powertools event classes | handlers work against AWS APIs without an account |
| Infra static | `terraform validate`, tflint, checkov | misconfig and security regressions |
| End-to-end | `make e2e` (upload sample → poll execution → assert Parquet + Athena count) | the deployed pipeline works |
| Idempotency | re-upload test (Phase 6) | zero double-spend |

---

## 11. README outline (deliverable 4)

1. What it does (3 sentences) + architecture diagram.
2. Quick start: prerequisites, `make bootstrap`, `make apply`, `make set-api-key`, `make upload`, where to look.
3. Architecture: components, data flow, why Step Functions/Parquet/Athena, security posture.
4. Data model: the three tables, partitions, the three questions with SQL.
5. Assumptions: input schema, name-only matching, provider choice (link the ADR with the comparison table), VPC stance, free-tier scope, PII minimisation, single region/env.
6. Failure handling and API limits: table from section 5.3, budget guard, idempotency, reprocessing.
7. Cost: what is free, what would cost at scale, teardown.
8. Development: layout, tests, CI, pre-commit, branching.
9. Limitations and next steps (section 12).

---

## 12. Stretch goals (only after v1.0.0)

- Distributed Map with S3 CSV `ItemReader` for files beyond the inline limit.
- Apache Iceberg tables via Athena for upserts/dedup across batches.
- Coresignal as a third provider, plus PDL Company Enrichment (100 free records/month) to
  normalise company names into a `dim_company` table.
- OIDC-based GitHub Actions deploy with `terraform plan` on PR, `apply` on main.
- dbt-athena models for analyst-facing marts; a small QuickSight or DuckDB notebook demo.
- LocalStack for fully offline integration tests.

---

## 13. Assumptions to state (living list — keep updated)

- Input CSV has a header; `first_name,last_name` required; optional identifier columns used when present.
- A name alone may not be uniquely resolvable; the pipeline records `ambiguous` rather than guessing.
- Free-tier credits bound the run size; the design scales by raising the budget and concurrency.
- Lambdas run outside a VPC because they need only outbound HTTPS; a VPC attachment is a variable toggle.
- Single AWS account/region/environment (`dev`); multi-env is a copy of `envs/dev`.
- Enrichment of event registrants would need a lawful basis in production; the POC stores minimal fields.
- PDL's free plan returns contact and fine-grained location fields as true/false; the pipeline never stores them.
- Credit accounting trusts PDL's `x-call-credits-spent` header; the DynamoDB counter is the source of truth for the guard.
