"""CSV ingestion: header normalisation, per-row validation, and an optional row cap.

Bad rows never abort a batch; they are reported individually so the operational
table can show exactly which inputs were rejected and why. A bad header or an
empty file does abort, because nothing sensible can be done with it.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from enrich_pipeline.models import InputRow, InvalidRow
from enrich_pipeline.normalize import normalize_text

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


class InputError(ValueError):
    """The file as a whole cannot be processed (missing header, empty, too big)."""


@dataclass
class ParsedInput:
    rows: list[InputRow] = field(default_factory=list)
    invalid: list[InvalidRow] = field(default_factory=list)
    columns: list[str] = field(default_factory=list)

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


def _column_mapping(fieldnames: list[str]) -> dict[str, str]:
    """Raw header -> canonical name. The first header claiming a canonical name wins."""
    mapping: dict[str, str] = {}
    claimed: set[str] = set()
    for raw in fieldnames:
        if raw is None:
            continue
        canonical = normalize_header(raw)
        if canonical in claimed:
            continue
        claimed.add(canonical)
        mapping[raw] = canonical
    return mapping


def _describe(exc: ValidationError) -> str:
    parts = []
    for error in exc.errors():
        loc = ".".join(str(p) for p in error["loc"]) or "row"
        parts.append(f"{loc}: {error['msg']}")
    return "; ".join(parts)


def parse_csv(source: str | Path | io.TextIOBase, *, max_rows: int | None = None) -> ParsedInput:
    """Parse a registrant CSV. `source` is a path or an open text stream."""
    if isinstance(source, io.TextIOBase):
        text = source.read()
    else:
        text = Path(source).read_text(encoding="utf-8-sig")

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise InputError("input file is empty or has no header row")

    mapping = _column_mapping(list(reader.fieldnames))
    columns = list(mapping.values())
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        raise InputError(
            f"missing required column(s) {missing}; header was {list(reader.fieldnames)}"
        )

    parsed = ParsedInput(columns=columns)
    wanted = set(REQUIRED_COLUMNS) | set(OPTIONAL_COLUMNS)
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
    return parsed
