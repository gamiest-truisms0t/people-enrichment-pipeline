"""Lambda 1/3: read the uploaded CSV, validate rows, persist the parsed input.

Input  : {"bucket": "<landing bucket>", "key": "incoming/<folder>/names.csv"}
Output : {"batch_id", "batch_date", "source", "input_ref", "row_count",
          "invalid_count", "warning_count", "rows": [InputRow, ...]}
The rows travel inline so a Step Functions Map can iterate them; the full
parsed input (valid and invalid rows, plus the file-level guard warnings) is
also written to the data bucket for the curated step.

Data guards: the object size is checked before the body is read; parse_csv then
applies the file-level guards (encoding, delimiter, header, rejected-row share) and
raises InputError when the file as a whole cannot be processed, which fails the
execution with that message and notifies the alerts topic.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from enrich_pipeline.aws.s3 import put_json
from enrich_pipeline.handlers.common import Settings, input_key, s3_client, utcnow
from enrich_pipeline.ingest import InputError, parse_csv
from enrich_pipeline.runner import new_batch_id

logger = Logger(service="validate-input")
metrics = Metrics(namespace="PeopleEnrichment", service="validate-input")


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    settings = Settings.from_env()
    bucket = event["bucket"]
    key = event["key"]
    now = utcnow()
    batch_id = event.get("batch_id") or new_batch_id(now)
    batch_date = now.date().isoformat()
    guards = settings.guard_config()

    s3 = s3_client()
    response = s3.get_object(Bucket=bucket, Key=key)
    size = int(response.get("ContentLength") or 0)
    if size > guards.max_input_bytes:
        raise InputError(
            f"s3://{bucket}/{key} is {size:,} bytes, above the {guards.max_input_bytes:,} "
            "byte limit; split the file"
        )
    parsed = parse_csv(response["Body"].read(), max_rows=settings.max_rows, guards=guards)

    document = {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": {"bucket": bucket, "key": key},
        "provider": settings.provider,
        "columns": parsed.columns,
        "ignored_columns": parsed.ignored_columns,
        "encoding": parsed.encoding,
        "delimiter": parsed.delimiter,
        "warnings": parsed.warnings,
        "rows": [row.model_dump() for row in parsed.rows],
        "invalid": [row.model_dump() for row in parsed.invalid],
        "created_at": now.isoformat(),
    }
    input_ref = put_json(s3, settings.data_bucket, input_key(batch_id), document)

    metrics.add_metric(name="RowsValid", unit=MetricUnit.Count, value=len(parsed.rows))
    metrics.add_metric(name="RowsInvalid", unit=MetricUnit.Count, value=len(parsed.invalid))
    logger.info(
        "validated input",
        extra={
            "batch_id": batch_id,
            "source": f"s3://{bucket}/{key}",
            "bytes": size,
            "encoding": parsed.encoding,
            "delimiter": parsed.delimiter,
            "rows_valid": len(parsed.rows),
            "rows_invalid": len(parsed.invalid),
            "warnings": parsed.warnings,
        },
    )
    return {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": document["source"],
        "input_ref": input_ref,
        "row_count": len(parsed.rows),
        "invalid_count": len(parsed.invalid),
        "warning_count": len(parsed.warnings),
        "rows": document["rows"],
    }
