# Security policy

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: **Security → Report a vulnerability** on this
repository, or
<https://github.com/gamiest-truisms0t/people-enrichment-pipeline/security/advisories/new>.
Please do not open a public issue for anything that could expose data or credentials. This
is a personal project; expect an acknowledgement within a few days.

## Scope

In scope: anything that would let a principal other than the pipeline's own roles read the
data bucket or the state table, reach the provider key, delete the raw layer or spend
provider credits, and any path by which personal data could leave the account. Out of
scope: findings against People Data Labs, AWS or GitHub themselves, and the behaviour of a
private deployment you operate with your own settings.

## Posture

The README's Security section describes the controls. In short: no public endpoints, public
access blocked at bucket and account level, TLS-only encrypted buckets, one least-privilege
role per principal (checked by `make iam-check` and IAM Access Analyzer), the provider key
only in SSM and never in Terraform state or git, an append-only raw layer, 14-day logs
without payloads, GitHub Actions pinned to commit SHAs, gitleaks in pre-commit and CI,
secret scanning with push protection, Dependabot alerts and CodeQL.

## Supported versions

Only the latest tag on `main` is supported.
