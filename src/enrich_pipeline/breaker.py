"""Circuit breaker for the provider: stop calling after a run of failures.

A 5xx storm or a network outage at the provider would otherwise cost every remaining row
its full retry budget (three attempts with waits) for nothing. After `threshold`
consecutive failures the breaker opens for `cooldown_seconds`; rows looked up while it is
open are recorded as `provider_unavailable` (not cached, so a later run retries them).
Any successful response closes it again.

The in-memory implementation serves the CLI and tests; the DynamoDB one in
`enrich_pipeline.aws.dynamo` shares the state across Lambda invocations.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Breaker(Protocol):
    def open_until(self) -> datetime | None:
        """When the breaker closes again, or None if it is closed now."""
        ...

    def record_failure(self) -> None: ...

    def record_success(self) -> None: ...


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MemoryBreaker:
    def __init__(
        self,
        *,
        threshold: int = 3,
        cooldown_seconds: int = 300,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.threshold = max(1, threshold)
        self.cooldown = timedelta(seconds=max(0, cooldown_seconds))
        self.clock = clock
        self.failures = 0
        self._open_until: datetime | None = None

    def open_until(self) -> datetime | None:
        if self._open_until is not None and self._open_until > self.clock():
            return self._open_until
        return None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self._open_until = self.clock() + self.cooldown
            self.failures = 0

    def record_success(self) -> None:
        self.failures = 0
        self._open_until = None


def unavailable_message(until: datetime) -> str:
    return (
        "provider circuit breaker open after consecutive failures; "
        f"skipped without a call, retry after {until.astimezone(UTC):%Y-%m-%d %H:%M:%S} UTC"
    )
