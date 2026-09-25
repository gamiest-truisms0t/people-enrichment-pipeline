"""Text normalisation and the idempotency lookup key.

Two names that differ only in case, accents encoding (composed vs decomposed),
surrounding or repeated whitespace, or stray punctuation must map to the same
key so a person is never paid for twice.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

_WHITESPACE = re.compile(r"\s+")
# Drop everything except word characters, whitespace, apostrophes and hyphens.
_PUNCTUATION = re.compile(r"[^\w\s'\-]", re.UNICODE)
_URL_PREFIX = re.compile(r"^(https?://)?(www\.)?", re.IGNORECASE)


def normalize_text(value: str | None) -> str:
    """NFKC, casefold, strip punctuation, collapse whitespace. Empty string for None."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).casefold()
    text = _PUNCTUATION.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def normalize_email(value: str | None) -> str:
    if not value:
        return ""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def normalize_url(value: str | None) -> str:
    """Canonical form for profile URLs: no scheme, no www, no trailing slash, lower case."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    text = _URL_PREFIX.sub("", text)
    return text.rstrip("/")


def lookup_key(
    first_name: str,
    last_name: str,
    *,
    email: str | None = None,
    company: str | None = None,
    location: str | None = None,
    linkedin_url: str | None = None,
    extra: str | None = None,
) -> str:
    """Stable SHA-256 over the identifiers that will actually be sent to the provider.

    `extra` carries any request parameter that changes the provider's answer (for example
    the enrichment likelihood threshold), so that tuning it re-queries instead of serving
    stale cached outcomes.
    """
    canonical = "|".join(
        (
            normalize_text(first_name),
            normalize_text(last_name),
            normalize_email(email),
            normalize_text(company),
            normalize_text(location),
            normalize_url(linkedin_url),
            normalize_text(extra),
        )
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
