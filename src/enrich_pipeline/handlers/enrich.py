"""Lambda 2/3: enrich one input row.

Input  : {"batch_id", "batch_date", "row": InputRow}
Output : a compact summary; the full LookupResult is written to
         results/batch_id=<id>/row=<n>.json and the raw provider response to raw/.
State  : DynamoDB holds the idempotency cache and the monthly credit budget, so
         every concurrent invocation shares one view of what has been paid for.
"""

from __future__ import annotations

from typing import Any

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit
from aws_lambda_powertools.utilities.typing import LambdaContext

from enrich_pipeline.aws.dynamo import DynamoBreaker, DynamoBudget, DynamoCache
from enrich_pipeline.aws.s3 import put_json
from enrich_pipeline.enricher import Enricher
from enrich_pipeline.handlers.common import (
    METRIC_BY_STATUS,
    Settings,
    dynamodb_resource,
    result_key,
    s3_client,
)
from enrich_pipeline.models import InputRow
from enrich_pipeline.providers.factory import make_provider
from enrich_pipeline.raw_store import S3RawStore

logger = Logger(service="enrich")
metrics = Metrics(namespace="PeopleEnrichment", service="enrich")


PLACEHOLDER_PREFIX = "PLACEHOLDER"


def provider_api_key(settings: Settings) -> str | None:
    """Fetch the provider key from SSM (cached in-process for 5 minutes); None for mock."""
    if settings.provider != "pdl":
        return None
    if not settings.api_key_param:
        raise RuntimeError("PROVIDER=pdl requires PDL_API_KEY_PARAM")
    from aws_lambda_powertools.utilities.parameters import get_parameter

    key = get_parameter(settings.api_key_param, decrypt=True, max_age=300)
    if not key or str(key).startswith(PLACEHOLDER_PREFIX):
        raise RuntimeError(
            f"{settings.api_key_param} still holds the placeholder; run `make set-api-key`"
        )
    return str(key)


def build_enricher(settings: Settings, *, batch_date: str, batch_id: str) -> Enricher:
    provider = make_provider(
        settings.provider, api_key=provider_api_key(settings), sandbox=settings.pdl_sandbox
    )
    table = dynamodb_resource().Table(settings.state_table)
    return Enricher(
        provider,
        config=settings.enrich_config(
            # Worst case 2 waits x 20 s plus call time stays well inside the 90 s timeout;
            # Step Functions retries the invocation if the row still needs more attempts.
            max_attempts=3,
            max_wait_seconds=20.0,
            max_enrich_credits=settings.max_enrich_credits,
            max_identify_credits=settings.max_identify_credits,
        ),
        cache=DynamoCache(table),
        budget=DynamoBudget(
            table,
            provider=provider.name,
            month=batch_date[:7],
            limits={
                "enrich": settings.max_enrich_credits,
                "identify": settings.max_identify_credits,
            },
            batch_id=batch_id,
            batch_limit=settings.max_credits_per_batch,
        ),
        raw_store=S3RawStore(
            settings.data_bucket,
            provider=provider.name,
            batch_date=batch_date,
            batch_id=batch_id,
            client=s3_client(),
        ),
        breaker=DynamoBreaker(
            table,
            provider=provider.name,
            threshold=settings.breaker_threshold,
            cooldown_seconds=settings.breaker_cooldown_seconds,
        ),
    )


@logger.inject_lambda_context(log_event=False)
@metrics.log_metrics(capture_cold_start_metric=False)  # a custom metric per function otherwise
def handler(event: dict[str, Any], context: LambdaContext) -> dict[str, Any]:
    settings = Settings.from_env()
    batch_id = event["batch_id"]
    batch_date = event["batch_date"]
    row = InputRow.model_validate(event["row"])

    enricher = build_enricher(settings, batch_date=batch_date, batch_id=batch_id)
    result = enricher.lookup(row)

    result_ref = put_json(
        s3_client(),
        settings.data_bucket,
        result_key(batch_id, row.row_number),
        result.model_dump(mode="json"),
    )

    if (status_metric := METRIC_BY_STATUS.get(result.status)) is not None:
        metrics.add_metric(name=status_metric, unit=MetricUnit.Count, value=1)
    metrics.add_metric(name="CreditsSpent", unit=MetricUnit.Count, value=result.credits_consumed)
    # Month-to-date counters from the shared budget; the credit alarms in
    # infra/envs/dev/monitoring.tf watch the Maximum of these against
    # budget_alarm_fraction of each ceiling.
    metrics.add_metric(
        name="EnrichCreditsUsedThisMonth",
        unit=MetricUnit.Count,
        value=enricher.budget.used("enrich"),
    )
    metrics.add_metric(
        name="IdentifyCreditsUsedThisMonth",
        unit=MetricUnit.Count,
        value=enricher.budget.used("identify"),
    )
    logger.info(
        "enriched row",
        extra={
            "batch_id": batch_id,
            "row_number": row.row_number,
            "status": result.status.value,
            "method": result.method.value if result.method else None,
            "credits": result.credits_consumed,
            "attempts": result.attempts,
            "http_status": result.http_status,
        },
    )
    return {
        "batch_id": batch_id,
        "row_number": row.row_number,
        "status": result.status.value,
        "method": result.method.value if result.method else None,
        "person_id": result.person_id,
        "likelihood": result.likelihood,
        "credits_consumed": result.credits_consumed,
        "http_status": result.http_status,
        "attempts": result.attempts,
        "result_ref": result_ref,
        "raw_ref": result.raw_ref,
    }
