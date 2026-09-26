"""Lambda 3/3: turn a batch's lookup results into the curated Parquet tables.

Input  : {"batch_id", "batch_date"?, "row_errors"?: [{"row_number", "error", "cause"}]}
Reads  : input/batch_id=<id>/input.json (all parsed rows) and results/batch_id=<id>/*.json
Writes : curated/<table>/batch_date=<date>/<batch_id>.parquet and manifests/<batch_id>.json
Needs the pyarrow layer; nothing else in the pipeline does.

Every valid input row ends up in fact_lookup. A row whose enrich invocation crashed or
timed out after Step Functions' retries has no result object; the Map's Catch turns it
into a `row_errors` entry, and this step records it as an `error` row so the audit table
is complete and the batch can be rebuilt without provider calls.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from enrich_pipeline.aws.s3 import get_json, list_keys, put_json, upload_file
from enrich_pipeline.enricher import EnrichConfig, plan_lookup
from enrich_pipeline.guards import batch_quality, check_tables
from enrich_pipeline.handlers.common import (
    Settings,
    curated_key,
    input_key,
    manifest_key,
    results_prefix,
    s3_client,
    utcnow,
)
from enrich_pipeline.models import InputRow, InvalidRow, LookupResult, LookupStatus
from enrich_pipeline.parquet import write_tables
from enrich_pipeline.transform import build_tables, quality_flags

logger = Logger(service="build-curated")
metrics = Metrics(namespace="PeopleEnrichment", service="build-curated")

UNRECORDED_MESSAGE = "no result recorded: the enrich invocation failed after retries"
MAX_ERROR_MESSAGE = 1000


def unrecorded_rows(
    input_doc: dict[str, Any],
    results: list[LookupResult],
    row_errors: list[dict[str, Any]],
    *,
    provider: str,
    config: EnrichConfig,
    at: datetime,
) -> list[LookupResult]:
    """`error` results for valid input rows that have no result object.

    `row_errors` carries the Step Functions Catch output for crashed rows (Error/Cause);
    a row missing for any other reason gets a generic message. The lookup key and method
    are the ones the enrich function would have used, so the row lines up with a later
    successful run of the same input.
    """
    recorded = {result.row.row_number for result in results}
    causes: dict[int, dict[str, Any]] = {}
    for item in row_errors:
        try:
            causes[int(item["row_number"])] = item
        except (KeyError, TypeError, ValueError):
            logger.warning("row_errors entry without a row_number", extra={"entry": item})

    synthesized: list[LookupResult] = []
    for item in input_doc.get("rows", []):
        if item.get("row_number") in recorded:
            continue
        row = InputRow.model_validate(item)
        cause = causes.get(row.row_number)
        message = UNRECORDED_MESSAGE
        if cause:
            message = f"{cause.get('error') or 'error'}: {cause.get('cause') or ''}".strip(": ")
        plan = plan_lookup(row, config)
        synthesized.append(
            LookupResult(
                row=row,
                lookup_key=plan.key,
                status=LookupStatus.ERROR,
                provider=provider,
                method=plan.method,
                error_message=message[:MAX_ERROR_MESSAGE],
                credits_consumed=0,
                attempts=0,
                requested_at=at,
            )
        )
    return synthesized


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    settings = Settings.from_env()
    batch_id = event["batch_id"]
    s3 = s3_client()
    now = utcnow()

    input_doc = get_json(s3, settings.data_bucket, input_key(batch_id))
    batch_date = event.get("batch_date") or input_doc["batch_date"]
    provider = input_doc.get("provider") or settings.provider
    invalid = [InvalidRow.model_validate(item) for item in input_doc.get("invalid", [])]

    results = [
        LookupResult.model_validate(get_json(s3, settings.data_bucket, key))
        for key in sorted(list_keys(s3, settings.data_bucket, results_prefix(batch_id)))
    ]
    unrecorded = unrecorded_rows(
        input_doc,
        results,
        event.get("row_errors") or [],
        provider=provider,
        config=settings.enrich_config(),
        at=now,
    )
    if unrecorded:
        logger.warning(
            "rows without a result recorded as errors",
            extra={"batch_id": batch_id, "row_numbers": [r.row.row_number for r in unrecorded]},
        )
        results.extend(unrecorded)

    tables = build_tables(results, batch_id=batch_id, invalid=invalid, provider=provider, at=now)
    # Output guards: inconsistent tables fail the step (and the execution) instead of
    # being published; advisory findings go into the manifest and the notification.
    check_tables(tables, expected_lookup_rows=len(results) + len(invalid))
    quality = batch_quality(
        statuses=[result.status.value for result in results],
        flags_per_result=[quality_flags(result) for result in results],
        rows_invalid=len(invalid),
        parse_warnings=input_doc.get("warnings") or [],
        unrecorded_rows=len(unrecorded),
        config=settings.guard_config(),
    )

    workdir = Path(tempfile.mkdtemp(prefix="curated-"))
    files = write_tables(tables, out_dir=workdir, batch_date=batch_date, batch_id=batch_id)
    uploaded = {
        table: upload_file(s3, path, settings.data_bucket, curated_key(table, batch_date, batch_id))
        for table, path in files.items()
    }

    counts = Counter(result.status.value for result in results)
    counts["invalid_input"] += len(invalid)
    manifest = {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "provider": provider,
        "source": input_doc.get("source"),
        "rows_valid": len(results),
        "rows_invalid": len(invalid),
        "rows_unrecorded": len(unrecorded),
        "status_counts": dict(sorted(counts.items())),
        "credits_spent": sum(result.credits_consumed for result in results),
        "persons": len(tables["dim_person"]),
        "employment_rows": len(tables["fact_employment"]),
        "quality": quality.to_dict(),
        "files": uploaded,
        "built_at": now.isoformat(),
    }
    if quality.warnings:
        logger.warning(
            "batch completed with data-quality warnings",
            extra={"batch_id": batch_id, "warnings": quality.warnings},
        )
    manifest["manifest_ref"] = put_json(s3, settings.data_bucket, manifest_key(batch_id), manifest)

    metrics.add_metric(name="PersonsCurated", unit=MetricUnit.Count, value=manifest["persons"])
    metrics.add_metric(
        name="EmploymentRows", unit=MetricUnit.Count, value=manifest["employment_rows"]
    )
    logger.info("built curated tables", extra={k: v for k, v in manifest.items() if k != "files"})
    return manifest
