from datetime import UTC, datetime

from enrich_pipeline.enricher import EnrichConfig, Enricher
from enrich_pipeline.models import InputRow, LookupMethod, LookupStatus
from enrich_pipeline.providers.mock import MockProvider


def row(number: int, first: str, last: str, **extra: str) -> InputRow:
    return InputRow(row_number=number, first_name=first, last_name=last, **extra)


def make(**config: object) -> tuple[Enricher, MockProvider, list[float]]:
    provider = MockProvider()
    sleeps: list[float] = []
    enricher = Enricher(
        provider,
        config=EnrichConfig(**config),  # type: ignore[arg-type]
        sleep=sleeps.append,
        clock=lambda: datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
    )
    return enricher, provider, sleeps


def test_plan_follows_the_matching_ladder() -> None:
    enricher, _, _ = make()
    assert enricher.plan(row(1, "a", "b", email="a@b.io")).method is LookupMethod.EMAIL
    assert enricher.plan(row(1, "a", "b", linkedin_url="linkedin.com/in/x")).method is (
        LookupMethod.LINKEDIN
    )

    context = enricher.plan(row(1, "a", "b", company="Acme"))
    assert context.method is LookupMethod.NAME_CONTEXT
    assert context.kind == "enrich"
    assert context.params["company"] == "Acme"
    assert context.params["min_likelihood"] == "4"

    name_only = enricher.plan(row(1, "a", "b"))
    assert name_only.method is LookupMethod.NAME_ONLY
    assert name_only.kind == "identify"
    assert "min_likelihood" not in name_only.params


def test_location_hint_upgrades_name_only_rows() -> None:
    hinted, _, _ = make(location_hint="Singapore")
    plain, _, _ = make()
    plan = hinted.plan(row(1, "a", "b"))
    assert plan.method is LookupMethod.NAME_CONTEXT
    assert plan.params["location"] == "Singapore"
    assert plan.key != plain.plan(row(1, "a", "b")).key


def test_identify_strong_single_candidate_matches() -> None:
    enricher, _, _ = make()
    result = enricher.lookup(row(1, "John", "Doe"))
    assert result.status is LookupStatus.MATCHED
    assert result.profile is not None
    assert result.profile.id == "pdl-mock-0001"
    assert result.likelihood == 88
    assert result.candidates == 1
    assert result.credits_consumed == 1
    assert result.method is LookupMethod.NAME_ONLY


def test_identify_is_ambiguous_when_the_margin_is_too_small() -> None:
    enricher, _, _ = make()
    result = enricher.lookup(row(1, "Alex", "Lee"))
    assert result.status is LookupStatus.AMBIGUOUS
    assert result.candidates == 3
    assert result.likelihood == 62
    assert result.profile is None
    assert result.credits_consumed == 1  # identify bills regardless


def test_identify_accepts_a_clear_winner_over_a_runner_up() -> None:
    enricher, _, _ = make()
    result = enricher.lookup(row(1, "Jane", "Smith"))
    assert result.status is LookupStatus.MATCHED
    assert result.candidates == 2
    assert result.likelihood == 90


def test_enrich_by_email_uses_the_enrich_call() -> None:
    enricher, provider, _ = make()
    result = enricher.lookup(row(1, "Jane", "Smith", email="jane.smith@example.com"))
    assert result.status is LookupStatus.MATCHED
    assert result.method is LookupMethod.EMAIL
    assert result.likelihood == 9
    assert provider.calls[0][0] == "enrich"


def test_rate_limit_is_retried_after_the_reset_header() -> None:
    enricher, provider, sleeps = make()
    result = enricher.lookup(row(1, "José", "García", company="Acme Corp"))
    assert result.status is LookupStatus.NOT_FOUND
    assert result.attempts == 2
    assert result.http_status == 404
    assert result.credits_consumed == 0
    assert sleeps == [0.0]
    assert [call[0] for call in provider.calls] == ["enrich", "enrich"]


