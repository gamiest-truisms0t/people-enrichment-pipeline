# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), versions follow
[Semantic Versioning](https://semver.org/), and every version is a git tag with a GitHub
Release whose notes are the matching section below (`make release TAG=vX.Y.Z`).

## [Unreleased]

## [1.2.6] - 2026-09-27

Property-based tests and reproducible Lambda builds.

### Added

- Property-based tests with Hypothesis for the normalisation and guard functions:
  idempotence, independence from the Unicode form, what a plausible name is, and that any
  text or byte string either parses or raises `InputError`. Deterministic in CI
  (`tests/conftest.py`).

### Fixed

- A bare carriage return inside an unquoted CSV field made the csv module raise an error
  that was not an `InputError`, so `ValidateInput` would have failed with a traceback
  instead of quarantining the upload with a reason. Found by the property tests.

### Changed

- `make package` is reproducible across machines: the console scripts, whose shebang names
  the installing interpreter's path, and the RECORD lines that hash them are dropped, so a
  laptop `make plan` after a CI deploy of the same commit shows no function changes.

## [1.2.5] - 2026-09-27

Documentation for reviewers and operators.

### Added

- A ten-minute reviewer path at the top of the README and `make demo`, which runs the
  sample file through the mock provider offline, dry-runs the guards on the messy sample
  and answers the three questions with DuckDB.
- An entity diagram of the four curated tables and the `person_current` view.
- `docs/runbook.md`: every alert mapped to its meaning, the first command to run and the
  way back, plus the routine operations (key rotation, reprocessing, quarantine, teardown
  and rebuild).
- terraform-docs: each stack and module has a README with generated requirements,
  resources, inputs and outputs tables (`make tf-docs`, pre-commit hook, `make tf-docs-check`
  in CI with a checksum-pinned binary).

## [1.2.4] - 2026-09-27

Repository surface for reviewers, and the one cost guard found blind while measuring.

### Fixed

- The AWS Budget now measures gross usage before credits (`cost_types { include_credit = false }`).
  With the default it netted the Free plan's credits and read $0.00 by construction, so it
  could never alert before the credits were gone.

### Added

- This changelog, and a GitHub Release for every tag since `v0.0.1`, published by
  `make release TAG=…` from the matching section here.
- Repository settings as code: `make repo-settings` applies the description and topics,
  branch deletion on merge, secret scanning with push protection, Dependabot alerts and
  security updates, private vulnerability reporting and CodeQL default setup through the
  GitHub API.
- Community files: MIT licence, contributing guide with the review-before-PR rule and the
  release steps, security policy, code of conduct, pull request template with the
  checklist, issue templates.

### Changed

- README cost section reports measured figures from 27 September 2026: $0.031 of gross
  usage for the month, $139.97 of credit remaining, every always-free meter far below its
  limit.

## [1.2.3] - 2026-09-26

Testing polish.

### Added

- Line-coverage floor of 90 % in the CI test job, with a summary on every run; `make coverage`.
- State-machine definition validation (`make asl-validate`) on every pull request.

### Changed

- The AWS make targets (`upload`, `smoke`, `e2e`) default to the cached demo file, so they
  cost no provider credits.
- README: tests by layer, coverage and counts.

## [1.2.2] - 2026-09-26

### Changed

- README opens with a table mapping each item of the brief to where it lives, followed by
  the additions made on our own initiative.
- PLAN records the final destroy-and-apply check at `v1.2.1`.

## [1.2.1] - 2026-09-26

### Changed

- README assumptions and failure handling rewritten for a first-time reader; step-by-step
  architecture flow.

## [1.2.0] - 2026-09-26

The $0 pass: the remaining best practices that cost nothing on the platforms in use.

### Added

- Circuit breaker shared across invocations; rows in an open window are
  `provider_unavailable` and are not cached.
- `fact_batch_quality` table with saved query 6; `pipeline_version` on every curated row;
  `make rebuild-all`.
- Generated input contract (`docs/input-contract.json`, `make input-contract`) and the
  `enrich validate` dry run.
- Optional `consent` column, enforced before any provider call; `require_consent` variable.
- CloudWatch dashboard and an execution-time alarm; IAM Access Analyzer external-access
  analyzer with findings routed to the alerts topic; $1 Cost Anomaly Detection subscription.

### Changed

- Custom metrics trimmed from 15 to 9 to stay within the free ten.

### Security

- The data bucket policy denies deletes under `raw/` for every principal except the
  account's IAM users and the CI apply role.

## [1.1.0] - 2026-09-26

Production practices.

### Added

- One execution per upload: a batch claim in DynamoDB and a `DuplicateIgnored` state absorb
  repeated S3 or EventBridge deliveries.
- Per-batch credit cap (`max_credits_per_batch`).
- Quarantine for rejected files with the reason as object metadata, a rejected-rows export,
  and `make quarantine`, `make quarantine-get`, `make redrive`.
- `person_current` Glue view: the latest enrichment per person.
- Deploys from GitHub Actions through OIDC roles: a plan comment on pull requests, apply on
  merge gated by a zero-credit run, a daily drift check
  ([ADR 0005](docs/adr/0005-ci-deploys-with-oidc.md)).

### Security

- The provider key is written with `value_wo` and no longer appears in Terraform state; the
  old state versions were purged.

## [1.0.0] - 2026-09-26

Submission.

### Added

- README written around the brief's three topics; ADRs 0002 to 0004; `docs/architecture.md`.
- Data guards at the file, row, match and output layers; `quality_flags` on every row;
  thresholds as Terraform variables; quality warnings in the notification email.
- Account-level S3 public access block in the bootstrap stack; latest-snapshot saved query.
- VPC toggle for the functions, AWS Budget, owner tag; the end-to-end run counts the batch
  in Athena.

### Fixed

- Rows whose enrich invocation crashed still reach `fact_lookup`.

### Verified

- The stack was destroyed and rebuilt from nothing with the data restored, and a follow-up
  plan showed no drift.

## [0.4.0] - 2026-09-26

Hardening, observability, CI.

### Added

- Month-to-date credit gauges and eight CloudWatch alarms.
- `make iam-check` (least privilege) and `make idempotency-proof` (a re-upload spends nothing).
- gitleaks job in CI, Dependabot, branch protection on `main`.

## [0.3.0] - 2026-09-26

Analytics layer.

### Added

- Glue catalog with partition projection and an Athena workgroup; the brief's three
  questions as saved queries over the live curated data; Glue columns generated from
  `schema.py`; `make athena-verify`.

## [0.2.0] - 2026-09-25

Live provider.

### Added

- People Data Labs provider over httpx with recorded sandbox fixtures, wired into the Lambda
  handler and the CLI; `pdl_sandbox` variable; batch report and demo inputs.

### Fixed

- Per-pool credit exhaustion and threshold-aware cache keys.

## [0.1.0] - 2026-09-25

Event-driven pipeline.

### Added

- Step Functions pipeline (validate, Map over rows, build curated) started by an S3 upload
  through EventBridge, with SNS alerts on failure; `make e2e`, `make executions`,
  `make rebuild`, `make asl-validate`.

### Fixed

- Cached hits populate the curated tables of a new batch.

## [0.1.0-infra] - 2026-09-25

Terraform foundation.

### Added

- Bootstrap and dev stacks: landing and data buckets, DynamoDB state table, SSM SecureString,
  three arm64 Python 3.13 Lambdas with least-privilege roles, alarms and a dead-letter queue;
  Lambda packaging, Terraform make targets, smoke script, CI job.

## [0.0.1] - 2026-09-25

Local pipeline.

### Added

- Domain models, name normalisation and CSV ingestion; provider contract, mock provider and
  the enrichment ladder with retries, budget and cache; curated schema, transform, Parquet
  writer, local runner and CLI verified with DuckDB; project skeleton with CI pinned to
  commit SHAs.

[Unreleased]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.6...HEAD
[1.2.6]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.5...v1.2.6
[1.2.5]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.4...v1.2.5
[1.2.4]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.3...v1.2.4
[1.2.3]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.2...v1.2.3
[1.2.2]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.1...v1.2.2
[1.2.1]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.2.0...v1.2.1
[1.2.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v0.4.0...v1.0.0
[0.4.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v0.1.0-infra...v0.1.0
[0.1.0-infra]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/compare/v0.0.1...v0.1.0-infra
[0.0.1]: https://github.com/gamiest-truisms0t/people-enrichment-pipeline/releases/tag/v0.0.1
