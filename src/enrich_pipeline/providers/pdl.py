"""People Data Labs provider over httpx.

Two endpoints cover the matching ladder:

    GET /v5/person/enrich    strong identifiers (email, profile URL, name + company or
                             location); billed only when a profile is returned (200)
    GET /v5/person/identify  name only; up to 20 candidates with a match_score;
                             billed on every call, matched or not

The sandbox host serves synthetic people for free at 5 calls per minute and accepts the
same API key. Nothing here interprets responses: the Enricher does that from the status
code, body and credit/rate-limit headers.
"""

from __future__ import annotations

from typing import Any

import httpx

from enrich_pipeline import __version__
from enrich_pipeline.providers.base import ProviderError, ProviderResponse, ResponseKind

PRODUCTION_URL = "https://api.peopledatalabs.com/v5"
SANDBOX_URL = "https://sandbox.api.peopledatalabs.com/v5"

ENDPOINTS: dict[ResponseKind, str] = {
    "enrich": "/person/enrich",
    "identify": "/person/identify",
}


def _json_or_none(response: httpx.Response) -> dict[str, Any] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


class PdlProvider:
    name = "pdl"

    def __init__(self, api_key: str, *, sandbox: bool = False, timeout: float = 20.0) -> None:
        if not api_key or not api_key.strip():
            raise ValueError("the People Data Labs API key is empty")
        self.sandbox = sandbox
        self.base_url = SANDBOX_URL if sandbox else PRODUCTION_URL
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=5.0),
            headers={
                "X-Api-Key": api_key.strip(),
                "Accept": "application/json",
                "User-Agent": f"people-enrichment-pipeline/{__version__}",
            },
        )

    def enrich(self, params: dict[str, str]) -> ProviderResponse:
        return self._get("enrich", params)

    def identify(self, params: dict[str, str]) -> ProviderResponse:
        return self._get("identify", params)

    def close(self) -> None:
        self._client.close()

    def _get(self, kind: ResponseKind, params: dict[str, str]) -> ProviderResponse:
        query = {key: value for key, value in params.items() if value not in (None, "")}
        query.setdefault("pretty", "false")
        try:
            response = self._client.get(ENDPOINTS[kind], params=query)
        except httpx.HTTPError as exc:
            raise ProviderError(f"{kind} request failed: {type(exc).__name__}: {exc}") from exc
        return ProviderResponse(
            kind=kind,
            status=response.status_code,
            body=_json_or_none(response),
            headers=dict(response.headers),
        )
