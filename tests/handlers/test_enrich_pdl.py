"""The enrich handler with the real provider class: key from SSM, HTTP mocked with respx."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import boto3
import httpx
import pytest
import respx
from aws_lambda_powertools.utilities import parameters

from enrich_pipeline.handlers import enrich
from enrich_pipeline.providers.pdl import SANDBOX_URL

from .conftest import REGION, FakeContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "pdl"
PARAM_NAME = "/people-enrichment-test/pdl_api_key"


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


def _mock_route(kind: str, fixture: str) -> respx.Route:
    record = _fixture(fixture)
    return respx.get(f"{SANDBOX_URL}/person/{kind}").mock(
        return_value=httpx.Response(
            record["status"], json=record["body"], headers=record["headers"]
        )
    )


@pytest.fixture
def pdl_env(aws: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    monkeypatch.setenv("PROVIDER", "pdl")
    monkeypatch.setenv("PDL_SANDBOX", "1")
    monkeypatch.setenv("PDL_API_KEY_PARAM", PARAM_NAME)
    parameters.base.DEFAULT_PROVIDERS.clear()  # SSM client must be created inside the mock
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=PARAM_NAME, Type="SecureString", Value="test-api-key-value"
    )
    return aws


@respx.mock
def test_linkedin_row_is_enriched_with_the_key_from_ssm(
    pdl_env: dict[str, Any], lambda_context: FakeContext
) -> None:
    route = _mock_route("enrich", "enrich_200")
    result = enrich.handler(
        {
            "batch_id": "b",
            "batch_date": "2026-10-01",
            "row": {
                "row_number": 1,
                "first_name": "ashley",
                "last_name": "armstrong",
                "linkedin_url": "linkedin.com/in/aa73",
            },
        },
        lambda_context,
    )
    assert result["status"] == "matched"
    assert result["method"] == "linkedin"
    assert result["person_id"] == _fixture("enrich_200")["body"]["data"]["id"]
    assert route.called
    request = route.calls.last.request
    assert request.headers["x-api-key"] == "test-api-key-value"
    assert request.url.params["profile"] == "linkedin.com/in/aa73"
    assert request.url.params["min_likelihood"] == "4"


@respx.mock
def test_name_only_row_uses_identify(pdl_env: dict[str, Any], lambda_context: FakeContext) -> None:
    route = _mock_route("identify", "identify_404")
    result = enrich.handler(
        {
            "batch_id": "b",
            "batch_date": "2026-10-01",
            "row": {"row_number": 2, "first_name": "zqxjv", "last_name": "wkplmt"},
        },
        lambda_context,
    )
    assert result["status"] == "not_found"
    assert result["method"] == "name_only"
    assert route.called
    assert route.calls.last.request.url.params["first_name"] == "zqxjv"


def test_placeholder_key_fails_fast(pdl_env: dict[str, Any], lambda_context: FakeContext) -> None:
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=PARAM_NAME,
        Type="SecureString",
        Value="PLACEHOLDER-run-make-set-api-key",
        Overwrite=True,
    )
    parameters.clear_caches()
    with pytest.raises(RuntimeError, match="make set-api-key"):
        enrich.handler(
            {
                "batch_id": "b",
                "batch_date": "2026-10-01",
                "row": {"row_number": 3, "first_name": "a", "last_name": "b"},
            },
            lambda_context,
        )
