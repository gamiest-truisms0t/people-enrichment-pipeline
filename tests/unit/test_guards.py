"""Data guards: file, row, match and batch rules."""

from __future__ import annotations

import codecs

import pytest

from enrich_pipeline.guards import (
    GuardConfig,
    OutputCheckError,
    batch_quality,
    check_tables,
    clean_input_data,
    decode_text,
    file_problem,
    match_flags,
    name_problem,
    sniff_delimiter,
    strip_invisible,
)
from enrich_pipeline.models import Experience, PersonProfile

# --------------------------------------------------------------------------- file layer


def test_decode_text_handles_boms_and_falls_back_to_cp1252() -> None:
    assert decode_text(b"first_name\nJos\xc3\xa9\n") == ("first_name\nJosé\n", "utf-8", [])
    assert decode_text(codecs.BOM_UTF8 + b"a\n")[1] == "utf-8-sig"
    text, encoding, warnings = decode_text(
        codecs.BOM_UTF16_LE + "a,b\nJosé,x\n".encode("utf-16-le")
    )
    assert (text, encoding) == ("a,b\nJosé,x\n", "utf-16")
    assert warnings and warnings[0].startswith("encoding: UTF-16")
    text, encoding, warnings = decode_text(b"first_name\nJos\xe9\n")  # Windows-1252 é
    assert (text, encoding) == ("first_name\nJosé\n", "cp1252")
    assert "0xe9 at offset 14" in warnings[0]


def test_sniff_delimiter() -> None:
    assert sniff_delimiter("first_name,last_name") == ","
    assert sniff_delimiter("first_name;last_name;email") == ";"
    assert sniff_delimiter("first_name\tlast_name") == "\t"
    assert sniff_delimiter("first_name|last_name") == "|"
    assert sniff_delimiter("name") == ","
    assert sniff_delimiter("a,b;c") == ","  # tie -> comma


def test_file_problem_thresholds() -> None:
    config = GuardConfig(max_invalid_fraction=0.5, min_rows_for_fraction=5)
    assert file_problem(total=0, invalid_reasons=[], config=config) == (
        "input has a header but no data rows"
    )
    all_bad = file_problem(
        total=2, invalid_reasons=["first_name: x", "first_name: y"], config=config
    )
    assert all_bad is not None and all_bad.startswith("no valid rows")
    # Small files never trip the fraction rule, however bad they are.
    assert file_problem(total=4, invalid_reasons=["a: b"] * 3, config=config) is None
    too_many = file_problem(
        total=10, invalid_reasons=["first_name: placeholder value 't'"] * 6, config=config
    )
    assert too_many is not None
    assert "6 of 10 rows rejected (60%)" in too_many
    assert "first_name: placeholder value x6" in too_many
    assert file_problem(total=10, invalid_reasons=["a: b"] * 5, config=config) is None


# --------------------------------------------------------------------------- row layer


def test_strip_invisible_removes_control_and_format_characters() -> None:
    assert strip_invisible("Jo​hn\x00﻿") == "John"
    assert strip_invisible("a\tb\nc") == "a b c"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("José", None),
        ("O'Brien-Smith", None),
        ("J.", None),
        ("李", None),
        ("R2", "contains digits"),
        ("---", "contains no letters"),
        ("john@example.com", "looks like an email address (columns swapped?)"),
        ("test", "placeholder value 'test'"),
        ("N/A", "placeholder value 'N/A'"),
        ("First Name", "placeholder value 'First Name'"),
        ("x" * 101, "longer than 100 characters"),
    ],
)
def test_name_problem(value: str, expected: str | None) -> None:
    assert name_problem(value) == expected


