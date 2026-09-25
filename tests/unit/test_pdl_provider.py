"""PdlProvider and the Enricher against responses recorded from the PDL sandbox.

HTTP is mocked with respx, so these run offline and spend nothing. Re-record the
fixtures with `make record-fixtures` if the provider changes its payloads.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from enrich_pipeline.enricher import Enricher
from enrich_pipeline.models import InputRow, LookupMethod, LookupStatus
from enrich_pipeline.providers.base import ProviderError
from enrich_pipeline.providers.pdl import PRODUCTION_URL, SANDBOX_URL, PdlProvider

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdl"


def fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def response(name: str) -> httpx.Response:
    record = fixture(name)
    return httpx.Response(record["status"], json=record["body"], headers=record["headers"])


def mock(kind: str, name: str) -> respx.Route:
    return respx.get(f"{SANDBOX_URL}/person/{kind}").mock(return_value=response(name))


def test_requires_an_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        PdlProvider("  ")


def test_base_url_follows_the_sandbox_flag() -> None:
    assert PdlProvider("k").base_url == PRODUCTION_URL
    assert PdlProvider("k", sandbox=True).base_url == SANDBOX_URL


@respx.mock
def test_enrich_sends_the_key_and_only_non_blank_params() -> None:
    route = mock("enrich", "enrich_200")
    result = PdlProvider("secret", sandbox=True).enrich(
        {"profile": "linkedin.com/in/aa73", "min_likelihood": "6", "company": "", "location": None}  # type: ignore[dict-item]
    )
    assert result.kind == "enrich"
    assert result.status == 200
    assert result.body is not None
    assert result.body["data"]["id"] == "qWjyEydGMozLkaOMBNmiHE_0000"
    assert result.rate_limit_reset_seconds == 0.0  # recorded reset time is in the past

    request = route.calls.last.request
    assert request.headers["x-api-key"] == "secret"
    assert request.headers["accept"] == "application/json"
    assert request.url.params["profile"] == "linkedin.com/in/aa73"
    assert request.url.params["min_likelihood"] == "6"
    assert request.url.params["pretty"] == "false"
    assert "company" not in request.url.params
    assert "location" not in request.url.params


@respx.mock
def test_not_found_is_free_and_carries_the_message() -> None:
    mock("enrich", "enrich_404")
    result = PdlProvider("k", sandbox=True).enrich({"first_name": "nobody", "last_name": "x"})
    assert result.status == 404
    assert result.credits_spent == 0  # an enrich 404 is never billed, header or not
    assert result.error_message == "No records were found matching your request"


@respx.mock
def test_bad_request_explains_the_minimum_identifiers() -> None:
    mock("enrich", "enrich_400")
    result = PdlProvider("k", sandbox=True).enrich({"first_name": "ashley", "last_name": "a"})
    assert result.status == 400
    assert result.error_message is not None
    assert "minimum combination" in result.error_message


@respx.mock
def test_identify_returns_candidates() -> None:
    mock("identify", "identify_200")
    result = PdlProvider("k", sandbox=True).identify({"profile": "linkedin.com/in/aa73"})
    assert result.kind == "identify"
    assert result.status == 200
    assert result.body is not None
    assert len(result.body["matches"]) == 6
    assert {m["match_score"] for m in result.body["matches"]} == {90}


@respx.mock
def test_transport_errors_become_provider_errors() -> None:
    respx.get(f"{SANDBOX_URL}/person/enrich").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(ProviderError, match="ConnectError"):
        PdlProvider("k", sandbox=True).enrich({"profile": "x"})


@respx.mock
def test_non_json_bodies_are_tolerated() -> None:
    respx.get(f"{SANDBOX_URL}/person/enrich").mock(
        return_value=httpx.Response(502, text="<html>bad gateway</html>")
    )
    result = PdlProvider("k", sandbox=True).enrich({"profile": "x"})
    assert result.status == 502
    assert result.body is None
    assert result.error_message is None


@respx.mock
def test_enricher_end_to_end_with_recorded_responses() -> None:
    enrich_route = respx.get(f"{SANDBOX_URL}/person/enrich")
    enrich_route.side_effect = [response("enrich_200"), response("enrich_404")]
    identify_route = mock("identify", "identify_200")
    enricher = Enricher(PdlProvider("k", sandbox=True), sleep=lambda _: None)

    linkedin = enricher.lookup(
        InputRow(
            row_number=1,
            first_name="ashley",
            last_name="armstrong",
            linkedin_url="linkedin.com/in/aa73",
        )
    )
    assert linkedin.status is LookupStatus.MATCHED
    assert linkedin.method is LookupMethod.LINKEDIN
    assert linkedin.likelihood == 9.0
    assert linkedin.profile is not None
    assert linkedin.profile.full_name == "autumn andrews"
    assert len(linkedin.profile.experience) == 4
    assert linkedin.profile.location_name is None  # obscured boolean coerced
    assert linkedin.profile.location_country == "canada"
    # The sandbox sends no credit header; a billable 200 is then assumed to cost one credit.
    assert linkedin.credits_consumed == 1

    context = enricher.lookup(
        InputRow(row_number=2, first_name="ashley", last_name="armstrong", company="dunn")
    )
    assert context.status is LookupStatus.NOT_FOUND
    assert context.method is LookupMethod.NAME_CONTEXT
    assert context.http_status == 404

    name_only = enricher.lookup(InputRow(row_number=3, first_name="ashley", last_name="armstrong"))
    assert name_only.status is LookupStatus.AMBIGUOUS  # six candidates, all scoring 90
    assert name_only.method is LookupMethod.NAME_ONLY
    assert name_only.candidates == 6
    assert name_only.likelihood == 90.0

    assert enrich_route.call_count == 2
    assert identify_route.call_count == 1


@respx.mock
def test_enricher_honours_retry_after_on_429() -> None:
    limited = httpx.Response(
        429,
        json={"status": 429, "error": {"type": "rate_limit_error", "message": "slow down"}},
        headers={"retry-after": "3", "x-ratelimit-reset": "2000-01-01 00:00:00"},
    )
    route = respx.get(f"{SANDBOX_URL}/person/enrich")
    route.side_effect = [limited, response("enrich_200")]
    sleeps: list[float] = []
    enricher = Enricher(PdlProvider("k", sandbox=True), sleep=sleeps.append)

    result = enricher.lookup(
        InputRow(row_number=1, first_name="a", last_name="b", linkedin_url="linkedin.com/in/aa73")
    )
    assert result.status is LookupStatus.MATCHED
    assert result.attempts == 2
    assert sleeps == [3.0]
