"""Lambda 1/3: read the uploaded CSV, validate rows, persist the parsed input.

Input  : {"bucket", "key", "version_id"?, "etag"?, "execution_id"?, ...}
Output : {"batch_id", "batch_date", "source", "input_ref", "row_count", "invalid_count",
          "warning_count", "duplicate", "owner_execution", "rejected_rows_ref",
          "rows": [InputRow, ...]}
The rows travel inline so a Step Functions Map can iterate them; the full parsed
input (valid and invalid rows, plus the file-level guard warnings) is also written to
the data bucket for the curated step.

Guards and guarantees:
- The batch id is derived from the object version, and the first execution to claim it
  in DynamoDB owns it. S3 and EventBridge deliver at least once; a duplicate trigger
  returns `duplicate: true` with no rows and the state machine ends without doing work.
  The claim outlives a rejected file (rejection is deterministic for that version, so a
  duplicate delivery is ignored rather than quarantined twice) but is released when the
  function fails unexpectedly, so a retry can claim it.
- The object size is checked before the body is read; parse_csv applies the file-level
  data guards and raises InputError when the file as a whole cannot be processed. Such a
  file is copied to quarantine/files/ with the reason as object metadata, then the error
  propagates, fails the execution and reaches the alerts topic.
- Rows rejected by the row guards are exported to quarantine/rows/<batch_id>.csv so the
  source owner can fix and re-upload them.
"""

from __future__ import annotations

import csv
import io
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from enrich_pipeline.aws.dynamo import BatchRegistry
from enrich_pipeline.aws.s3 import put_json
from enrich_pipeline.handlers.common import (
    Settings,
    batch_id_for_object,
    dynamodb_resource,
    input_key,
    quarantine_file_key,
    rejected_rows_key,
    s3_client,
    utcnow,
)
from enrich_pipeline.ingest import OPTIONAL_COLUMNS, REQUIRED_COLUMNS, InputError, parse_csv
from enrich_pipeline.models import InvalidRow

logger = Logger(service="validate-input")
metrics = Metrics(namespace="PeopleEnrichment", service="validate-input")

METADATA_LIMIT = 1000  # S3 user metadata is capped at 2 KB in total


def _ascii(value: str, limit: int = METADATA_LIMIT) -> str:
    """S3 user metadata must be ASCII; keep the reason readable and within the limit."""
    return value.encode("ascii", "replace").decode("ascii")[:limit]


def quarantine_file(
    s3: Any, settings: Settings, *, data: bytes, source: dict[str, Any], reason: str
) -> str:
    """Keep a rejected upload next to its rejection reason and return its S3 URI."""
    key = quarantine_file_key(utcnow(), source["key"].rsplit("/", 1)[-1])
    s3.put_object(
        Bucket=settings.data_bucket,
        Key=key,
        Body=data,
        ContentType="text/csv",
        Metadata={
            "reason": _ascii(reason),
            "source": _ascii(f"s3://{source['bucket']}/{source['key']}"),
            "version-id": _ascii(source.get("version_id") or ""),
        },
    )
    return f"s3://{settings.data_bucket}/{key}"


def rejected_rows_csv(invalid: list[InvalidRow]) -> str:
    columns = ["row_number", "reason", *REQUIRED_COLUMNS, *OPTIONAL_COLUMNS]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    for row in invalid:
        writer.writerow({"row_number": row.row_number, "reason": row.reason, **row.raw})
    return buffer.getvalue()


def _duplicate_response(batch_id: str, batch_date: str, source: dict[str, Any], owner: str) -> dict:
    return {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": {"bucket": source["bucket"], "key": source["key"]},
        "duplicate": True,
        "owner_execution": owner,
        "input_ref": "",
        "rejected_rows_ref": "",
        "row_count": 0,
        "invalid_count": 0,
        "warning_count": 0,
        "rows": [],
    }


