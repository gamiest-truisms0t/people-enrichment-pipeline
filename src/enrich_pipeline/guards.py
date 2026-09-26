"""Data guards: what happens when the data does not look like the ETL expects.

Three layers, each with a different response:

* **Input file** (``decode_text``, ``sniff_delimiter``, ``file_problem``): a file that cannot
  be processed as a whole aborts the batch with a clear ``InputError`` (wrong or missing
  header, not text, too large, header but no rows, every row rejected, or more rows
  rejected than ``GuardConfig.max_invalid_fraction`` allows, which almost always means the
  columns are not what the header says). Recoverable oddities (a UTF-16 or Windows-1252
  export, a semicolon delimiter, extra columns) are accepted and recorded as warnings.
* **Input row** (``clean_input_data``, ``name_problem``): required fields that fail a rule
  make the row ``invalid_input`` with a reason; optional fields that fail are dropped and
  the row continues with a note (``input.email_invalid`` and friends), because a bad email
  is no reason to skip a person who can still be found by name and company.
* **Provider response and curated output** (``match_flags``, ``batch_quality``,
  ``check_tables``): a matched profile that looks doubtful is kept but flagged
  (``match.*``); the batch as a whole gets warnings (low match rate, many rejected rows,
  flagged matches, decoding fallbacks) that travel into the manifest and the completion
  notification; structural checks on the curated tables fail the step rather than write
  inconsistent data.

Flags and notes are plain strings with a ``layer.rule`` shape so they can be counted and
filtered in Athena.
"""

from __future__ import annotations

import codecs
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from enrich_pipeline.normalize import normalize_text, normalize_url

NAME_MAX_LENGTH = 100
FIELD_MAX_LENGTH = 200
EMAIL_MAX_LENGTH = 254

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")
# After normalize_url(): optional regional subdomain (sg., uk., ...), then /in/ or /pub/.
_LINKEDIN = re.compile(r"^(?:[a-z]{2,3}\.)?(linkedin\.com/(?:in|pub)/[^/?#\s]+)")
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")
_DELIMITERS = (",", ";", "\t", "|")

# Values people type when they do not want to give a name. Compared after normalize_text.
NAME_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "test",
        "testing",
        "tester",
        "test test",
        "n a",
        "na",
        "none",
        "null",
        "nil",
        "unknown",
        "anonymous",
        "anon",
        "asdf",
        "qwerty",
        "xxx",
        "tbd",
        "tba",
        "name",
        "first name",
        "first_name",
        "firstname",
        "last name",
        "last_name",
        "lastname",
        "surname",
    }
)
# Deliberately absent: words that are also real surnames (Guest, Bar, Sample, Demo, User).

# Not an employer or a place; sending these to the provider can only hurt the match.
ORG_PLACEHOLDERS: frozenset[str] = frozenset(
    {
        "n a",
        "na",
        "none",
        "null",
        "nil",
        "unknown",
        "test",
        "tbd",
        "tba",
        "not applicable",
        "no",
        "no company",
        "self",
        "self employed",
        "selfemployed",
        "unemployed",
        "student",
        "retired",
        "freelance",
        "freelancer",
        "independent",
        "individual",
        "personal",
        "private",
        "other",
    }
)


# Spellings of consent answers, compared after placeholder_key(); anything else is
# unrecognised and recorded as a note.
CONSENT_YES: frozenset[str] = frozenset(
    {
        "yes",
        "y",
        "true",
        "1",
        "opt in",
        "optin",
        "opted in",
        "agreed",
        "agree",
        "granted",
        "consented",
        "ok",
    }
)
CONSENT_NO: frozenset[str] = frozenset(
    {
        "no",
        "n",
        "false",
        "0",
        "opt out",
        "optout",
        "opted out",
        "declined",
        "decline",
        "withheld",
        "refused",
        "none",
    }
)


class InputError(ValueError):
    """The file as a whole cannot be processed (bad header, not text, too big, all junk)."""


class OutputCheckError(RuntimeError):
    """The curated tables are internally inconsistent; nothing was published."""


@dataclass(frozen=True)
class GuardConfig:
    """Batch-level thresholds. Field rules (lengths, placeholders) are fixed constants."""

    max_input_bytes: int = 5_000_000
    # Above this share of rejected rows the file is treated as the wrong layout.
    max_invalid_fraction: float = 0.5
    min_rows_for_fraction: int = 5
    # Below this share of matched (or cached) valid rows the batch gets a warning.
    min_match_rate: float = 0.2
    min_rows_for_match_rate: int = 5
    # Above this share of rejected rows (but below the abort threshold) the batch warns.
    warn_invalid_fraction: float = 0.2
    # Reject rows whose consent is not recorded as given (a consent column is optional;
    # an explicit "no" is always rejected).
    require_consent: bool = False


