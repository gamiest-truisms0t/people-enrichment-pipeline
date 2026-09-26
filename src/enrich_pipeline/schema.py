"""Single source of truth for the curated tables.

The transform builds rows with exactly these columns, the Parquet writer derives
its Arrow schema from them, and Phase 5 generates the Glue table definitions
from the same list so the three never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ColumnKind = Literal["string", "int", "double", "boolean", "timestamp", "string_list"]

PARTITION_COLUMN = "batch_date"
DATABASE_NAME = "people_enrichment"


@dataclass(frozen=True)
class Column:
    name: str
    kind: ColumnKind
    description: str = ""


DIM_PERSON: tuple[Column, ...] = (
    Column("person_id", "string", "provider's stable person id"),
    Column("provider", "string"),
    Column("batch_id", "string"),
    Column("input_row_number", "int"),
    Column("input_first_name", "string"),
    Column("input_last_name", "string"),
    Column("input_email", "string"),
    Column("full_name", "string"),
    Column("first_name", "string"),
    Column("last_name", "string"),
    Column("linkedin_url", "string"),
    Column("location_country", "string"),
    Column("location_name", "string", "null on PDL's free plan"),
    Column("current_job_title", "string"),
    Column("current_job_title_role", "string"),
    Column("current_company_name", "string"),
    Column("current_company_industry", "string"),
    Column("inferred_years_experience", "int"),
    Column("match_likelihood", "double", "PDL likelihood 1-10 or Identify match_score 1-99"),
    Column("lookup_method", "string"),
    Column("quality_flags", "string_list", "input.* notes and match.* doubts (data guards)"),
    Column("enriched_at", "timestamp"),
)

FACT_EMPLOYMENT: tuple[Column, ...] = (
    Column("person_id", "string"),
    Column("batch_id", "string"),
    Column("sequence_no", "int", "0 = current/most recent position"),
    Column("company_name", "string"),
    Column("company_id", "string"),
    Column("company_website", "string"),
    Column("company_linkedin_url", "string"),
    Column("company_industry", "string"),
    Column("company_size", "string"),
    Column("company_location_country", "string"),
    Column("title_name", "string"),
    Column("title_role", "string"),
    Column("title_sub_role", "string"),
    Column("title_levels", "string_list"),
    Column("start_date", "string", "as delivered: YYYY, YYYY-MM or YYYY-MM-DD"),
    Column("end_date", "string"),
    Column("start_year", "int"),
    Column("is_current", "boolean"),
)

FACT_LOOKUP: tuple[Column, ...] = (
    Column("batch_id", "string"),
    Column("row_number", "int"),
    Column("input_first_name", "string"),
    Column("input_last_name", "string"),
    Column("input_email", "string"),
    Column("input_company", "string"),
    Column("lookup_key", "string"),
    Column("status", "string"),
    Column("lookup_method", "string"),
    Column("person_id", "string"),
    Column("likelihood", "double"),
    Column("candidates", "int"),
    Column("http_status", "int"),
    Column("error_message", "string"),
    Column("credits_consumed", "int"),
    Column("attempts", "int"),
    Column("provider", "string"),
    Column("raw_ref", "string"),
    Column("quality_flags", "string_list", "input.* notes and match.* doubts (data guards)"),
    Column("requested_at", "timestamp"),
)

TABLES: dict[str, tuple[Column, ...]] = {
    "dim_person": DIM_PERSON,
    "fact_employment": FACT_EMPLOYMENT,
    "fact_lookup": FACT_LOOKUP,
}

GLUE_TYPES: dict[ColumnKind, str] = {
    "string": "string",
    "int": "int",
    "double": "double",
    "boolean": "boolean",
    "timestamp": "timestamp",
    "string_list": "array<string>",
}
