from enrich_pipeline.normalize import lookup_key, normalize_email, normalize_text, normalize_url


def test_normalize_text_folds_case_whitespace_and_punctuation() -> None:
    assert normalize_text("  JOSÉ   García-López ") == "josé garcía-lópez"
    assert normalize_text("O'Brien, Jr.") == "o'brien jr"


def test_normalize_text_unifies_unicode_forms() -> None:
    composed = "José"
    decomposed = "José"
    assert normalize_text(composed) == normalize_text(decomposed)


def test_normalize_text_none_is_empty() -> None:
    assert normalize_text(None) == ""
    assert normalize_text("") == ""


def test_normalize_email() -> None:
    assert normalize_email("  Jane.Smith@Example.COM ") == "jane.smith@example.com"
    assert normalize_email(None) == ""


def test_normalize_url() -> None:
    assert normalize_url("https://www.LinkedIn.com/in/john-doe/") == "linkedin.com/in/john-doe"
    assert normalize_url("linkedin.com/in/john-doe") == "linkedin.com/in/john-doe"


def test_lookup_key_is_stable_and_insensitive_to_case_and_spacing() -> None:
    a = lookup_key("John", "Doe")
    b = lookup_key(" john ", "DOE")
    assert a == b
    assert len(a) == 64


def test_lookup_key_changes_with_identifiers() -> None:
    base = lookup_key("John", "Doe")
    assert base != lookup_key("John", "Doe", company="Acme")
    assert base != lookup_key("John", "Doe", email="john@example.com")
    assert base != lookup_key("John", "Doe", location="Singapore")
    assert base != lookup_key("John", "Doe", linkedin_url="linkedin.com/in/john-doe")
