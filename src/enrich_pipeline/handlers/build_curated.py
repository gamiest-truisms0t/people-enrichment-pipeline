"""Lambda 3/3: turn a batch's lookup results into the curated Parquet tables.

Input  : {"batch_id", "batch_date"?}
Reads  : input/batch_id=<id>/input.json (invalid rows) and results/batch_id=<id>/*.json
Writes : curated/<table>/batch_date=<date>/<batch_id>.parquet and manifests/<batch_id>.json
Needs the pyarrow layer; nothing else in the pipeline does.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from enrich_pipeline.aws.s3 import get_json, list_keys, put_json, upload_file
from enrich_pipeline.handlers.common import (
    Settings,
    curated_key,
    input_key,
    manifest_key,
    results_prefix,
    s3_client,
    utcnow,
)
from enrich_pipeline.models import InvalidRow, LookupResult
from enrich_pipeline.parquet import write_tables
from enrich_pipeline.transform import build_tables

logger = Logger(service="build-curated")
metrics = Metrics(namespace="PeopleEnrichment", service="build-curated")


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

    tables = build_tables(results, batch_id=batch_id, invalid=invalid, provider=provider, at=now)

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
        "status_counts": dict(sorted(counts.items())),
        "credits_spent": sum(result.credits_consumed for result in results),
        "persons": len(tables["dim_person"]),
        "employment_rows": len(tables["fact_employment"]),
        "files": uploaded,
        "built_at": now.isoformat(),
    }
    manifest["manifest_ref"] = put_json(s3, settings.data_bucket, manifest_key(batch_id), manifest)

    metrics.add_metric(name="PersonsCurated", unit=MetricUnit.Count, value=manifest["persons"])
    metrics.add_metric(
        name="EmploymentRows", unit=MetricUnit.Count, value=manifest["employment_rows"]
    )
    logger.info("built curated tables", extra={k: v for k, v in manifest.items() if k != "files"})
    return manifest
