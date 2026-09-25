import pytest

from enrich_pipeline.models import PersonProfile
from enrich_pipeline.providers.base import ProviderError
from enrich_pipeline.providers.mock import MockProvider, load_fixture


def test_identify_strong_match_is_billed_once() -> None:
    response = MockProvider().identify({"first_name": "John", "last_name": "Doe"})
    assert response.status == 200
    assert response.credits_spent == 1
    assert response.body is not None
    assert response.body["matches"][0]["match_score"] == 88


def test_identify_unknown_name_is_404_but_still_billed() -> None:
    response = MockProvider().identify({"first_name": "Nobody", "last_name": "Here"})
    assert response.status == 404
    assert response.credits_spent == 1


def test_enrich_by_email_and_profile_url() -> None:
    provider = MockProvider()
    by_email = provider.enrich({"email": "Jane.Smith@example.com"})
    assert by_email.status == 200
    assert by_email.body is not None
    assert by_email.body["likelihood"] == 9
    assert by_email.body["data"]["id"] == "pdl-mock-0002"

    by_profile = provider.enrich({"profile": "https://www.linkedin.com/in/john-doe-mock/"})
    assert by_profile.status == 200
    assert by_profile.body is not None
    assert by_profile.body["data"]["id"] == "pdl-mock-0001"


def test_enrich_unknown_person_is_404_and_free() -> None:
    response = MockProvider().enrich({"first_name": "Nobody", "last_name": "Here", "company": "x"})
    assert response.status == 404
    assert response.credits_spent == 0
    assert response.error_message == "No records were found matching your request"


def test_rate_limit_fires_only_once_per_name() -> None:
    provider = MockProvider()
    params = {"first_name": "José", "last_name": "García", "company": "Acme Corp"}
    first = provider.enrich(params)
    assert first.status == 429
    assert first.rate_limit_reset_seconds == 0.0
    assert provider.enrich(params).status == 404


def test_fault_scenarios() -> None:
    provider = MockProvider()
    assert provider.enrich({"first_name": "Budget", "last_name": "Exhausted"}).status == 402
    assert provider.enrich({"first_name": "Server", "last_name": "Error"}).status == 500
    with pytest.raises(ProviderError):
        provider.enrich({"first_name": "Network", "last_name": "Failure"})


def test_every_fixture_profile_parses() -> None:
    data = load_fixture("profiles.json")
    for raw in data["profiles"].values():
        profile = PersonProfile.model_validate(raw)
        assert profile.id == raw["id"]


def test_sandbox_sample_parses_into_profiles() -> None:
    """Contract test against a real (synthetic) sandbox response shape."""
    sample = load_fixture("sandbox_search_sample.json")
    profiles = [PersonProfile.model_validate(record) for record in sample["data"]]
    assert profiles
    assert all(p.id for p in profiles)
    assert all(p.location_name is None for p in profiles)  # obscured -> true -> None
    assert any(p.experience for p in profiles)
    assert all(p.location_country for p in profiles)