# --------------------------------------------------------------------------- file layer


def decode_text(data: bytes) -> tuple[str, str, list[str]]:
    """Bytes -> text. Returns (text, encoding used, warnings).

    UTF-8 (with or without BOM) and UTF-16 with a BOM decode cleanly. Anything else is read
    as Windows-1252, the usual source of accented names that are not UTF-8, with a warning
    so the operator can re-export the file properly.
    """
    if data.startswith(codecs.BOM_UTF8):
        return data.decode("utf-8-sig"), "utf-8-sig", []
    if data.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return data.decode("utf-16"), "utf-16", ["encoding: UTF-16 file; decoded from its BOM"]
    try:
        return data.decode("utf-8"), "utf-8", []
    except UnicodeDecodeError as exc:
        text = data.decode("cp1252", errors="replace")
        return (
            text,
            "cp1252",
            [
                f"encoding: not valid UTF-8 (byte 0x{data[exc.start]:02x} at offset {exc.start});"
                " decoded as Windows-1252, check accented names"
            ],
        )


def sniff_delimiter(header_line: str) -> str:
    """The most frequent of , ; tab | in the header line; comma on a tie or when absent."""
    counts = {d: header_line.count(d) for d in _DELIMITERS}
    best = max(counts.values())
    if best == 0:
        return ","
    return next(d for d in _DELIMITERS if counts[d] == best)


def file_problem(*, total: int, invalid_reasons: Sequence[str], config: GuardConfig) -> str | None:
    """Why a parsed file should abort the batch, or None. Called after all rows are read."""
    invalid = len(invalid_reasons)
    if total == 0:
        return "input has a header but no data rows"
    if invalid == total:
        return (
            f"no valid rows: all {total} data rows were rejected ({top_reasons(invalid_reasons)})"
        )
    # Opt-outs are legitimate rejections, not a sign of a wrong layout, so they do not
    # count towards the "the columns are not what the header says" rule.
    layout_reasons = [r for r in invalid_reasons if not r.startswith("consent:")]
    fraction = len(layout_reasons) / total
    if total >= config.min_rows_for_fraction and fraction > config.max_invalid_fraction:
        return (
            f"{len(layout_reasons)} of {total} rows rejected ({fraction:.0%}), above the "
            f"{config.max_invalid_fraction:.0%} limit; the columns probably do not hold what "
            f"the header says ({top_reasons(layout_reasons)})"
        )
    return None


def top_reasons(reasons: Iterable[str], limit: int = 3) -> str:
    """'first_name: placeholder value x3; email: ... x1' for error messages and warnings."""
    counts = Counter(_reason_family(r) for r in reasons)
    return "; ".join(f"{reason} x{n}" for reason, n in counts.most_common(limit)) or "none"


def _reason_family(reason: str) -> str:
    """Collapse 'first_name: placeholder value 'test'' into 'first_name: placeholder value'."""
    head = reason.split(";")[0]
    return re.sub(r"\s*'[^']*'\s*$", "", head).strip()


# --------------------------------------------------------------------------- row layer


def strip_invisible(value: str) -> str:
    """Remove control and format characters (zero-width joiners, stray BOMs, NULs)."""
    cleaned = []
    for ch in value:
        category = unicodedata.category(ch)
        if category == "Cc":
            cleaned.append(" " if ch in "\t\n\r" else "")
        elif category == "Cf":
            continue
        else:
            cleaned.append(ch)
    return "".join(cleaned)


def name_problem(value: str) -> str | None:
    """Why a required name field is unusable, or None if it passes every rule."""
    if len(value) > NAME_MAX_LENGTH:
        return f"longer than {NAME_MAX_LENGTH} characters"
    if "@" in value:
        return "looks like an email address (columns swapped?)"
    if any(unicodedata.category(ch) == "Nd" for ch in value):
        return "contains digits"
    if not any(unicodedata.category(ch).startswith("L") for ch in value):
        return "contains no letters"
    if placeholder_key(value) in NAME_PLACEHOLDERS:
        return f"placeholder value {value!r}"
    return None