def test_clean_input_data_salvages_optional_fields_with_notes() -> None:
    row = clean_input_data(
        {
            "row_number": 1,
            "first_name": " Jo​hn ",
            "last_name": "Doe",
            "email": "john.doe@example",
            "company": "N/A",
            "location": "x" * 201,
            "linkedin_url": "https://www.LinkedIn.com/in/John-Doe/?trk=1",
        }
    )
    assert row["first_name"] == "John"
    assert row["email"] is None
    assert row["company"] is None
    assert row["location"] is None
    assert row["linkedin_url"] == "linkedin.com/in/john-doe"  # query string dropped, canonical
    assert row["notes"] == [
        "input.email_invalid",
        "input.company_placeholder",
        "input.location_too_long",
    ]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.linkedin.com/in/jane-tan/", "linkedin.com/in/jane-tan"),
        ("https://sg.linkedin.com/in/jane-tan?originalSubdomain=sg", "linkedin.com/in/jane-tan"),
        ("uk.linkedin.com/pub/jane-tan/1/2/3", "linkedin.com/pub/jane-tan"),
        ("linkedin.com/company/acme", None),
        ("https://twitter.com/jane", None),
    ],
)
def test_linkedin_urls_are_canonicalised_or_dropped(url: str, expected: str | None) -> None:
    row = clean_input_data({"first_name": "Jane", "last_name": "Tan", "linkedin_url": url})
    assert row["linkedin_url"] == expected
    assert row["notes"] == ([] if expected else ["input.linkedin_url_invalid"])


def test_real_surnames_that_look_like_placeholders_pass() -> None:
    for surname in ("Guest", "Bar", "Sample", "User", "Foo"):
        assert name_problem(surname) is None


def test_clean_input_data_keeps_good_values_and_is_idempotent() -> None:
    data = {
        "row_number": 1,
        "first_name": "Jane",
        "last_name": "Smith",
        "email": "Jane@Example.com",
        "company": "Globex",
        "location": "",
        "linkedin_url": "not-a-profile",
    }
    once = clean_input_data(data)
    assert once["email"] == "Jane@Example.com"  # the model casefolds later
    assert once["company"] == "Globex"
    assert once["location"] is None
    assert once["linkedin_url"] is None
    assert once["notes"] == ["input.linkedin_url_invalid"]
    assert clean_input_data(once) == once


# --------------------------------------------------------------------------- match layer


def _profile(**overrides: object) -> PersonProfile:
    base: dict[str, object] = {
        "id": "p1",
        "full_name": "john doe",
        "first_name": "john",
        "last_name": "doe",
        "job_title": "engineer",
        "job_company_name": "acme",
        "experience": [{"start_date": "2020-01", "end_date": None, "is_primary": True}],
    }
    return PersonProfile.model_validate({**base, **overrides})


def test_match_flags_clean_profile_has_none() -> None:
    flags = match_flags(
        input_first_name="John",
        input_last_name="Doe",
        profile=_profile(),
        likelihood=8,
        likelihood_floor=4,
    )
    assert flags == []


def test_match_flags_name_mismatch_and_floor() -> None:
    flags = match_flags(
        input_first_name="Mary",
        input_last_name="Barra",
        profile=_profile(full_name="maria garcia", first_name="maria", last_name="garcia"),
        likelihood=4,
        likelihood_floor=4,
    )
    assert flags == ["match.name_mismatch", "match.likelihood_at_floor"]
    # Compound surnames match on any shared token, and identify has no floor.
    assert (
        match_flags(
            input_first_name="José",
            input_last_name="García López",
            profile=_profile(full_name="jose garcia", first_name="jose", last_name="garcia"),
            likelihood=70,
            likelihood_floor=None,
        )
        == []
    )


def test_match_flags_sparse_profile_and_dates() -> None:
    profile = _profile(
        full_name=None,
        job_title=None,
        job_company_name=None,
        experience=[
            {"start_date": "2020-13", "end_date": None},
            {"start_date": "2021-05", "end_date": "2019-01"},
        ],
    )
    flags = match_flags(
        input_first_name="John",
        input_last_name="Doe",
        profile=profile,
        likelihood=None,
        likelihood_floor=4,
    )
    # 2020-13 has no such month; the second position ends before it starts.
    assert flags == ["match.sparse_profile", "match.malformed_dates", "match.end_before_start"]
    ordered = _profile(experience=[{"start_date": "2021-05", "end_date": "2019-01"}])
    assert "match.end_before_start" in match_flags(
        input_first_name="John",
        input_last_name="Doe",
        profile=ordered,
        likelihood=None,
        likelihood_floor=None,
    )
    assert "match.no_employment" in match_flags(
        input_first_name="John",
        input_last_name="Doe",
        profile=_profile(experience=[]),
        likelihood=None,
        likelihood_floor=None,
    )
    assert isinstance(ordered.experience[0], Experience)
    # Well-formed dates in every shape the provider uses pass without a flag.
    fine = _profile(
        experience=[
            {"start_date": "2019", "end_date": "2020-12"},
            {"start_date": "2021-05-03", "end_date": None},
        ]
    )
    assert (
        match_flags(
            input_first_name="John",
            input_last_name="Doe",
            profile=fine,
            likelihood=None,
            likelihood_floor=None,
        )
        == []
    )


