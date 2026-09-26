# ADR 0003: Curated storage as Parquet in S3 with Glue and Athena

**Status:** accepted, 2026-09-25 (Phase 1 locally, Phase 5 in AWS)

## Context

The brief asks for output "in a structured format that is easy for a data engineer or
analyst to use" and for data that answers three questions: who was identified, which
companies they worked at, which roles they held. Employment history is many rows per
person. Volumes are small (hundreds of rows a batch) but the shape should not need
rework at larger volumes, and nothing may cost money at rest.

## Options considered

| Option | For | Against |
|---|---|---|
| **Parquet (Snappy) in S3, Hive partitions, Glue tables, Athena** | Columnar, typed, compressed; plain SQL for analysts; Glue Data Catalog and S3 at POC volume are free or cents; nothing to run | Needs a catalogue; one file per table per batch means small files at scale |
| DynamoDB as the analytical store | Already present for the cache | Not queryable with SQL joins; would push analysts to export |
| CSV in S3 | Zero tooling | Untyped, no nested values, slow at scale, no schema enforcement |
| Glue crawler to register partitions | Automatic | $0.44 per DPU-hour with a 10-minute minimum per run; a crawler run per batch is the single most expensive item in the design |
| Apache Iceberg tables via Athena | Upserts and deduplication across batches | More moving parts than a POC needs; kept as the next step |

## Decision

Three tables, written by the curated step as **one Parquet file per table per batch**
under `curated/<table>/batch_date=YYYY-MM-DD/<batch_id>.parquet`:

- `dim_person`: one row per matched person **per batch** (a snapshot), with input lineage
  columns, the match score, the lookup method and the data-guard `quality_flags`.
- `fact_employment`: one row per position held, ordered (`sequence_no` 0 = current),
  with company, title, seniority levels and dates as delivered.
- `fact_lookup`: one row per input row, valid or not, with status, reason, credits,
  attempts and the raw-object reference: the operational and audit view.

`src/enrich_pipeline/schema.py` is the single source of truth: the transform builds rows
with exactly those columns, the Parquet writer derives its Arrow schema from them, and
`make glue-columns` generates the Glue column definitions Terraform reads (a unit test
fails when the committed file is stale). Glue tables use **partition projection** over
`batch_date`, so a new batch is queryable the moment its file lands, with no crawler and
no `MSCK REPAIR`. The Athena workgroup enforces encrypted results under a lifecycle-expired
prefix and a per-query scan cutoff. The brief's three questions, an operational view and
a "latest snapshot per person" query are saved in the workgroup.

Raw provider responses are kept as JSON under `raw/` (with TTL by lifecycle rule), so
the curated layer can be rebuilt without spending credits.

## Consequences

- Analysts join `fact_employment` to `dim_person` on `person_id` **and** `batch_id`; a
  person uploaded twice appears once per batch. The saved query 5 gives the latest
  snapshot per person for cross-batch questions.
- Adding a column is one edit in `schema.py`, a regenerated `columns.json` and a Terraform
  apply; older Parquet files read the new column as null.
- Locally the same files are queried with DuckDB (`make query`), so the data model is
  exercised without an AWS account.
- Small files would matter at thousands of batches; compaction or Iceberg would be the
  answer then, not now.
