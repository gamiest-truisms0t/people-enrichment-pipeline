"""Build a provider from its configured name."""

from __future__ import annotations

from enrich_pipeline.providers.base import Provider

PROVIDER_NAMES: tuple[str, ...] = ("mock", "pdl")


def make_provider(name: str, *, api_key: str | None = None) -> Provider:
    if name == "mock":
        from enrich_pipeline.providers.mock import MockProvider

        return MockProvider()
    if name == "pdl":
        raise NotImplementedError("the People Data Labs provider arrives in Phase 4")
    raise ValueError(f"unknown provider {name!r}; expected one of {PROVIDER_NAMES}")
