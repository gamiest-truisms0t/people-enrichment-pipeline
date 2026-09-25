"""Deterministic offline provider backed by synthetic fixtures.

Scenarios are keyed by the normalised input name (or by email / profile URL) so
the sample CSV exercises every outcome the pipeline has to handle: strong match,
ambiguous candidates, no match, a one-off rate limit, exhausted credits, server
errors, and transport failures. See `mock_data/profiles.json`.
"""

from __future__ import annotations

import json
from copy import deepcopy
from importlib import resources
from typing import Any

from enrich_pipeline.normalize import normalize_email, normalize_text, normalize_url
from enrich_pipeline.providers.base import ProviderError, ProviderResponse, ResponseKind

NOT_FOUND_MESSAGE = "No records were found matching your request"


def load_fixture(name: str) -> dict[str, Any]:
    package = resources.files("enrich_pipeline.providers.mock_data")
    return json.loads(package.joinpath(name).read_text(encoding="utf-8"))


class MockProvider:
    name = "mock"

    def __init__(self, *, credits_remaining: int = 100) -> None:
        data = load_fixture("profiles.json")
        self._profiles: dict[str, dict[str, Any]] = data["profiles"]
        self._scenarios: dict[str, dict[str, Any]] = data["scenarios"]
        self._by_email: dict[str, str] = data["by_email"]
        self._by_profile: dict[str, str] = data["by_profile"]
        self.credits_remaining = credits_remaining
        self.calls: list[tuple[str, dict[str, str]]] = []
        self._rate_limited: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def _scenario_name(self, params: dict[str, str]) -> str:
        if email := params.get("email"):
            return self._by_email.get(normalize_email(email), "")
        if profile := params.get("profile"):
            return self._by_profile.get(normalize_url(profile), "")
        return normalize_text(f"{params.get('first_name', '')} {params.get('last_name', '')}")

    def _headers(self, spent: int, credit_type: str) -> dict[str, str]:
        return {
            "x-call-credits-spent": str(spent),
            "x-call-credits-type": credit_type,
            "x-ratelimit-limit.minute": "100",
            "x-ratelimit-remaining.minute": "99",
            "x-ratelimit-reset": "0",
            "x-totallimit-remaining": str(self.credits_remaining),
        }

    def _error(
        self, kind: ResponseKind, status: int, error_type: str, message: str, spent: int = 0
    ) -> ProviderResponse:
        credit_type = "enrich" if kind == "enrich" else "person_identify"
        body = {"status": status, "error": {"type": error_type, "message": message}}
        return ProviderResponse(kind, status, body, self._headers(spent, credit_type))

    def _fault(
        self, kind: ResponseKind, name: str, scenario: dict[str, Any]
    ) -> ProviderResponse | None:
        if scenario.get("transport_error"):
            raise ProviderError("simulated connection reset by peer")
        if scenario.get("server_error"):
            return self._error(kind, 500, "internal_server_error", "simulated server error")
        if scenario.get("payment_required"):
            return self._error(
                kind,
                402,
                "payment_required",
                "You have reached your account maximum (all matches have been used)",
            )
        if scenario.get("rate_limit_first") and name not in self._rate_limited:
            self._rate_limited.add(name)
            return self._error(kind, 429, "rate_limit_error", "Rate limit exceeded")
        return None

    # ------------------------------------------------------------------ Provider API

    def enrich(self, params: dict[str, str]) -> ProviderResponse:
        self.calls.append(("enrich", dict(params)))
        name = self._scenario_name(params)
        scenario = self._scenarios.get(name, {})
        if (fault := self._fault("enrich", name, scenario)) is not None:
            return fault
        match = scenario.get("enrich")
        if not match:
            return self._error("enrich", 404, "not_found", NOT_FOUND_MESSAGE)
        self.credits_remaining -= 1
        body = {
            "status": 200,
            "likelihood": match["likelihood"],
            "data": deepcopy(self._profiles[match["id"]]),
        }
        return ProviderResponse("enrich", 200, body, self._headers(1, "enrich"))

    def identify(self, params: dict[str, str]) -> ProviderResponse:
        self.calls.append(("identify", dict(params)))
        name = self._scenario_name(params)
        scenario = self._scenarios.get(name, {})
        if (fault := self._fault("identify", name, scenario)) is not None:
            return fault
        # Identify bills every call, matched or not.
        self.credits_remaining -= 1
        candidates = scenario.get("identify") or []
        if not candidates:
            return self._error("identify", 404, "not_found", NOT_FOUND_MESSAGE, spent=1)
        body = {
            "status": 200,
            "matches": [
                {
                    "data": deepcopy(self._profiles[c["id"]]),
                    "match_score": c["match_score"],
                    "matched_on": ["first_name", "last_name"],
                }
                for c in candidates
            ],
        }
        return ProviderResponse("identify", 200, body, self._headers(1, "person_identify"))
