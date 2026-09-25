"""Provider-neutral contract every enrichment backend implements."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

ResponseKind = Literal["enrich", "identify"]


class ProviderError(Exception):
    """Transport-level failure: the request never produced an HTTP response."""


def _seconds_until(value: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds from now until a UTC timestamp header value; None if it is not a timestamp."""
    if not value:
        return None
    text = value.strip().replace("T", " ").removesuffix("Z")
    try:
        moment = datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    current = now or datetime.now(UTC)
    return (moment - current).total_seconds()


def _first_number(value: str | None) -> float | None:
    """Parse a header that is either a number or a JSON object of numbers (smallest wins)."""
    if value is None:
        return None
    text = value.strip()
    try:
        return float(text)
    except ValueError:
        pass
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if isinstance(parsed, int | float) and not isinstance(parsed, bool):
        return float(parsed)
    if isinstance(parsed, dict):
        numbers = [float(v) for v in parsed.values() if isinstance(v, int | float)]
        return min(numbers) if numbers else None
    return None


@dataclass(frozen=True)
class ProviderResponse:
    """An HTTP response from a provider, before interpretation."""

    kind: ResponseKind
    status: int
    body: dict[str, Any] | None
    headers: dict[str, str] = field(default_factory=dict)

    def header(self, name: str) -> str | None:
        wanted = name.casefold()
        for key, value in self.headers.items():
            if key.casefold() == wanted:
                return value
        return None

    @property
    def billable(self) -> bool:
        """Whether this response would be billed if the provider sent no credit header:
        a 200 on either endpoint, or a 404 from identify (which bills on no-match too)."""
        return self.status == 200 or (self.kind == "identify" and self.status == 404)

    @property
    def credits_spent(self) -> int:
        """Credits charged for this call, from the provider's header; when the header is
        missing or unreadable, assume one credit for a billable response so the budget
        guard never under-counts."""
        value = self.header("x-call-credits-spent")
        if value is not None:
            try:
                return int(value)
            except ValueError:
                pass
        return 1 if self.billable else 0

    @property
    def rate_limit_reset_seconds(self) -> float | None:
        """Seconds to wait before retrying a 429, from Retry-After or the provider's reset header.

        Accepts a plain number of seconds, a JSON object of numbers keyed by window
        (`{"minute": 60}`), or a UTC timestamp such as `2026-09-25 14:08:26`, which is what
        People Data Labs actually sends in `x-ratelimit-reset`.
        """
        for name in ("retry-after", "x-ratelimit-reset"):
            value = self.header(name)
            seconds = _first_number(value)
            if seconds is None:
                seconds = _seconds_until(value)
            if seconds is not None:
                return max(0.0, seconds)
        return None

    @property
    def total_credits_remaining(self) -> int | None:
        value = _first_number(self.header("x-totallimit-remaining"))
        return int(value) if value is not None else None

    @property
    def error_message(self) -> str | None:
        if not self.body:
            return None
        error = self.body.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            return str(message) if message else None
        return str(error) if error else None


class Provider(Protocol):
    """Two calls cover the matching ladder: enrich (strong identifiers) and identify (name)."""

    name: str

    def enrich(self, params: dict[str, str]) -> ProviderResponse: ...

    def identify(self, params: dict[str, str]) -> ProviderResponse: ...
