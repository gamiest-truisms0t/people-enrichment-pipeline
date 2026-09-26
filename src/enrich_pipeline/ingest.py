"""CSV ingestion: header normalisation, per-row validation, and the file-level data guards.

Bad rows never abort a batch on their own; they are reported individually so the
operational table can show exactly which inputs were rejected and why. The file aborts
(`InputError`) when nothing sensible can be done with it: no header, missing required
columns, not text, too large, no data rows, every row rejected, or more rows rejected
than `GuardConfig.max_invalid_fraction` allows. Recoverable oddities (non-UTF-8 bytes,
a semicolon delimiter, extra columns) are accepted and listed in `ParsedInput.warnings`.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from enrich_pipeline.guards import (
    GuardConfig,
    InputError,
    decode_text,
    file_problem,
    sniff_delimiter,
)
from enrich_pipeline.models import InputRow, InvalidRow
from enrich_pipeline.normalize import normalize_text

__all__ = ["InputError", "ParsedInput", "normalize_header", "parse_csv"]

REQUIRED_COLUMNS: tuple[str, ...] = ("first_name", "last_name")
OPTIONAL_COLUMNS: tuple[str, ...] = ("email", "company", "location", "linkedin_url")

HEADER_ALIASES: dict[str, frozenset[str]] = {
    "first_name": frozenset({"first_name", "firstname", "first", "given_name", "given"}),
    "last_name": frozenset({"last_name", "lastname", "last", "surname", "family_name"}),
    "email": frozenset({"email", "e_mail", "email_address", "mail"}),
    "company": frozenset({"company", "company_name", "organisation", "organization", "employer"}),
    "location": frozenset({"location", "city", "country", "region"}),
    "linkedin_url": frozenset({"linkedin_url", "linkedin", "linkedin_profile", "profile_url"}),
}


@dataclass
class ParsedInput:
    rows: list[InputRow] = field(default_factory=list)
    invalid: list[InvalidRow] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    encoding: str = "text"
    delimiter: str = ","
    warnings: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.invalid)


def normalize_header(name: str) -> str:
    """'First Name' -> 'first_name', 'E-mail' -> 'email', unknown headers pass through."""
    key = normalize_text(name).replace("-", " ").replace(" ", "_")
    for canonical, aliases in HEADER_ALIASES.items():
        if key == canonical or key in aliases:
            return canonical
    return key


def _column_mapping(fieldnames: list[str]) -> tuple[dict[str, str], list[str]]:
    """Raw header -> canonical name, plus the raw headers that lost a duplicate claim."""
    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    duplicates: list[str] = []
    for raw in fieldnames:
        if raw is None:
            continue
        canonical = normalize_header(raw)
        if canonical in claimed:
            duplicates.append(raw)
            continue
        claimed.add(canonical)
        mapping[raw] = canonical
    return mapping, duplicates


def _describe(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) or "row"
        message = error["msg"].removeprefix("Value error, ")
        parts.append(f"{loc}: {message}")
    return "; ".join(parts)


def _read_source(
    source: str | Path | bytes | io.TextIOBase, config: GuardConfig
) -> tuple[str, str, list[str]]:
    """Source -> (text, encoding, warnings); applies the size guard to bytes and files."""
    if isinstance(source, io.TextIOBase):
        text = source.read()
        if len(text) > config.max_input_bytes:
            raise InputError(f"input is over the {config.max_input_bytes:,} byte limit")
        return text, "text", []
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if len(data) > config.max_input_bytes:
        raise InputError(
            f"input is {len(data):,} bytes, above the {config.max_input_bytes:,} byte limit;"
            " split the file"
        )
    return decode_text(data)


def parse_csv(
    source: str | Path | bytes | io.TextIOBase,
    *,
    max_rows: int | None = None,
    guards: GuardConfig | None = None,
) -> ParsedInput:
    """Parse a registrant CSV from a path, raw bytes or an open text stream."""
    config = guards or GuardConfig()
    text, encoding, warnings = _read_source(source, config)
    if "\x00" in text:
        raise InputError("input contains NUL bytes; this is not a text CSV file")

    text = text.lstrip("\r\n")  # a blank first line would otherwise read as an empty header
    header_line = next((line for line in text.splitlines() if line.strip()), "")
    delimiter = sniff_delimiter(header_line)
    if delimiter != ",":
        warnings.append(f"delimiter: {delimiter!r} detected instead of ','")

    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    if not reader.fieldnames:
        raise InputError("input file is empty or has no header row")

    mapping, duplicates = _column_mapping(list(reader.fieldnames))
    columns = list(mapping.values())
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise InputError(
            f"missing required column(s) {missing}; header was {list(reader.fieldnames)}"
        )
    wanted = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
    ignored = [raw for raw, canonical in mapping.items() if canonical not in wanted]
    if ignored:
        warnings.append(f"ignored_columns: {', '.join(ignored)}")
    if duplicates:
        warnings.append(f"duplicate_columns: {', '.join(duplicates)} (first occurrence kept)")

    parsed = ParsedInput(
        columns=[c for c in columns if c in wanted],
        ignored_columns=ignored,
        encoding=encoding,
        delimiter=delimiter,
        warnings=warnings,
    )
    for row_number, record in enumerate(reader, start=1):
        data = {
            mapping[raw]: (value or "").strip()
            for raw, value in record.items()
            if raw in mapping and mapping[raw] in wanted
        }
        if not any(data.values()):
            continue  # blank line
        if max_rows is not None and parsed.total >= max_rows:
            raise InputError(f"input has more than {max_rows} rows; split the file")
        try:
            parsed.rows.append(InputRow(row_number=row_number, **data))
        except ValidationError as exc:
            parsed.invalid.append(
                InvalidRow(row_number=row_number, reason=_describe(exc), raw=data)
            )

    problem = file_problem(
        total=parsed.total, invalid_reasons=[i.reason for i in parsed.invalid], config=config
    )
    if problem:
        raise InputError(problem)
    return parsed