# --------------------------------------------------------------------------- batch layer


def test_batch_quality_warnings() -> None:
    config = GuardConfig(min_match_rate=0.2, min_rows_for_match_rate=5, warn_invalid_fraction=0.2)
    report = batch_quality(
        statuses=["not_found"] * 5 + ["matched"],
        flags_per_result=[[]] * 5 + [["match.likelihood_at_floor"]],
        rows_invalid=2,
        parse_warnings=["delimiter: ';' detected instead of ','"],
        unrecorded_rows=1,
        config=config,
    )
    assert report.rows_valid == 6 and report.rows_invalid == 2
    assert report.match_rate == pytest.approx(1 / 6)
    assert report.flagged_matches == 1
    assert report.flag_counts == {"match.likelihood_at_floor": 1}
    kinds = [w.split(":")[0] for w in report.warnings]
    assert kinds == [
        "delimiter",
        "low_match_rate",
        "high_invalid_rate",
        "flagged_matches",
        "unrecorded_rows",
    ]
    as_dict = report.to_dict()
    assert as_dict["warning_count"] == 5
    assert as_dict["notes"].startswith("delimiter:")


def test_batch_quality_quiet_on_a_good_batch() -> None:
    report = batch_quality(
        statuses=["matched", "cached", "not_found"],
        flags_per_result=[[], [], []],
        rows_invalid=0,
        config=GuardConfig(),
    )
    assert report.warnings == []
    assert report.to_dict()["notes"] == "none"
    # Too few rows for the match-rate rule, even at 0 %.
    tiny = batch_quality(
        statuses=["not_found"] * 4, flags_per_result=[[]] * 4, rows_invalid=0, config=GuardConfig()
    )
    assert tiny.warnings == []


def _tables() -> dict[str, list[dict[str, object]]]:
    return {
        "dim_person": [{"person_id": "p1"}],
        "fact_employment": [{"person_id": "p1"}, {"person_id": "p1"}],
        "fact_lookup": [
            {"row_number": 1, "status": "matched"},
            {"row_number": 2, "status": "invalid_input"},
        ],
    }


def test_check_tables_passes_consistent_tables() -> None:
    check_tables(_tables(), expected_lookup_rows=2)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda t: t["fact_lookup"].pop(), "fact_lookup has 1 rows for 2 input rows"),
        (lambda t: t["fact_lookup"][1].update(row_number=1), "row_number is not unique"),
        (lambda t: t["dim_person"].append({"person_id": "p1"}), "not unique within the batch"),
        (lambda t: t["dim_person"].append({"person_id": None}), "without person_id"),
        (lambda t: t["fact_employment"].append({"person_id": "ghost"}), "missing from dim_person"),
        (lambda t: t["fact_lookup"][0].update(status="not_found"), "only 0 lookups matched"),
    ],
)
def test_check_tables_rejects_inconsistencies(mutate: object, message: str) -> None:
    tables = _tables()
    mutate(tables)  # type: ignore[operator]
    with pytest.raises(OutputCheckError, match=message):
        check_tables(tables, expected_lookup_rows=2)


def test_opt_outs_do_not_count_as_a_layout_problem() -> None:
    config = GuardConfig(max_invalid_fraction=0.5, min_rows_for_fraction=5)
    reasons = ["consent: withheld"] * 7 + ["first_name: contains digits"]
    # Seven of ten rows opted out: a valid file with few people to enrich, not a wrong layout.
    assert file_problem(total=10, invalid_reasons=reasons, config=config) is None
    # Everyone opted out: still nothing to do, and the message says why.
    all_out = file_problem(total=3, invalid_reasons=["consent: withheld"] * 3, config=config)
    assert all_out is not None and "consent: withheld x3" in all_out
    # Layout problems are counted as before, with opt-outs left out of the fraction.
    layout = ["first_name: placeholder value 'test'"] * 6 + ["consent: withheld"] * 2
    problem = file_problem(total=10, invalid_reasons=layout, config=config)
    assert problem is not None and problem.startswith("6 of 10 rows rejected (60%)")
