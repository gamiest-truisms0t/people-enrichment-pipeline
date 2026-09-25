"""Lambda 1/3: read the uploaded CSV, validate rows, persist the parsed input.

Input  : {"bucket": "<landing bucket>", "key": "incoming/<folder>/names.csv"}
Output : {"batch_id", "batch_date", "source", "input_ref", "row_count",
          "invalid_count", "rows": [InputRow, ...]}
The rows travel inline so a Step Functions Map can iterate them; the full
parsed input (valid and invalid rows) is also written to the data bucket for
the curated step.
"""

from __future__ import annotations

import io
from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from enrich_pipeline.aws.s3 import get_text, put_json
from enrich_pipeline.handlers.common import Settings, input_key, s3_client, utcnow
from enrich_pipeline.ingest import parse_csv
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

    s3 = s3_client()
    text = get_text(s3, bucket, key)
    parsed = parse_csv(io.StringIO(text), max_rows=settings.max_rows)

    document = {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": {"bucket": bucket, "key": key},
        "provider": settings.provider,
        "columns": parsed.columns,
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
            "rows_valid": len(parsed.rows),
            "rows_invalid": len(parsed.invalid),
        },
    )
    return {
        "batch_id": batch_id,
        "batch_date": batch_date,
        "source": document["source"],
        "input_ref": input_ref,
        "row_count": len(parsed.rows),
        "invalid_count": len(parsed.invalid),
        "rows": document["rows"],
    }