def _process(
    s3: Any,
    settings: Settings,
    *,
    data: bytes,
    source: dict[str, Any],
    batch_id: str,
    batch_date: str,
    execution_id: str,
    now: Any,
    size: int,
) -> dict[str, Any]:
    """Parse, export rejected rows, persist the parsed input, return the Map's rows."""
    parsed = parse_csv(data, max_rows=settings.max_rows, guards=settings.guard_config())

    rejected_rows_ref = ""
    if parsed.invalid:
        rejected_key = rejected_rows_key(batch_id)
        s3.put_object(
            Bucket=settings.data_bucket,
            Key=rejected_key,
            Body=rejected_rows_csv(parsed.invalid).encode("utf-8"),
            ContentType="text/csv",
        )
        rejected_rows_ref = f"s3://{settings.data_bucket}/{rejected_key}"

    document = {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": source,
        "execution_id": execution_id,
        "provider": settings.provider,
        "columns": parsed.columns,
        "ignored_columns": parsed.ignored_columns,
        "encoding": parsed.encoding,
        "delimiter": parsed.delimiter,
        "warnings": parsed.warnings,
        "rejected_rows_ref": rejected_rows_ref,
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
            "source": f"s3://{source['bucket']}/{source['key']}",
            "version_id": source.get("version_id"),
            "bytes": size,
            "encoding": parsed.encoding,
            "delimiter": parsed.delimiter,
            "rows_valid": len(parsed.rows),
            "rows_invalid": len(parsed.invalid),
            "warnings": parsed.warnings,
            "rejected_rows_ref": rejected_rows_ref,
        },
    )
    return {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": {"bucket": source["bucket"], "key": source["key"]},
        "duplicate": False,
        "owner_execution": "",
        "input_ref": input_ref,
        "rejected_rows_ref": rejected_rows_ref,
        "row_count": len(parsed.rows),
        "invalid_count": len(parsed.invalid),
        "warning_count": len(parsed.warnings),
        "rows": document["rows"],
    }


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=True)
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    settings = Settings.from_env()
    bucket = event["bucket"]
    key = event["key"]
    trigger = event.get("trigger") or {}
    version_id = event.get("version_id") or trigger.get("version_id") or None
    execution_id = event.get("execution_id") or "manual"
    now = utcnow()
    batch_date = now.date().isoformat()
    guards = settings.guard_config()
    s3 = s3_client()

    response = s3.get_object(
        Bucket=bucket, Key=key, **({"VersionId": version_id} if version_id else {})
    )
    size = int(response.get("ContentLength") or 0)
    if size > guards.max_input_bytes:
        raise InputError(
            f"s3://{bucket}/{key} is {size:,} bytes, above the {guards.max_input_bytes:,} "
            "byte limit; split the file"
        )
    data = response["Body"].read()
    version_id = version_id or response.get("VersionId") or None
    etag = str(response.get("ETag") or "").strip('"')
    source = {"bucket": bucket, "key": key, "version_id": version_id, "etag": etag}
    batch_id = event.get("batch_id") or batch_id_for_object(
        bucket, key, version_id or etag, response.get("LastModified") or now
    )

    registry = BatchRegistry(dynamodb_resource().Table(settings.state_table))
    owner = registry.claim(batch_id, execution_id=execution_id, source=source)
    if owner is not None:
        logger.warning(
            "duplicate trigger ignored",
            extra={"batch_id": batch_id, "owner_execution": owner, "source": source},
        )
        return _duplicate_response(batch_id, batch_date, source, owner)

    try:
        return _process(
            s3,
            settings,
            data=data,
            source=source,
            batch_id=batch_id,
            batch_date=batch_date,
            execution_id=execution_id,
            now=now,
            size=size,
        )
    except InputError as exc:
        # Deterministic for this object version: keep the claim, so a duplicate delivery of
        # the same rejected file is ignored instead of quarantined and emailed twice.
        quarantined = quarantine_file(s3, settings, data=data, source=source, reason=str(exc))
        logger.warning(
            "input rejected and quarantined",
            extra={"source": f"s3://{bucket}/{key}", "quarantine": quarantined, "reason": str(exc)},
        )
        raise InputError(f"{exc}; file kept at {quarantined}") from None
    except Exception:
        registry.release(batch_id, execution_id=execution_id)  # let a retry claim it
        raise
