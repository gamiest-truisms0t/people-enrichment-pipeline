# people-enrichment-pipeline

Serverless ETL on AWS that takes a CSV of event registrants (first name, last name,
optional email/company), enriches each person through a free people-profile API, and
lands analyst-ready Parquet tables (people, employment history, lookup log) queryable
in Athena. Provisioned entirely with Terraform. Designed to run on free-tier credits.

**Status:** Phase 0 (tooling, accounts, skeleton). See [PLAN.md](PLAN.md) for the
architecture, data model, failure handling, and build phases. This README is filled in
at Phase 7.

## Quick start (developer)

```bash
make setup    # uv sync + git hooks
make check    # lint + unit tests
make login    # browser sign-in for a 12-hour AWS CLI session
```

## Layout

```
src/enrich_pipeline/   Python package (models, providers, transform, Lambda handlers)
tests/                 unit + handler tests, fixtures (synthetic data only)
infra/                 Terraform: bootstrap (state), envs/dev, modules/
docs/                  ADRs, architecture notes, Athena queries
data/sample/           sample input CSV
scripts/               local setup helpers
```
