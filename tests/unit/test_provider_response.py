from datetime import UTC, datetime

from enrich_pipeline.providers.base import ProviderResponse, _first_number, _seconds_until


def _response(**headers: str) -> ProviderResponse:
    return ProviderResponse(kind="enrich", status=200, body={}, headers=headers)


def test_first_number_accepts_numbers_and_json_objects() -> None:
    assert _first_number("42") == 42.0
    assert _first_number(" 7.5 ") == 7.5
    assert _first_number('{"minute": 99}') == 99.0
    assert _first_number('{"minute": 30, "day": 900}') == 30.0
    assert _first_number("2026-09-25 14:08:26") is None
    assert _first_number("true") is None
    assert _first_number(None) is None


def test_seconds_until_parses_pdl_timestamps() -> None:
    now = datetime(2026, 9, 25, 14, 8, 0, tzinfo=UTC)
    assert _seconds_until("2026-09-25 14:08:26", now=now) == 26.0
    assert _seconds_until("2026-09-25T14:08:26Z", now=now) == 26.0
    assert _seconds_until("not a time", now=now) is None
    assert _seconds_until(None) is None


def test_rate_limit_reset_prefers_retry_after_then_reset_header() -> None:
    assert (
        _response(**{"Retry-After": "12", "x-ratelimit-reset": "60"}).rate_limit_reset_seconds
        == 12.0
    )
    assert _response(**{"x-ratelimit-reset": '{"minute": 45}'}).rate_limit_reset_seconds == 45.0
    # A timestamp in the past clamps to zero rather than producing a negative wait.
    assert _response(**{"x-ratelimit-reset": "2000-01-01 00:00:00"}).rate_limit_reset_seconds == 0.0
    assert _response().rate_limit_reset_seconds is None


def test_credit_headers_are_case_insensitive_and_tolerant() -> None:
    response = _response(**{"X-Call-Credits-Spent": "1", "X-TotalLimit-Remaining": "97"})
    assert response.credits_spent == 1
    assert response.total_credits_remaining == 97
    assert _response().total_credits_remaining is None


def test_missing_credit_header_assumes_one_credit_only_when_billable() -> None:
    assert ProviderResponse("enrich", 200, {}).credits_spent == 1
    assert ProviderResponse("enrich", 404, {}).credits_spent == 0
    assert ProviderResponse("identify", 404, {}).credits_spent == 1
    assert ProviderResponse("identify", 429, {}).credits_spent == 0
    assert (
        ProviderResponse("enrich", 200, {}, {"x-call-credits-spent": "garbage"}).credits_spent == 1
    )
    assert ProviderResponse("enrich", 200, {}, {"x-call-credits-spent": "0"}).credits_spent == 0


def test_error_message_extraction() -> None:
    body = {"status": 400, "error": {"type": ["invalid_request_error"], "message": "bad input"}}
    assert ProviderResponse("enrich", 400, body).error_message == "bad input"
    assert (
        ProviderResponse("enrich", 404, {"error": "plain string"}).error_message == "plain string"
    )
    assert ProviderResponse("enrich", 200, None).error_message is None
