import pytest
from pydantic import ValidationError

from enrich_pipeline.models import Experience, InputRow, LookupResult, PersonProfile, TitleRef


def test_input_row_strips_and_blanks_optionals() -> None:
    row = InputRow(row_number=1, first_name=" José ", last_name="García", email=" ", company="")
    assert row.first_name == "José"
    assert row.email is None
    assert row.company is None


def test_input_row_requires_both_names() -> None:
    with pytest.raises(ValidationError):
        InputRow(row_number=1, first_name="  ", last_name="Nguyen")
    with pytest.raises(ValidationError):
        InputRow(row_number=1, first_name="Alex", last_name="")


def test_input_row_email_is_validated_and_casefolded() -> None:
    row = InputRow(row_number=1, first_name="a", last_name="b", email="A@B.io")
    assert row.email == "a@b.io"
    assert row.notes == []
    salvaged = InputRow(row_number=1, first_name="a", last_name="b", email="not-an-email")
    assert salvaged.email is None
    assert salvaged.notes == ["input.email_invalid"]


def test_lookup_results_cached_before_the_guards_still_load() -> None:
    """DynamoDB holds results written before quality_flags and row.notes existed."""
    legacy = {
        "row": {
            "row_number": 3,
            "first_name": "Mark",
            "last_name": "Zuckerberg",
            "email": None,
            "company": "Meta",
            "location": None,
            "linkedin_url": None,
        },
        "lookup_key": "k" * 64,
        "status": "matched",
        "provider": "pdl",
        "method": "name_context",
        "profile": {"id": "p1", "full_name": "mark zuckerberg"},
        "likelihood": 4.0,
        "candidates": 1,
        "http_status": 200,
        "credits_consumed": 1,
        "attempts": 1,
        "raw_ref": "s3://b/raw/x.json",
        "requested_at": "2026-09-26T04:00:00+00:00",
    }
    result = LookupResult.model_validate(legacy)
    assert result.quality_flags == []
    assert result.row.notes == []
    assert (result.row.company, result.lookup_key) == ("Meta", "k" * 64)


def test_input_row_name_rules_and_url_canonicalisation() -> None:
    with pytest.raises(ValidationError, match="placeholder value"):
        InputRow(row_number=1, first_name="test", last_name="test")
    with pytest.raises(ValidationError, match="contains digits"):
        InputRow(row_number=1, first_name="R2", last_name="D2")
    with pytest.raises(ValidationError, match="looks like an email"):
        InputRow(row_number=1, first_name="john@example.com", last_name="Doe")
    row = InputRow(
        row_number=1,
        first_name="Zoë",
        last_name="O'Neil-Ng",
        linkedin_url="https://www.linkedin.com/in/Zoe-Ng/?trk=x",
        company="Self-employed",
    )
    assert row.linkedin_url == "linkedin.com/in/zoe-ng"
    assert row.company is None
    assert row.notes == ["input.company_placeholder"]


def test_profile_coerces_obscured_booleans_and_ignores_extras() -> None:
    profile = PersonProfile.model_validate(
        {
            "id": "x",
            "location_name": True,
            "location_country": "canada",
            "birth_year": 1980,
            "experience": None,
            "inferred_years_experience": False,
        }
    )
    assert profile.location_name is None
    assert profile.location_country == "canada"
    assert profile.experience == []
    assert profile.inferred_years_experience is None
    assert not hasattr(profile, "birth_year")


def test_title_levels_are_deduplicated_in_order() -> None:
    title = TitleRef(levels=["director", "director", None, "vp"])
    assert title.levels == ["director", "vp"]


def test_experience_current_flag_and_start_year() -> None:
    current = Experience(start_date="2018-10-08", end_date=None, is_primary=False)
    assert current.is_current
    assert current.start_year == 2018

    ended = Experience(start_date="2015", end_date="2016-01", is_primary=None)
    assert not ended.is_current
    assert ended.start_year == 2015

    unknown = Experience(start_date=None)
    assert unknown.start_year is None


def test_company_location_is_flattened_to_country() -> None:
    exp = Experience.model_validate({"company": {"name": "acme", "location": {"country": "peru"}}})
    assert exp.company is not None
    assert exp.company.location_country == "peru"


def test_ordered_experience_puts_current_first_then_most_recent() -> None:
    profile = PersonProfile(
        id="p",
        experience=[
            Experience(start_date="2016-07", end_date="2018-12"),
            Experience(start_date="2022-03", is_primary=True),
            Experience(start_date="2019-01", end_date="2022-02"),
        ],
    )
    assert [e.start_date for e in profile.ordered_experience()] == ["2022-03", "2019-01", "2016-07"]