def clean_input_data(data: Mapping[str, Any]) -> dict[str, Any]:
    """Tidy one raw input row before validation and salvage the optional fields.

    Strings lose invisible characters and surrounding whitespace; blank optionals become
    None. An optional field that fails its rule is dropped with a note rather than
    rejecting the row. Required names are only tidied here: ``name_problem`` decides
    whether they reject the row, so the reason lands on the right field.
    """
    row = dict(data)
    notes: list[str] = list(row.get("notes") or [])

    for key, value in list(row.items()):
        if isinstance(value, str):
            row[key] = strip_invisible(value).strip()

    for key in ("email", "company", "location", "linkedin_url", "consent"):
        if key in row and (row[key] is None or row[key] == ""):
            row[key] = None

    email = row.get("email")
    if isinstance(email, str) and (len(email) > EMAIL_MAX_LENGTH or not _EMAIL.match(email)):
        row["email"] = None
        notes.append("input.email_invalid")

    url = row.get("linkedin_url")
    if isinstance(url, str):
        # normalize_url drops scheme, www and case; also drop tracking parameters and
        # fragments so the same profile always yields the same cache key.
        canonical = re.split(r"[?#]", normalize_url(url), maxsplit=1)[0].rstrip("/")
        if match := _LINKEDIN.match(canonical):
            row["linkedin_url"] = match.group(1)  # regional subdomain dropped
        else:
            row["linkedin_url"] = None
            notes.append("input.linkedin_url_invalid")

    for key in ("company", "location"):
        value = row.get(key)
        if not isinstance(value, str):
            continue
        if len(value) > FIELD_MAX_LENGTH:
            row[key] = None
            notes.append(f"input.{key}_too_long")
        elif placeholder_key(value) in ORG_PLACEHOLDERS:
            row[key] = None
            notes.append(f"input.{key}_placeholder")

    consent = row.get("consent")
    if isinstance(consent, str):
        answer = placeholder_key(consent)
        if answer in CONSENT_YES:
            row["consent"] = True
        elif answer in CONSENT_NO:
            row["consent"] = False
        else:
            row["consent"] = None
            notes.append("input.consent_unrecognised")

    row["notes"] = list(dict.fromkeys(notes))
    return row


def consent_problem(consent: bool | None, *, require_consent: bool) -> str | None:
    """Why a row may not be enriched on consent grounds, or None."""
    if consent is False:
        return "consent: withheld"
    if consent is None and require_consent:
        return "consent: not recorded (require_consent is on)"
    return None


# --------------------------------------------------------------------------- match layer


def fold_text(value: str | None) -> str:
    """normalize_text plus accent stripping, so 'García' and 'garcia' compare equal."""
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return normalize_text(without_marks)


def placeholder_key(value: str) -> str:
    """Comparison form for the placeholder sets: folded, hyphens and apostrophes removed."""
    return " ".join(fold_text(value).replace("-", " ").replace("'", "").split())


def _name_tokens(*values: str | None) -> set[str]:
    return {token for value in values if value for token in fold_text(value).split()}


def _valid_date(value: str) -> bool:
    """YYYY, YYYY-MM or YYYY-MM-DD with a plausible year, month and day."""
    match = _DATE.match(value)
    if not match:
        return False
    parts = [int(p) for p in value.split("-")]
    year = parts[0]
    month = parts[1] if len(parts) > 1 else 1
    day = parts[2] if len(parts) > 2 else 1
    return 1900 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31


def match_flags(
    *,
    input_first_name: str,
    input_last_name: str,
    profile: Any,
    likelihood: float | None,
    likelihood_floor: float | None,
) -> list[str]:
    """Doubts about a matched profile. The match is kept; analysts can filter on these.

    `profile` is a PersonProfile (typed loosely to keep this module free of model imports).
    """
    flags: list[str] = []

    input_tokens = _name_tokens(input_last_name)
    profile_tokens = _name_tokens(profile.full_name, profile.first_name, profile.last_name)
    # No profile name at all is not a mismatch; sparse_profile covers that case below.
    if input_tokens and profile_tokens and not (input_tokens & profile_tokens):
        flags.append("match.name_mismatch")

    if profile.full_name is None or (
        profile.job_title is None and profile.job_company_name is None
    ):
        flags.append("match.sparse_profile")
    if not profile.experience:
        flags.append("match.no_employment")
    if likelihood is not None and likelihood_floor is not None and likelihood <= likelihood_floor:
        flags.append("match.likelihood_at_floor")

    malformed = False
    reversed_dates = False
    for exp in profile.experience:
        dates = [d for d in (exp.start_date, exp.end_date) if d is not None]
        if not all(_valid_date(d) for d in dates):
            malformed = True
        elif exp.start_date and exp.end_date and exp.end_date < exp.start_date:
            reversed_dates = True  # ISO prefixes compare correctly as strings
    if malformed:
        flags.append("match.malformed_dates")
    if reversed_dates:
        flags.append("match.end_before_start")
    return flags


# --------------------------------------------------------------------------- batch layer

MATCHED_STATUSES = frozenset({"matched", "cached"})


