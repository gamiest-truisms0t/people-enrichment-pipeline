"""Configuration, AWS clients and key layout shared by the three Lambda handlers."""

from __future__ import annotations

import functools
import hashlib
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from enrich_pipeline.enricher import EnrichConfig
from enrich_pipeline.guards import GuardConfig
from enrich_pipeline.models import LookupStatus

# CloudWatch custom metrics cost $0.30 a month each beyond the ten that are always free,
# so the pipeline publishes exactly the ones an alarm or the dashboard needs (nine, with
# the two credit gauges, CreditsSpent, RowsInvalid and PersonsCurated). Every other
# count lives in fact_lookup and fact_batch_quality, queryable in Athena at no cost.
METRIC_BY_STATUS: dict[LookupStatus, str] = {
    LookupStatus.MATCHED: "Matched",
    LookupStatus.BUDGET_DEFERRED: "BudgetDeferred",
    LookupStatus.ERROR: "Error",
    LookupStatus.PROVIDER_UNAVAILABLE: "ProviderUnavailable",
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
    pdl_sandbox: bool = False
    max_rows: int = 500
    max_enrich_credits: int | None = None
    max_identify_credits: int | None = None
    # Tuning defaults live in EnrichConfig; env vars override them per deployment.
    identify_min_score: int = EnrichConfig.identify_min_score
    identify_min_margin: int = EnrichConfig.identify_min_margin
    enrich_min_likelihood: int = EnrichConfig.enrich_min_likelihood
    location_hint: str | None = None
    metrics_namespace: str = "PeopleEnrichment"
    # Data guards (batch-level thresholds); field rules are constants in guards.py.
    max_input_bytes: int = GuardConfig.max_input_bytes
    max_invalid_fraction: float = GuardConfig.max_invalid_fraction
    min_match_rate: float = GuardConfig.min_match_rate
    # Credits one batch may spend in total; None disables the cap.
    max_credits_per_batch: int | None = None
    # Provider circuit breaker and the consent rule.
    breaker_threshold: int = EnrichConfig.breaker_threshold
    breaker_cooldown_seconds: int = EnrichConfig.breaker_cooldown_seconds
    require_consent: bool = GuardConfig.require_consent

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = env if env is not None else os.environ
        return cls(
            max_credits_per_batch=_int_or_none(e.get("MAX_CREDITS_PER_BATCH")),
            breaker_threshold=int(e.get("BREAKER_THRESHOLD", cls.breaker_threshold)),
            breaker_cooldown_seconds=int(
                e.get("BREAKER_COOLDOWN_SECONDS", cls.breaker_cooldown_seconds)
            ),
            require_consent=e.get("REQUIRE_CONSENT", "").strip().lower() in ("1", "true", "yes"),
            max_input_bytes=int(e.get("MAX_INPUT_BYTES", cls.max_input_bytes)),
            max_invalid_fraction=float(e.get("MAX_INVALID_FRACTION", cls.max_invalid_fraction)),
            min_match_rate=float(e.get("MIN_MATCH_RATE", cls.min_match_rate)),
            data_bucket=e["DATA_BUCKET"],
            landing_bucket=e.get("LANDING_BUCKET", ""),
            state_table=e["STATE_TABLE"],
            provider=e.get("PROVIDER", "mock"),
            api_key_param=e.get("PDL_API_KEY_PARAM") or None,
            pdl_sandbox=e.get("PDL_SANDBOX", "").strip().lower() in ("1", "true", "yes"),
            max_rows=int(e.get("MAX_ROWS", "500")),
            max_enrich_credits=_int_or_none(e.get("MAX_ENRICH_CREDITS")),
            max_identify_credits=_int_or_none(e.get("MAX_IDENTIFY_CREDITS")),
            identify_min_score=int(e.get("IDENTIFY_MIN_SCORE", cls.identify_min_score)),
            identify_min_margin=int(e.get("IDENTIFY_MIN_MARGIN", cls.identify_min_margin)),
            enrich_min_likelihood=int(e.get("ENRICH_MIN_LIKELIHOOD", cls.enrich_min_likelihood)),
            location_hint=e.get("LOCATION_HINT") or None,
            metrics_namespace=e.get("POWERTOOLS_METRICS_NAMESPACE", "PeopleEnrichment"),
        )

    def enrich_config(self, **overrides: Any) -> EnrichConfig:
        """The matching-ladder tuning from this deployment's settings.

        Both the enrich function (which adds retry and budget settings) and the curated
        step (which reproduces lookup keys for unrecorded rows) build their config here,
        so the two can never disagree on what a row's key is.
        """
        return EnrichConfig(
            identify_min_score=self.identify_min_score,
            identify_min_margin=self.identify_min_margin,
            enrich_min_likelihood=self.enrich_min_likelihood,
            location_hint=self.location_hint,
            breaker_threshold=self.breaker_threshold,
            breaker_cooldown_seconds=self.breaker_cooldown_seconds,
            **overrides,
        )

    def guard_config(self) -> GuardConfig:
        return GuardConfig(
            max_input_bytes=self.max_input_bytes,
            max_invalid_fraction=self.max_invalid_fraction,
            min_match_rate=self.min_match_rate,
            require_consent=self.require_consent,
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


def quarantine_file_key(when: datetime, filename: str) -> str:
    """Where a rejected upload is kept, with the rejection reason as object metadata."""
    return f"quarantine/files/{when:%Y%m%dT%H%M%S}-{filename}"


def rejected_rows_key(batch_id: str) -> str:
    """CSV of the rows a batch rejected, for the source owner to fix and re-upload."""
    return f"quarantine/rows/{batch_id}.csv"


def batch_id_for_object(bucket: str, key: str, identity: str, when: datetime) -> str:
    """Deterministic batch id for one object version, so duplicate triggers share it.

    `identity` is the object's version id (or its ETag when the bucket is unversioned);
    `when` is the object's last-modified time, which keeps the ids sortable by upload.
    """
    digest = hashlib.sha256(f"{bucket}/{key}@{identity}".encode()).hexdigest()[:8]
    return f"{when.astimezone(UTC):%Y%m%dT%H%M%S}-{digest}"


def results_prefix(batch_id: str) -> str:
    return f"results/batch_id={batch_id}/"


def result_key(batch_id: str, row_number: int) -> str:
    return f"{results_prefix(batch_id)}row={row_number:05d}.json"


def manifest_key(batch_id: str) -> str:
    return f"manifests/{batch_id}.json"


def curated_key(table: str, batch_date: str, batch_id: str) -> str:
    return f"curated/{table}/batch_date={batch_date}/{batch_id}.parquet"
