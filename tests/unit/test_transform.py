from datetime import UTC, datetime

from enrich_pipeline.enricher import EnrichConfig, Enricher
from enrich_pipeline.models import InputRow, InvalidRow, LookupResult
from enrich_pipeline.providers.mock import MockProvider
from enrich_pipeline.schema import TABLES
from enrich_pipeline.transform import build_tables


def _clock() -> datetime:
    return datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def _results() -> list[LookupResult]:
    enricher = Enricher(MockProvider(), config=EnrichConfig(), sleep=lambda _: None, clock=_clock)
    rows = [
        InputRow(row_number=1, first_name="John", last_name="Doe"),
        InputRow(row_number=2, first_name="Alex", last_name="Lee"),
        InputRow(row_number=3, first_name="John", last_name="Doe"),
    ]
    return [enricher.lookup(r) for r in rows]


def test_rows_have_exactly_the_schema_columns() -> None:
    tables = build_tables(_results(), batch_id="b1")
    for name, rows in tables.items():
        columns = [c.name for c in TABLES[name]]
        assert rows, name
        for row in rows:
            assert list(row) == columns


def test_one_person_row_per_distinct_person() -> None:
    tables = build_tables(_results(), batch_id="b1")
    persons = tables["dim_person"]
    assert [p["person_id"] for p in persons] == ["pdl-mock-0001"]
    person = persons[0]
    assert person["location_name"] is None
    assert person["current_company_name"] == "northwind analytics"
    assert person["match_likelihood"] == 88
    assert person["lookup_method"] == "name_only"
    assert person["enriched_at"].tzinfo is None  # naive UTC for Parquet


def test_employment_rows_are_ordered_and_flagged() -> None:
    employment = build_tables(_results(), batch_id="b1")["fact_employment"]
    assert [
        (r["sequence_no"], r["company_name"], r["is_current"], r["start_year"]) for r in employment
    ] == [
        (0, "northwind analytics", True, 2022),
        (1, "contoso logistics", False, 2019),
        (2, "fabrikam retail", False, 2016),
    ]
    assert employment[0]["title_levels"] == ["senior"]
    assert employment[0]["company_location_country"] == "singapore"


def test_lookup_rows_cover_every_input_including_invalid_ones() -> None:
    invalid = [
        InvalidRow(
            row_number=4,
            reason="first_name: String should have at least 1 character",
            raw={"first_name": "", "last_name": "Nguyen"},
        )
    ]
    lookups = build_tables(
        _results(), batch_id="b1", invalid=invalid, provider="mock", at=_clock()
    )["fact_lookup"]
    assert [(r["row_number"], r["status"]) for r in lookups] == [
        (1, "matched"),
        (2, "ambiguous"),
        (3, "cached"),
        (4, "invalid_input"),
    ]
    assert lookups[2]["credits_consumed"] == 0
    assert lookups[3]["input_last_name"] == "Nguyen"
    assert lookups[3]["error_message"].startswith("first_name")
