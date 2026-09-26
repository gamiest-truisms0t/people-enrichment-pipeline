# ADR 0005: Deploys from GitHub Actions through OIDC

**Status:** accepted, 2026-09-26 (Phase 8)

## Context

Until Phase 8 every `terraform apply` ran from a laptop with the operator's browser-based
AWS session, and CI only validated. Reviewers of a pull request could not see what it
would change in the account, nothing checked that the account still matched `main`, and
a deploy depended on one person's machine. Long-lived AWS access keys in GitHub were never
an option.

## Options considered

| Option | For | Against |
|---|---|---|
| **GitHub Actions with OIDC roles**: plan on PR, apply on merge, scheduled drift check | No stored credentials; roles scoped per workflow; plan visible on the PR; free on a public repository | Three roles to reason about; GitHub variables and one secret to configure |
| Terraform Cloud / Scalr / Spacelift | Managed runs, policy engines | Another account and vendor for a POC; overlaps with what Actions does here |
| Keep applying from a laptop | Nothing to build | No plan on PRs, no drift check, single point of failure, an admin session for every change |

## Decision

Three IAM roles in the bootstrap stack, each trusting only this repository through the
GitHub OIDC provider and only the context that needs it:

| Role | Assumable by | Permissions | Used for |
|---|---|---|---|
| `github-plan` | pull requests | read the dev state object, list the state bucket, and the metadata Terraform reads even without a refresh: the `aws/ssm` key alias and description, the project DynamoDB table's description | `terraform plan -refresh=false -lock=false`, posted as a PR comment |
| `github-readonly` | `main` | `ReadOnlyAccess` plus `kms:Decrypt` through SSM | daily drift check with a real refresh; fails the workflow when the account differs from `main` |
| `github-apply` | `main` | `PowerUserAccess` plus IAM management of `people-enrichment-*` roles and `iam:PassRole` to Lambda, Step Functions and EventBridge | plan, apply the saved plan, then a zero-credit end-to-end run as the deploy gate |

The trust policies match GitHub's **immutable subject** format, in which the owner and the
repository carry their numeric ids (`repo:owner@<id>/name@<id>:pull_request`): a renamed
repository keeps its trust, a deleted and re-created one with the same name does not. The
ids are bootstrap variables; `gh api repos/<owner>/<name>/actions/oidc/customization/sub`
shows the prefix GitHub uses.

The PR plan deliberately runs **without refreshing** against AWS: it compares the proposed
configuration with the recorded state, so a pull request from a collaborator cannot read
the account's data (and cannot read the SSM parameter); the only live reads are the key
alias and table descriptions the provider consults while diffing. Drift, which needs a
refresh, is the read-only role's job on a schedule.

Configuration the workflows need lives in GitHub Actions variables (region, state bucket,
role ARNs, non-secret Terraform variables) and one secret (the alert email);
`make ci-config` sets them from the bootstrap outputs and the local tfvars.

The same change fixed a related finding: `aws_ssm_parameter` stores a SecureString's
decrypted value in Terraform state even with `ignore_changes`. The parameter now uses the
provider's write-only argument (`value_wo`), the state holds an empty value, and the
older state versions that contained the key were deleted from the versioned state bucket.

## Consequences

- Every merge to `main` that touches `infra/`, `src/` or the lock file deploys itself;
  a failed deploy gate fails the workflow before anyone uploads a file.
- A rebuild of the CI roles needs the laptop once (`make bootstrap`), which is where
  account-level, applied-once resources already live.
- `make apply` still works locally for emergencies; the drift job will report anything
  applied outside `main` the next morning.
- GitHub Actions minutes are free on a public repository; a private one would spend its
  monthly allowance at roughly five minutes per run.
