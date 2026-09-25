"""Configuration, AWS clients and key layout shared by the three Lambda handlers."""

from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from enrich_pipeline.models import LookupStatus

METRIC_BY_STATUS: dict[LookupStatus, str] = {
    LookupStatus.MATCHED: "Matched",
    LookupStatus.NOT_FOUND: "NotFound",
    LookupStatus.AMBIGUOUS: "Ambiguous",
    LookupStatus.CACHED: "Cached",
    LookupStatus.BUDGET_DEFERRED: "BudgetDeferred",
    LookupStatus.INVALID_INPUT: "InvalidInput",
    LookupStatus.ERROR: "Error",
}


def _int_or_none(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


@dataclass(frozen=True)
class Settings:
    data_bucket: str
    landing_bucket: str
    state_table: str
    provider: str = "mock"
    api_key_param: str | None = None
    max_rows: int = 500
    max_enrich_credits: int | None = None
    max_identify_credits: int | None = None
    identify_min_score: int = 70
    identify_min_margin: int = 20
    enrich_min_likelihood: int = 6
    location_hint: str | None = None
    metrics_namespace: str = "PeopleEnrichment"

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = env if env is not None else os.environ
        return cls(
            data_bucket=e["DATA_BUCKET"],
            landing_bucket=e.get("LANDING_BUCKET", ""),
            state_table=e["STATE_TABLE"],
            provider=e.get("PROVIDER", "mock"),
            api_key_param=e.get("PDL_API_KEY_PARAM") or None,
            max_rows=int(e.get("MAX_ROWS", "500")),
            max_enrich_credits=_int_or_none(e.get("MAX_ENRICH_CREDITS")),
            max_identify_credits=_int_or_none(e.get("MAX_IDENTIFY_CREDITS")),
            identify_min_score=int(e.get("IDENTIFY_MIN_SCORE", "70")),
            identify_min_margin=int(e.get("IDENTIFY_MIN_MARGIN", "20")),
            enrich_min_likelihood=int(e.get("ENRICH_MIN_LIKELIHOOD", "6")),
            location_hint=e.get("LOCATION_HINT") or None,
            metrics_namespace=e.get("POWERTOOLS_METRICS_NAMESPACE", "PeopleEnrichment"),
        )


def utcnow() -> datetime:
    return datetime.now(UTC)


@functools.lru_cache(maxsize=1)
def s3_client() -> Any:
    import boto3

    return boto3.client("s3")


@functools.lru_cache(maxsize=1)
def dynamodb_resource() -> Any:
    import boto3

    return boto3.resource("dynamodb")


def reset_clients() -> None:
    """Tests call this so clients are created inside the mocked AWS context."""
    s3_client.cache_clear()
    dynamodb_resource.cache_clear()


# ------------------------------------------------------------------ key layout in the data bucket


def input_key(batch_id: str) -> str:
    return f"input/batch_id={batch_id}/input.json"


def results_prefix(batch_id: str) -> str:
    return f"results/batch_id={batch_id}/"


def result_key(batch_id: str, row_number: int) -> str:
    return f"{results_prefix(batch_id)}row={row_number:05d}.json"


def manifest_key(batch_id: str) -> str:
    return f"manifests/{batch_id}.json"


def curated_key(table: str, batch_date: str, batch_id: str) -> str:
    return f"curated/{table}/batch_date={batch_date}/{batch_id}.parquet"
