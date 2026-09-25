"""Build a provider from its configured name."""

from __future__ import annotations

from functools import lru_cache

from enrich_pipeline.providers.base import Provider

PROVIDER_NAMES: tuple[str, ...] = ("mock", "pdl")


@lru_cache(maxsize=4)
def _pdl_provider(api_key: str, sandbox: bool) -> Provider:
    """One HTTP client per (key, host) for the life of the process: Lambda containers
    serve many rows, and every row should reuse the same connection pool."""
    from enrich_pipeline.providers.pdl import PdlProvider

    return PdlProvider(api_key, sandbox=sandbox)


def make_provider(name: str, *, api_key: str | None = None, sandbox: bool = False) -> Provider:
    if name == "mock":
        from enrich_pipeline.providers.mock import MockProvider

        return MockProvider()
    if name == "pdl":
        if not api_key:
            raise ValueError("the pdl provider needs an API key (PDL_API_KEY or the key file)")
        return _pdl_provider(api_key, sandbox)
    raise ValueError(f"unknown provider {name!r}; expected one of {PROVIDER_NAMES}")
