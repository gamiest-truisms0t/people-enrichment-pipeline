import pytest
from pydantic import ValidationError

from enrich_pipeline.models import Experience, InputRow, PersonProfile, TitleRef


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
    with pytest.raises(ValidationError):
        InputRow(row_number=1, first_name="a", last_name="b", email="not-an-email")


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
