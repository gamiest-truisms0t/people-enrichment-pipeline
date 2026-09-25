"""Provider-neutral contract every enrichment backend implements."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

ResponseKind = Literal["enrich", "identify"]


class ProviderError(Exception):
    """Transport-level failure: the request never produced an HTTP response."""


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
    def credits_spent(self) -> int:
        value = self.header("x-call-credits-spent")
        try:
            return int(value) if value is not None else 0
        except ValueError:
            return 0

    @property
    def rate_limit_reset_seconds(self) -> float | None:
        value = self.header("x-ratelimit-reset")
        if value is None:
            return None
        try:
            return max(0.0, float(value))
        except ValueError:
            return None

    @property
    def total_credits_remaining(self) -> int | None:
        value = self.header("x-totallimit-remaining")
        try:
            return int(value) if value is not None else None
        except ValueError:
            return None

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
