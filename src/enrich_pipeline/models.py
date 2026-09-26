"""Domain models: input rows, provider profiles, and lookup outcomes.

Profile models mirror the People Data Labs person schema closely enough that a
raw response parses directly, while keeping provider-neutral names so a second
provider can be mapped onto the same shapes. Contact fields are deliberately
absent: the pipeline never stores them.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from enrich_pipeline.guards import clean_input_data, name_problem


class LookupStatus(StrEnum):
    MATCHED = "matched"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"
    CACHED = "cached"
    BUDGET_DEFERRED = "budget_deferred"
    INVALID_INPUT = "invalid_input"
    ERROR = "error"


class LookupMethod(StrEnum):
    EMAIL = "email"
    LINKEDIN = "linkedin"
    NAME_CONTEXT = "name_context"
    NAME_ONLY = "name_only"


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _obscured_to_none(value: Any) -> Any:
    """PDL's free plan replaces obscured field values with true/false."""
    return None if isinstance(value, bool) else value


# --------------------------------------------------------------------------- input


class InputRow(BaseModel):
    """One registrant from the input CSV, after header normalisation and the data guards.

    `guards.clean_input_data` runs first: it tidies every string and drops optional fields
    that fail their rule, recording a note per drop in `notes`. The name validators then
    decide whether the row is usable at all.
    """

    model_config = ConfigDict(str_strip_whitespace=True, extra="ignore")

    row_number: int = Field(ge=1, description="1-based position in the input file")
    first_name: str = Field(min_length=1)
    last_name: str = Field(min_length=1)
    email: str | None = None
    company: str | None = None
    location: str | None = None
    linkedin_url: str | None = None
    notes: list[str] = Field(
        default_factory=list, description="input.* notes: optional fields dropped by a guard"
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_guards(cls, data: Any) -> Any:
        return clean_input_data(data) if isinstance(data, dict) else data

    @field_validator("email", "company", "location", "linkedin_url", mode="before")
    @classmethod
    def _blank_optional(cls, value: Any) -> Any:
        return _blank_to_none(value)

    @field_validator("email")
    @classmethod
    def _casefold_email(cls, value: str | None) -> str | None:
        return value.casefold() if value is not None else None

    @field_validator("first_name", "last_name")
    @classmethod
    def _name_rules(cls, value: str) -> str:
        problem = name_problem(value)
        if problem is not None:
            raise ValueError(problem)
        return value


class InvalidRow(BaseModel):
    row_number: int
    reason: str
    raw: dict[str, Any]


# --------------------------------------------------------------------------- profile


class CompanyRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    id: str | None = None
    website: str | None = None
    industry: str | None = None
    size: str | None = None
    linkedin_url: str | None = None
    location_country: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _flatten_location(cls, data: Any) -> Any:
        if isinstance(data, dict):
            location = data.get("location")
            if isinstance(location, dict) and "location_country" not in data:
                data = {**data, "location_country": location.get("country")}
        return data

    @field_validator("*", mode="before")
    @classmethod
    def _obscured(cls, value: Any) -> Any:
        return _obscured_to_none(value)


class TitleRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = None
    role: str | None = None
    sub_role: str | None = None
    levels: list[str] = Field(default_factory=list)

    @field_validator("levels", mode="before")
    @classmethod
    def _levels(cls, value: Any) -> Any:
        """Drop obscured/null values and duplicates (the API repeats levels), keeping order."""
        if value is None or isinstance(value, bool):
            return []
        if isinstance(value, list):
            return list(dict.fromkeys(str(v) for v in value if v))
        return value

    @field_validator("name", "role", "sub_role", mode="before")
    @classmethod
    def _obscured(cls, value: Any) -> Any:
        return _obscured_to_none(value)


class Experience(BaseModel):
    model_config = ConfigDict(extra="ignore")

    company: CompanyRef | None = None
    title: TitleRef | None = None
    start_date: str | None = None
    end_date: str | None = None
    is_primary: bool = False

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _obscured(cls, value: Any) -> Any:
        return _obscured_to_none(value)

    @field_validator("is_primary", mode="before")
    @classmethod
    def _primary(cls, value: Any) -> Any:
        return bool(value) if value is not None else False

    @property
    def is_current(self) -> bool:
        return self.is_primary or self.end_date is None

    @property
    def start_year(self) -> int | None:
        if self.start_date and self.start_date[:4].isdigit():
            return int(self.start_date[:4])
        return None


class PersonProfile(BaseModel):
    """The subset of a provider person record that the curated layer keeps."""

    model_config = ConfigDict(extra="ignore")

    id: str
    full_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    linkedin_url: str | None = None
    location_name: str | None = None
    location_country: str | None = None
    job_title: str | None = None
    job_title_role: str | None = None
    job_company_name: str | None = None
    job_company_industry: str | None = None
    inferred_years_experience: int | None = None
    experience: list[Experience] = Field(default_factory=list)

    @field_validator(
        "full_name",
        "first_name",
        "last_name",
        "linkedin_url",
        "location_name",
        "location_country",
        "job_title",
        "job_title_role",
        "job_company_name",
        "job_company_industry",
        "inferred_years_experience",
        mode="before",
    )
    @classmethod
    def _obscured(cls, value: Any) -> Any:
        return _obscured_to_none(value)

    @field_validator("experience", mode="before")
    @classmethod
    def _experience(cls, value: Any) -> Any:
        return [] if value is None or isinstance(value, bool) else value

    def ordered_experience(self) -> list[Experience]:
        """Current positions first; within each group the most recent start date first."""
        by_recency = sorted(self.experience, key=lambda e: e.start_date or "", reverse=True)
        return sorted(by_recency, key=lambda e: 0 if e.is_current else 1)


# --------------------------------------------------------------------------- outcome


class LookupResult(BaseModel):
    """Everything the curated and operational tables need about one lookup."""

    model_config = ConfigDict(extra="forbid")

    row: InputRow
    lookup_key: str
    status: LookupStatus
    provider: str
    method: LookupMethod | None = None
    profile: PersonProfile | None = None
    likelihood: float | None = None
    candidates: int | None = None
    http_status: int | None = None
    error_message: str | None = None
    credits_consumed: int = 0
    attempts: int = 1
    raw_ref: str | None = None
    quality_flags: list[str] = Field(
        default_factory=list, description="match.* doubts about a matched profile (data guards)"
    )
    requested_at: datetime

    @property
    def person_id(self) -> str | None:
        return self.profile.id if self.profile else None
