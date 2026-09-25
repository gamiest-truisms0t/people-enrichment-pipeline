from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from enrich_pipeline.aws.dynamo import DynamoBudget, DynamoCache
from enrich_pipeline.models import InputRow, LookupResult, LookupStatus


def _result() -> LookupResult:
    return LookupResult(
        row=InputRow(row_number=1, first_name="John", last_name="Doe"),
        lookup_key="k" * 64,
        status=LookupStatus.NOT_FOUND,
        provider="mock",
        http_status=404,
        requested_at=datetime(2026, 10, 1, tzinfo=UTC),
    )


def test_cache_roundtrip(aws: dict[str, Any]) -> None:
    cache = DynamoCache(aws["table"], ttl_days=1)
    assert cache.get("missing") is None
    cache.put("k" * 64, _result())
    loaded = cache.get("k" * 64)
    assert loaded is not None
    assert loaded.status is LookupStatus.NOT_FOUND
    assert loaded.row.first_name == "John"
    item = aws["table"].get_item(Key={"pk": "lookup#" + "k" * 64})["Item"]
    assert item["status"] == "not_found"
    assert item["ttl"] > 0


def test_budget_counts_and_limits(aws: dict[str, Any]) -> None:
    budget = DynamoBudget(
        aws["table"], provider="mock", month="2026-10", limits={"enrich": 2, "identify": None}
    )
    assert budget.allows("enrich")
    assert budget.allows("identify")
    budget.record("enrich", 1)
    budget.record("enrich", 1)
    budget.record("enrich", 0)  # no-op
    assert not budget.allows("enrich")
    assert budget.allows("identify")
    assert budget.spent("enrich") == 2
    assert budget.total_spent == 2

    assert not budget.is_exhausted("enrich")
    budget.mark_exhausted("enrich")
    assert budget.is_exhausted("enrich")
    assert not budget.is_exhausted("identify")  # separate pool

    # A different month starts clean.
    fresh = DynamoBudget(
        aws["table"], provider="mock", month="2026-11", limits={"enrich": 2, "identify": None}
    )
    assert fresh.allows("enrich")
    assert not fresh.is_exhausted("enrich")
