import io
from pathlib import Path

import pytest

from enrich_pipeline.guards import GuardConfig
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
    # A bad email is dropped with a note; the row still goes through by name.
    assert [r.row_number for r in parsed.rows] == [2, 3]
    assert parsed.rows[0].email is None
    assert parsed.rows[0].notes == ["input.email_invalid"]
    assert parsed.rows[1].email == "jane@example.com"
    assert parsed.rows[1].notes == []
    assert [i.row_number for i in parsed.invalid] == [1]
    assert parsed.invalid[0].reason == "first_name: String should have at least 1 character"
    assert parsed.invalid[0].raw["last_name"] == "Nguyen"


def test_parse_csv_bytes_with_semicolons_and_windows_1252() -> None:
    data = "first_name;last_name;company\nJosé;García;Acme\n".encode("cp1252")
    parsed = parse_csv(data)
    assert (parsed.delimiter, parsed.encoding) == (";", "cp1252")
    assert parsed.rows[0].first_name == "José"
    assert parsed.rows[0].company == "Acme"
    assert [w.split(":")[0] for w in parsed.warnings] == ["encoding", "delimiter"]


def test_parse_csv_aborts_when_most_rows_are_junk() -> None:
    header = "first_name,last_name\n"
    mostly_junk = header + "\n".join(["test,test"] * 6 + ["John,Doe"] * 4) + "\n"
    with pytest.raises(InputError, match=r"6 of 10 rows rejected \(60%\)"):
        parse_csv(io.StringIO(mostly_junk))
    # Under the threshold the file proceeds and keeps the rejected rows for fact_lookup.
    some_junk = header + "\n".join(["test,test"] * 4 + ["John,Doe"] * 6) + "\n"
    parsed = parse_csv(io.StringIO(some_junk))
    assert (len(parsed.rows), len(parsed.invalid)) == (6, 4)
    # A stricter deployment can lower the threshold.
    with pytest.raises(InputError, match=r"4 of 10 rows rejected \(40%\)"):
        parse_csv(io.StringIO(some_junk), guards=GuardConfig(max_invalid_fraction=0.3))


def test_parse_csv_tolerates_leading_blank_lines() -> None:
    parsed = parse_csv(io.StringIO("\n\nfirst_name,last_name\nJohn,Doe\n"))
    assert [r.first_name for r in parsed.rows] == ["John"]
    assert parsed.warnings == []


def test_parse_csv_aborts_with_no_usable_rows() -> None:
    with pytest.raises(InputError, match="header but no data rows"):
        parse_csv(io.StringIO("first_name,last_name\n"))
    with pytest.raises(InputError, match="no valid rows: all 2 data rows were rejected"):
        parse_csv(io.StringIO("first_name,last_name\ntest,test\nR2,D2\n"))


def test_parse_csv_rejects_repeated_headers_and_emails_in_the_name_column() -> None:
    text = "first_name,last_name\nJohn,Doe\nfirst_name,last_name\njohn@example.com,Doe\n"
    parsed = parse_csv(io.StringIO(text))
    assert [r.row_number for r in parsed.rows] == [1]
    assert [i.reason for i in parsed.invalid] == [
        "first_name: placeholder value 'first_name'; last_name: placeholder value 'last_name'",
        "first_name: looks like an email address (columns swapped?)",
    ]


def test_parse_csv_size_and_binary_guards() -> None:
    with pytest.raises(InputError, match="above the 10 byte limit"):
        parse_csv(b"first_name,last_name\nJohn,Doe\n", guards=GuardConfig(max_input_bytes=10))
    with pytest.raises(InputError, match="NUL bytes"):
        parse_csv("first_name,last_name\nJohn,Doe\n".encode("utf-16-le"))  # no BOM: not text


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
    text = "first_name,last_name,city,country,ticket_type\nJohn,Doe,Lisbon,Portugal,VIP\n"
    parsed = parse_csv(io.StringIO(text))
    assert parsed.rows[0].location == "Lisbon"
    assert parsed.ignored_columns == ["ticket_type"]
    assert parsed.warnings == [
        "ignored_columns: ticket_type",
        "duplicate_columns: country (first occurrence kept)",
    ]
