"""Property-based tests for the normalisation and guard functions.

The example tests pin behaviour on inputs we thought of. These state the invariants and
let Hypothesis look for inputs we did not: any Unicode text, any byte string, any mix of
optional fields. They run deterministically in CI (see tests/conftest.py).
"""

from __future__ import annotations

import contextlib
import io
import re
import unicodedata

from hypothesis import assume, given
from hypothesis import strategies as st

from enrich_pipeline.guards import (
    EMAIL_MAX_LENGTH,
    FIELD_MAX_LENGTH,
    NAME_MAX_LENGTH,
    InputError,
    clean_input_data,
    consent_problem,
    fold_text,
    name_problem,
    strip_invisible,
)
from enrich_pipeline.ingest import parse_csv
from enrich_pipeline.normalize import lookup_key, normalize_email, normalize_text, normalize_url

text = st.text(max_size=60)
name = st.text(min_size=1, max_size=30)
padding = st.text(alphabet=" \t\r\n", max_size=4)
optional = st.one_of(st.none(), st.text(max_size=80))
ascii_word = st.text(
    alphabet=st.characters(categories=("Ll", "Nd"), max_codepoint=0x7F), min_size=1, max_size=30
)


def _category(ch: str) -> str:
    return unicodedata.category(ch)


# ------------------------------------------------------------------ normalisation


@given(text)
def test_normalize_text_is_idempotent_and_tidy(value: str) -> None:
    once = normalize_text(value)
    assert normalize_text(once) == once
    assert once == once.strip()
    assert "  " not in once
    assert once == once.casefold()
    assert re.search(r"[^\w\s'\-]", once) is None


@given(text)
def test_normalize_text_does_not_depend_on_the_unicode_form(value: str) -> None:
    forms = ("NFC", "NFD", "NFKC", "NFKD")
    assert len({normalize_text(unicodedata.normalize(form, value)) for form in forms}) == 1


@given(text, padding, padding)
def test_normalize_text_ignores_surrounding_whitespace(value: str, before: str, after: str) -> None:
    assert normalize_text(before + value + after) == normalize_text(value)


@given(text)
def test_normalize_email_is_idempotent(value: str) -> None:
    once = normalize_email(value)
    assert normalize_email(once) == once


@given(text)
def test_normalize_url_has_no_trailing_slash_and_is_case_folded(value: str) -> None:
    canonical = normalize_url(value)
    assert not canonical.endswith("/")
    assert canonical == canonical.casefold()


@given(ascii_word)
def test_normalize_url_treats_scheme_and_www_as_noise(host: str) -> None:
    expected = normalize_url(host)
    for prefix in ("", "http://", "https://", "www.", "HTTPS://WWW.", "https://www."):
        assert normalize_url(prefix + host + "/") == expected


@given(name, name, padding, padding)
def test_lookup_key_is_hex_and_ignores_surrounding_whitespace(
    first: str, last: str, before: str, after: str
) -> None:
    key = lookup_key(before + first + after, last)
    assert re.fullmatch(r"[0-9a-f]{64}", key)
    assert key == lookup_key(first, before + last + after)


@given(name, name, name)
def test_lookup_key_changes_when_an_identifier_is_added(
    first: str, last: str, company: str
) -> None:
    assume(normalize_text(company) != "")
    assert lookup_key(first, last, company=company) != lookup_key(first, last)


# ------------------------------------------------------------------------ guards


@given(text)
def test_strip_invisible_removes_control_and_format_characters(value: str) -> None:
    cleaned = strip_invisible(value)
    assert all(_category(ch) not in ("Cc", "Cf") for ch in cleaned)
    assert strip_invisible(cleaned) == cleaned


@given(text)
def test_name_problem_only_accepts_plausible_names(value: str) -> None:
    problem = name_problem(value)
    has_digit = any(_category(ch) == "Nd" for ch in value)
    has_letter = any(_category(ch).startswith("L") for ch in value)
    structurally_fine = (
        len(value) <= NAME_MAX_LENGTH and "@" not in value and not has_digit and has_letter
    )
    if problem is None:
        assert structurally_fine
    else:
        assert not structurally_fine or problem.startswith("placeholder value")


@given(text)
def test_fold_text_has_no_combining_marks_and_ignores_the_unicode_form(value: str) -> None:
    folded = fold_text(value)
    assert all(_category(ch) != "Mn" for ch in folded)
    assert fold_text(unicodedata.normalize("NFD", value)) == folded


@given(
    first=text,
    last=text,
    email=optional,
    company=optional,
    location=optional,
    linkedin=optional,
    consent=optional,
)
def test_clean_input_data_salvages_optional_fields_without_raising(
    first: str,
    last: str,
    email: str | None,
    company: str | None,
    location: str | None,
    linkedin: str | None,
    consent: str | None,
) -> None:
    row = clean_input_data(
        {
            "first_name": first,
            "last_name": last,
            "email": email,
            "company": company,
            "location": location,
            "linkedin_url": linkedin,
            "consent": consent,
        }
    )
    assert row["email"] is None or ("@" in row["email"] and len(row["email"]) <= EMAIL_MAX_LENGTH)
    assert row["linkedin_url"] is None or row["linkedin_url"].startswith("linkedin.com/")
    assert row["consent"] in (True, False, None)
    assert len(row["notes"]) == len(set(row["notes"]))
    assert all(note.startswith("input.") for note in row["notes"])
    for key in ("company", "location"):
        value = row[key]
        assert value is None or (value == value.strip() and len(value) <= FIELD_MAX_LENGTH)


@given(st.one_of(st.none(), st.booleans()), st.booleans())
def test_consent_problem_is_total_and_only_clears_a_yes_or_an_optional_blank(
    consent: bool | None, require: bool
) -> None:
    problem = consent_problem(consent, require_consent=require)
    assert (problem is None) == (consent is True or (consent is None and not require))


# --------------------------------------------------------------------- ingestion


@given(st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=240))
def test_parse_csv_text_either_parses_or_rejects_with_input_error(body: str) -> None:
    try:
        parsed = parse_csv(io.StringIO("first_name,last_name,email,company\n" + body))
    except InputError:
        return
    assert parsed.columns[:2] == ["first_name", "last_name"]
    for row in parsed.rows:
        assert name_problem(row.first_name) is None
        assert name_problem(row.last_name) is None


@given(st.binary(max_size=240))
def test_parse_csv_bytes_either_parse_or_reject_with_input_error(data: bytes) -> None:
    # Any other exception type escaping here is a crash the validate function would
    # surface as a traceback instead of a quarantined file with a reason.
    with contextlib.suppress(InputError):
        parse_csv(b"first_name,last_name\n" + data)
