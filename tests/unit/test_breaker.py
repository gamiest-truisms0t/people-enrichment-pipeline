"""Provider circuit breaker: opens after consecutive failures, skips rows, closes again."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from enrich_pipeline.breaker import MemoryBreaker
from enrich_pipeline.enricher import EnrichConfig, Enricher
from enrich_pipeline.models import InputRow, LookupStatus
from enrich_pipeline.providers.mock import MockProvider


def row(number: int, first: str, last: str, **extra: str) -> InputRow:
    return InputRow(row_number=number, first_name=first, last_name=last, **extra)


def test_memory_breaker_threshold_and_cooldown() -> None:
    now = [datetime(2026, 10, 1, 12, 0, tzinfo=UTC)]
    breaker = MemoryBreaker(threshold=2, cooldown_seconds=60, clock=lambda: now[0])
    assert breaker.open_until() is None
    breaker.record_failure()
    assert breaker.open_until() is None  # one failure is not a storm
    breaker.record_failure()
    assert breaker.open_until() == now[0] + timedelta(seconds=60)
    now[0] += timedelta(seconds=61)
    assert breaker.open_until() is None  # cooldown over: half-open, the next call decides
    breaker.record_failure()
    breaker.record_success()  # a success resets the count
    breaker.record_failure()
    assert breaker.open_until() is None


def test_enricher_skips_rows_while_the_breaker_is_open_and_recovers() -> None:
    now = [datetime(2026, 10, 1, 12, 0, tzinfo=UTC)]
    provider = MockProvider()
    enricher = Enricher(
        provider,
        config=EnrichConfig(max_attempts=1, breaker_threshold=2, breaker_cooldown_seconds=300),
        sleep=lambda _: None,
        clock=lambda: now[0],
    )

    # Two rows hit the provider's 500s (the mock's "Server Error" scenario) and open it.
    first = enricher.lookup(row(1, "Server", "Error", company="Acme"))
    second = enricher.lookup(row(2, "Server", "Error", company="Globex"))
    assert (first.status, second.status) == (LookupStatus.ERROR, LookupStatus.ERROR)
    assert len(provider.calls) == 2

    # The next row would match, but nothing is sent while the breaker is open.
    skipped = enricher.lookup(row(3, "John", "Doe", company="Acme"))
    assert skipped.status is LookupStatus.PROVIDER_UNAVAILABLE
    assert skipped.attempts == 0 and skipped.credits_consumed == 0
    assert "retry after 2026-10-01 12:05:00 UTC" in (skipped.error_message or "")
    assert len(provider.calls) == 2
    assert enricher.cache.get(enricher.plan(row(3, "John", "Doe", company="Acme")).key) is None

    # After the cooldown the row goes through and the success closes the breaker.
    now[0] += timedelta(seconds=301)
    recovered = enricher.lookup(row(4, "John", "Doe", company="Acme"))
    assert recovered.status is LookupStatus.MATCHED
    assert enricher.breaker.open_until() is None
    assert len(provider.calls) == 3
