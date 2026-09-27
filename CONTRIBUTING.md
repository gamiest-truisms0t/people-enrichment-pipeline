# Contributing

This repository is a take-home submission, so "contributing" mostly means the author
working in the open. The rules below are the ones the history follows, written down so a
reviewer can check them and a fork can keep them.

## Set up

```bash
make setup        # uv sync and pre-commit install
make check        # ruff plus the unit, provider-contract and handler tests, about ten seconds
make coverage     # the same tests with line coverage; CI fails below 90 %
make precommit    # every pre-commit hook on the whole tree
```

The AWS targets need `make login` (a browser sign-in for a 12-hour session). None of the
tests do: they run against a mock provider and moto-mocked AWS.

## Branches and commits

- Branch from `main` as `feat/…`, `fix/…`, `docs/…`, `chore/…`, `test/…` or `ci/…`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/):
  `type(scope): summary` in the imperative, under 72 characters. Group a pull request's
  work into a few commits by concern (code, infrastructure, docs) rather than one squash
  or fifty steps.
- Never commit secrets. The provider key goes through `make set-api-key`, which stores it
  in SSM without touching Terraform state or git. gitleaks runs in pre-commit and in CI,
  and GitHub's push protection blocks a known secret pattern before it lands.

## Pull requests

1. **Review before opening.** Run a detailed code review of the whole diff (correctness,
   removed behaviour, callers, simplification, efficiency, conventions) and fix what it
   finds before the pull request exists. The body summarises what changed, why, and how it
   was verified.
2. Fill in the checklist from the pull request template.
3. The three protected checks must be green: `lint + unit tests`,
   `terraform fmt / validate / tflint / checkov` and `secret scan (gitleaks)`. CodeQL and,
   for infrastructure changes, the `terraform plan` comment report on the pull request as
   well; read them.
4. Merge only after `gh pr checks <number>` exits 0. `main` is protected and the branch
   must be up to date with it; no force pushes.
5. A merge that touches `infra/`, `src/`, `pyproject.toml` or `uv.lock` deploys through the
   OIDC apply role and then runs the zero-credit demo batch as the gate. Keep the README's
   "Brief coverage and additions" section and the `Unreleased` section of `CHANGELOG.md`
   current in the same pull request.

## Releases

Semantic versioning: patch for fixes, documentation and tooling; minor for features; major
for a breaking change to the input contract or the curated schema.

1. Bump `version` in `pyproject.toml` and `__version__` in
   `src/enrich_pipeline/__init__.py`, then run `uv lock` so `uv sync --locked` in CI still
   passes. The version is stamped on every curated row as `pipeline_version`.
2. Move the `Unreleased` entries of `CHANGELOG.md` under the new version and date, with a
   one-line summary as the first paragraph.
3. Merge, then run `make release TAG=vX.Y.Z`. It creates the annotated tag if it does not
   exist, pushes it, and publishes the GitHub Release with the changelog section as notes.

## Costs

The pipeline runs on an AWS Free plan account and the provider's free plan. Before adding
anything, read the README's Cost section: stay inside the always-free allowances or the
credits, and never spend provider credits from tests or CI (`make e2e` defaults to a file
whose rows are all cached).
