"""Lookup results -> rows for the three curated tables.

Rows are plain dicts whose keys are exactly the columns declared in `schema.py`;
`_row` enforces that so the writer, Glue tables and this module cannot drift.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from enrich_pipeline import __version__
from enrich_pipeline.models import (
    CompanyRef,
    InvalidRow,
    LookupResult,
    LookupStatus,
    PersonProfile,
    TitleRef,
)
from enrich_pipeline.schema import TABLES

Row = dict[str, Any]
Tables = dict[str, list[Row]]

WITH_PROFILE = frozenset({LookupStatus.MATCHED, LookupStatus.CACHED})


def _naive_utc(value: datetime) -> datetime:
    """Parquet/Athena timestamps are stored as naive UTC."""
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _row(table: str, **values: Any) -> Row:
    columns = [c.name for c in TABLES[table]]
    unknown = set(values) - set(columns)
    if unknown:
        raise KeyError(f"{table}: unknown column(s) {sorted(unknown)}")
    return {name: values.get(name) for name in columns}


def person_row(result: LookupResult, batch_id: str, version: str = __version__) -> Row:
    profile = result.profile
    assert profile is not None  # callers only pass matched results
    return _row(
        "dim_person",
        pipeline_version=version,
        person_id=profile.id,
        provider=result.provider,
        batch_id=batch_id,
        input_row_number=result.row.row_number,
        input_first_name=result.row.first_name,
        input_last_name=result.row.last_name,
        input_email=result.row.email,
        full_name=profile.full_name,
        first_name=profile.first_name,
        last_name=profile.last_name,
        linkedin_url=profile.linkedin_url,
        location_country=profile.location_country,
        location_name=profile.location_name,
        current_job_title=profile.job_title,
        current_job_title_role=profile.job_title_role,
        current_company_name=profile.job_company_name,
        current_company_industry=profile.job_company_industry,
        inferred_years_experience=profile.inferred_years_experience,
        match_likelihood=result.likelihood,
        lookup_method=result.method.value if result.method else None,
        quality_flags=quality_flags(result),
        enriched_at=_naive_utc(result.requested_at),
    )


def quality_flags(result: LookupResult) -> list[str]:
    """Input notes (fields a guard dropped) followed by doubts about the match, deduplicated."""
    return list(dict.fromkeys([*result.row.notes, *result.quality_flags]))


def employment_rows(profile: PersonProfile, batch_id: str, version: str = __version__) -> list[Row]:
    rows: list[Row] = []
    for sequence_no, exp in enumerate(profile.ordered_experience()):
        company = exp.company or CompanyRef()
        title = exp.title or TitleRef()
        rows.append(
            _row(
                "fact_employment",
                pipeline_version=version,
                person_id=profile.id,
                batch_id=batch_id,
                sequence_no=sequence_no,
                company_name=company.name,
                company_id=company.id,
                company_website=company.website,
                company_linkedin_url=company.linkedin_url,
                company_industry=company.industry,
                company_size=company.size,
                company_location_country=company.location_country,
                title_name=title.name,
                title_role=title.role,
                title_sub_role=title.sub_role,
                title_levels=list(title.levels),
                start_date=exp.start_date,
                end_date=exp.end_date,
                start_year=exp.start_year,
                is_current=exp.is_current,
            )
        )
    return rows


def lookup_row(result: LookupResult, batch_id: str, version: str = __version__) -> Row:
    return _row(
        "fact_lookup",
        pipeline_version=version,
        batch_id=batch_id,
        row_number=result.row.row_number,
        input_first_name=result.row.first_name,
        input_last_name=result.row.last_name,
        input_email=result.row.email,
        input_company=result.row.company,
        lookup_key=result.lookup_key,
        status=result.status.value,
        lookup_method=result.method.value if result.method else None,
        person_id=result.person_id,
        likelihood=result.likelihood,
        candidates=result.candidates,
        http_status=result.http_status,
        error_message=result.error_message,
        credits_consumed=result.credits_consumed,
        attempts=result.attempts,
        provider=result.provider,
        raw_ref=result.raw_ref,
        quality_flags=quality_flags(result),
        requested_at=_naive_utc(result.requested_at),
    )


def invalid_row(
    invalid: InvalidRow, batch_id: str, provider: str, at: datetime, version: str = __version__
) -> Row:
    return _row(
        "fact_lookup",
        pipeline_version=version,
        batch_id=batch_id,
        row_number=invalid.row_number,
        input_first_name=invalid.raw.get("first_name") or None,
        input_last_name=invalid.raw.get("last_name") or None,
        input_email=invalid.raw.get("email") or None,
        input_company=invalid.raw.get("company") or None,
        status=LookupStatus.INVALID_INPUT.value,
        error_message=invalid.reason,
        credits_consumed=0,
        attempts=0,
        provider=provider,
        quality_flags=["input.rejected"],
        requested_at=_naive_utc(at),
    )


def build_tables(
    results: Iterable[LookupResult],
    *,
    batch_id: str,
    invalid: Sequence[InvalidRow] = (),
    provider: str = "",
    at: datetime | None = None,
    version: str = __version__,
) -> Tables:
    """One person row per distinct match, all of their positions, one lookup row per input.

    Every row carries `pipeline_version`, the package version that produced it, so a
    rebuild after a transform change is visible in the data.
    """
    persons: dict[str, Row] = {}
    employment: list[Row] = []
    lookups: list[Row] = []

    # A cached hit carries the profile from an earlier lookup: it cost nothing, but the
    # person still belongs in this batch's tables. Only results without a profile
    # (not found, ambiguous, deferred, error) contribute nothing beyond the lookup row.
    for result in results:
        lookups.append(lookup_row(result, batch_id, version))
        if result.status in WITH_PROFILE and result.profile is not None:
            if result.profile.id in persons:
                continue
            persons[result.profile.id] = person_row(result, batch_id, version)
            employment.extend(employment_rows(result.profile, batch_id, version))

    stamp = at or datetime.now(UTC)
    lookups.extend(invalid_row(inv, batch_id, provider, stamp, version) for inv in invalid)
    lookups.sort(key=lambda r: r["row_number"])

    return {
        "dim_person": list(persons.values()),
        "fact_employment": employment,
        "fact_lookup": lookups,
        "fact_batch_quality": [],  # filled by quality_row once the batch report exists
    }


def quality_row(
    *,
    batch_id: str,
    provider: str,
    quality: dict[str, Any],
    status_counts: dict[str, int],
    rows_unrecorded: int,
    credits_spent: int,
    persons: int,
    employment_rows: int,
    built_at: datetime,
    version: str = __version__,
) -> Row:
    """The batch's quality report as one fact_batch_quality row (see guards.QualityReport)."""
    return _row(
        "fact_batch_quality",
        batch_id=batch_id,
        provider=provider,
        pipeline_version=version,
        rows_valid=quality["rows_valid"],
        rows_invalid=quality["rows_invalid"],
        rows_unrecorded=rows_unrecorded,
        matched=status_counts.get("matched", 0),
        cached=status_counts.get("cached", 0),
        not_found=status_counts.get("not_found", 0),
        ambiguous=status_counts.get("ambiguous", 0),
        budget_deferred=status_counts.get("budget_deferred", 0),
        provider_unavailable=status_counts.get("provider_unavailable", 0),
        error=status_counts.get("error", 0),
        match_rate=quality["match_rate"],
        flagged_matches=quality["flagged_matches"],
        warning_count=quality["warning_count"],
        warnings=list(quality["warnings"]),
        credits_spent=credits_spent,
        persons=persons,
        employment_rows=employment_rows,
        built_at=_naive_utc(built_at),
    )