def test_rate_limit_wait_is_capped_by_max_wait_seconds() -> None:
    from enrich_pipeline.providers.base import ProviderResponse

    class SlowProvider:
        name = "slow"

        def __init__(self) -> None:
            self.calls = 0

        def enrich(self, params: dict[str, str]) -> ProviderResponse:
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse("enrich", 429, None, {"retry-after": "3600"})
            return ProviderResponse("enrich", 404, None)

        def identify(self, params: dict[str, str]) -> ProviderResponse:
            raise AssertionError("not used")

    sleeps: list[float] = []
    enricher = Enricher(
        SlowProvider(), config=EnrichConfig(max_wait_seconds=20.0), sleep=sleeps.append
    )
    result = enricher.lookup(row(1, "a", "b", company="x"))
    assert result.status is LookupStatus.NOT_FOUND
    assert sleeps == [20.0]


def test_identify_thresholds_are_part_of_the_cache_key() -> None:
    strict, _, _ = make(identify_min_score=90)
    relaxed, _, _ = make(identify_min_score=50)
    assert strict.plan(row(1, "a", "b")).key != relaxed.plan(row(1, "a", "b")).key
    # Enrich keys ignore identify thresholds and vice versa.
    assert (
        strict.plan(row(1, "a", "b", company="x")).key
        == relaxed.plan(row(1, "a", "b", company="x")).key
    )


def test_cache_hit_costs_nothing_and_keeps_the_new_row() -> None:
    enricher, provider, _ = make()
    first = enricher.lookup(row(1, "John", "Doe"))
    again = enricher.lookup(row(5, "john", "DOE"))
    assert again.status is LookupStatus.CACHED
    assert again.credits_consumed == 0
    assert again.profile is not None and first.profile is not None
    assert again.profile.id == first.profile.id
    assert again.row.row_number == 5
    assert len(provider.calls) == 1


def test_run_budget_defers_rows_once_spent() -> None:
    enricher, _, _ = make(max_identify_credits=1)
    assert enricher.lookup(row(1, "John", "Doe")).status is LookupStatus.MATCHED
    assert enricher.budget.used("identify") == 1
    assert enricher.budget.used("enrich") == 0
    deferred = enricher.lookup(row(2, "Alex", "Lee"))
    assert deferred.status is LookupStatus.BUDGET_DEFERRED
    assert deferred.credits_consumed == 0
    assert deferred.error_message is not None and "budget" in deferred.error_message
    assert enricher.budget.used("identify") == 1


def test_402_marks_that_call_kind_exhausted_for_the_rest_of_the_run() -> None:
    enricher, provider, _ = make()
    first = enricher.lookup(row(1, "Budget", "Exhausted", company="x"))
    assert first.status is LookupStatus.BUDGET_DEFERRED
    assert first.http_status == 402
    assert enricher.provider_exhausted("enrich")
    assert not enricher.provider_exhausted("identify")

    later = enricher.lookup(row(2, "Jane", "Smith", company="Globex"))  # another enrich call
    assert later.status is LookupStatus.BUDGET_DEFERRED
    assert later.attempts == 0
    assert len(provider.calls) == 1  # no call was made for the deferred row

    # Identify draws on a separate credit pool and keeps working.
    other_pool = enricher.lookup(row(3, "John", "Doe"))
    assert other_pool.status is LookupStatus.MATCHED
    assert len(provider.calls) == 2


def test_server_errors_are_retried_then_reported() -> None:
    enricher, _, sleeps = make(max_attempts=3)
    result = enricher.lookup(row(1, "Server", "Error", company="x"))
    assert result.status is LookupStatus.ERROR
    assert result.attempts == 3
    assert result.http_status == 500
    assert len(sleeps) == 2


def test_transport_errors_become_error_results() -> None:
    enricher, _, _ = make()
    result = enricher.lookup(row(1, "Network", "Failure", company="x"))
    assert result.status is LookupStatus.ERROR
    assert result.error_message is not None and "transport" in result.error_message