@dataclass
class QualityReport:
    rows_valid: int
    rows_invalid: int
    match_rate: float | None
    flagged_matches: int
    flag_counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def warning_count(self) -> int:
        return len(self.warnings)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_valid": self.rows_valid,
            "rows_invalid": self.rows_invalid,
            "match_rate": self.match_rate,
            "flagged_matches": self.flagged_matches,
            "flag_counts": dict(sorted(self.flag_counts.items())),
            "warnings": list(self.warnings),
            "warning_count": self.warning_count,
            "notes": "; ".join(self.warnings) or "none",
        }


def batch_quality(
    *,
    statuses: Sequence[str],
    flags_per_result: Sequence[Sequence[str]],
    rows_invalid: int,
    parse_warnings: Iterable[str] = (),
    unrecorded_rows: int = 0,
    config: GuardConfig,
) -> QualityReport:
    """Batch-level warnings from per-row outcomes. Never raises: these are advisory."""
    rows_valid = len(statuses)
    total = rows_valid + rows_invalid
    matched = sum(1 for s in statuses if s in MATCHED_STATUSES)
    match_rate = (matched / rows_valid) if rows_valid else None

    # Every note and flag is counted; only match.* doubts on a matched row make the batch
    # warn, because input.* notes describe fields that were salvaged, not a doubtful match.
    flag_counts: Counter[str] = Counter()
    flagged_matches = 0
    for status, flags in zip(statuses, flags_per_result, strict=True):
        if flags:
            flag_counts.update(flags)
            if status in MATCHED_STATUSES and any(f.startswith("match.") for f in flags):
                flagged_matches += 1

    warnings = list(parse_warnings)
    enough_rows = rows_valid >= config.min_rows_for_match_rate
    if enough_rows and match_rate is not None and match_rate < config.min_match_rate:
        warnings.append(
            f"low_match_rate: {matched} of {rows_valid} valid rows matched "
            f"({match_rate:.0%}), below {config.min_match_rate:.0%}; check the input "
            "columns and the provider's credit state"
        )
    if total and rows_invalid / total >= config.warn_invalid_fraction:
        warnings.append(
            f"high_invalid_rate: {rows_invalid} of {total} rows rejected "
            f"({rows_invalid / total:.0%}); see fact_lookup rows with status invalid_input"
        )
    if flagged_matches:
        doubts = Counter({f: n for f, n in flag_counts.items() if f.startswith("match.")})
        top = ", ".join(f"{flag} x{n}" for flag, n in doubts.most_common(3))
        warnings.append(
            f"flagged_matches: {flagged_matches} matched rows carry quality flags ({top})"
        )
    if unrecorded_rows:
        warnings.append(
            f"unrecorded_rows: {unrecorded_rows} rows produced no result and were recorded "
            "as errors"
        )
    unavailable = sum(1 for s in statuses if s == "provider_unavailable")
    if unavailable:
        warnings.append(
            f"provider_unavailable: {unavailable} rows were skipped while the provider circuit "
            "breaker was open; re-upload or redrive the file once the provider recovers"
        )
    return QualityReport(
        rows_valid=rows_valid,
        rows_invalid=rows_invalid,
        match_rate=match_rate,
        flagged_matches=flagged_matches,
        flag_counts=dict(flag_counts),
        warnings=warnings,
    )


def check_tables(
    tables: Mapping[str, Sequence[Mapping[str, Any]]], *, expected_lookup_rows: int
) -> None:
    """Structural checks on the curated tables; raises OutputCheckError on the first failure."""
    lookups = tables.get("fact_lookup", [])
    persons = tables.get("dim_person", [])
    employment = tables.get("fact_employment", [])

    if len(lookups) != expected_lookup_rows:
        raise OutputCheckError(
            f"fact_lookup has {len(lookups)} rows for {expected_lookup_rows} input rows"
        )
    row_numbers = [r.get("row_number") for r in lookups]
    if len(set(row_numbers)) != len(row_numbers) or None in row_numbers:
        raise OutputCheckError("fact_lookup row_number is not unique and non-null")

    person_ids = [p.get("person_id") for p in persons]
    if None in person_ids or "" in person_ids:
        raise OutputCheckError("dim_person has a row without person_id")
    if len(set(person_ids)) != len(person_ids):
        raise OutputCheckError("dim_person person_id is not unique within the batch")

    orphans = {e.get("person_id") for e in employment} - set(person_ids)
    if orphans:
        raise OutputCheckError(
            f"fact_employment references {len(orphans)} person_id(s) missing from dim_person"
        )

    matched_lookups = sum(1 for r in lookups if r.get("status") in MATCHED_STATUSES)
    if len(persons) > matched_lookups:
        raise OutputCheckError(
            f"dim_person has {len(persons)} rows but only {matched_lookups} lookups matched"
        )
