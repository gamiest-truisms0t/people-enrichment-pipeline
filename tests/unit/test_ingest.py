import io
from pathlib import Path

import pytest

from enrich_pipeline.ingest import InputError, normalize_header, parse_csv


def test_header_aliases() -> None:
    assert normalize_header("First Name") == "first_name"
    assert normalize_header("Surname") == "last_name"
    assert normalize_header("E-mail") == "email"
    assert normalize_header("Organisation") == "company"
    assert normalize_header("LinkedIn") == "linkedin_url"
    assert normalize_header("Ticket Type") == "ticket_type"


def test_parse_csv_with_the_brief_headers_and_a_blank_row() -> None:
    text = "First Name,Last Name\nJohn,Doe\nJane,Smith\n,\nAlex,Lee\n"
    parsed = parse_csv(io.StringIO(text))
    assert [r.row_number for r in parsed.rows] == [1, 2, 4]
    assert parsed.invalid == []
    assert parsed.columns == ["first_name", "last_name"]


def test_parse_csv_reports_invalid_rows_without_aborting() -> None:
    text = (
        "first_name,last_name,email\n,Nguyen,\nJohn,Doe,not-an-email\nJane,Smith,Jane@Example.com\n"
    )
    parsed = parse_csv(io.StringIO(text))
    assert len(parsed.rows) == 1
    assert parsed.rows[0].email == "jane@example.com"
    assert [i.row_number for i in parsed.invalid] == [1, 2]
    assert "first_name" in parsed.invalid[0].reason
    assert "email" in parsed.invalid[1].reason
    assert parsed.invalid[0].raw["last_name"] == "Nguyen"


def test_parse_csv_missing_required_header() -> None:
    with pytest.raises(InputError, match="missing required"):
        parse_csv(io.StringIO("name,email\nJohn Doe,x@y.z\n"))


def test_parse_csv_empty_file() -> None:
    with pytest.raises(InputError):
        parse_csv(io.StringIO(""))


def test_parse_csv_row_cap() -> None:
    text = "first_name,last_name\n" + "\n".join(f"a{i},b{i}" for i in range(5)) + "\n"
    with pytest.raises(InputError, match="more than 3"):
        parse_csv(io.StringIO(text), max_rows=3)


def test_parse_csv_from_path_handles_bom(tmp_path: Path) -> None:
    path = tmp_path / "names.csv"
    path.write_text("﻿first_name,last_name\nJohn,Doe\n", encoding="utf-8")
    parsed = parse_csv(path)
    assert parsed.rows[0].first_name == "John"


def test_duplicate_canonical_headers_keep_the_first() -> None:
    text = "first_name,last_name,city,country\nJohn,Doe,Lisbon,Portugal\n"
    parsed = parse_csv(io.StringIO(text))
    assert parsed.rows[0].location == "Lisbon"
