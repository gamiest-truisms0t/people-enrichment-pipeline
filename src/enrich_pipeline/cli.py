"""Command-line entry point for local runs.

enrich run   --input data/sample/names.csv --provider mock --out ./out
enrich query --out ./out          # answers the brief's three questions with DuckDB
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from enrich_pipeline import __version__
from enrich_pipeline.enricher import EnrichConfig
from enrich_pipeline.guards import GuardConfig, InputError
from enrich_pipeline.providers.base import Provider

QUESTIONS: dict[str, str] = {
    "1. Who are the individuals identified?": """
        SELECT full_name, current_job_title, current_company_name, location_country,
               match_likelihood, lookup_method
        FROM read_parquet('{out}/curated/dim_person/*/*.parquet', hive_partitioning = true)
        ORDER BY full_name
    """,
    "2. What companies have they worked at?": """
        SELECT p.full_name, e.company_name, e.company_industry, e.start_date, e.end_date,
               e.is_current
        FROM read_parquet('{out}/curated/fact_employment/*/*.parquet', hive_partitioning = true) e
        JOIN read_parquet('{out}/curated/dim_person/*/*.parquet', hive_partitioning = true) p
          ON p.person_id = e.person_id AND p.batch_id = e.batch_id
        ORDER BY p.full_name, e.sequence_no
    """,
    "3. What roles have they held?": """
        SELECT p.full_name, e.title_name, e.title_role, e.title_levels, e.company_name
        FROM read_parquet('{out}/curated/fact_employment/*/*.parquet', hive_partitioning = true) e
        JOIN read_parquet('{out}/curated/dim_person/*/*.parquet', hive_partitioning = true) p
          ON p.person_id = e.person_id AND p.batch_id = e.batch_id
        ORDER BY p.full_name, e.sequence_no
    """,
    "Operational: outcome per input row": """
        SELECT batch_id, row_number, input_first_name, input_last_name, status, lookup_method,
               likelihood, candidates, http_status, credits_consumed, attempts
        FROM read_parquet('{out}/curated/fact_lookup/*/*.parquet', hive_partitioning = true)
        ORDER BY batch_id, row_number
    """,
}


DEFAULT_KEY_FILE = Path.home() / ".config" / "people-enrichment" / "pdl_api_key"


def _read_api_key(path: Path | None) -> str:
    """An explicit --api-key-file wins; otherwise PDL_API_KEY; otherwise the default file."""
    if path is None:
        if key := os.environ.get("PDL_API_KEY"):
            return key.strip()
        path = DEFAULT_KEY_FILE
    if not path.is_file():
        raise SystemExit(f"no API key: set PDL_API_KEY or create {path}")
    key = path.read_text(encoding="utf-8").strip()
    if not key:
        raise SystemExit(f"API key file {path} is empty")
    return key


def make_provider(
    name: str, *, sandbox: bool = False, api_key_file: Path | None = None
) -> Provider:
    from enrich_pipeline.providers.factory import make_provider as _make

    api_key = _read_api_key(api_key_file) if name == "pdl" else None
    try:
        return _make(name, api_key=api_key, sandbox=sandbox)
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="enrich", description="People-enrichment pipeline")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="enrich a CSV locally and write raw JSON + curated Parquet")
    run.add_argument("--input", required=True, type=Path, help="CSV with first_name,last_name,...")
    run.add_argument("--out", default=Path("out"), type=Path, help="output directory")
    run.add_argument("--provider", default="mock", choices=["mock", "pdl"])
    run.add_argument(
        "--sandbox",
        action="store_true",
        help="use the provider's sandbox host (PDL: synthetic people, zero credits)",
    )
    run.add_argument(
        "--api-key-file",
        type=Path,
        default=None,
        help=f"file holding the PDL API key; otherwise PDL_API_KEY, otherwise {DEFAULT_KEY_FILE}",
    )
    run.add_argument("--batch-id", default=None)
    run.add_argument("--location-hint", default=None, help="event location added to name lookups")
    run.add_argument("--max-enrich-credits", type=int, default=None)
    run.add_argument("--max-identify-credits", type=int, default=None)
    run.add_argument(
        "--max-credits-per-batch",
        type=int,
        default=None,
        help="stop spending after this many credits in this run (rows become budget_deferred)",
    )
    run.add_argument("--identify-min-score", type=int, default=70)
    run.add_argument("--identify-min-margin", type=int, default=20)
    run.add_argument(
        "--enrich-min-likelihood",
        type=int,
        default=EnrichConfig.enrich_min_likelihood,
        help="provider-side match threshold for enrich calls, 1-10",
    )
    run.add_argument(
        "--max-invalid-fraction",
        type=float,
        default=GuardConfig.max_invalid_fraction,
        help="abort when more than this share of rows is rejected (files of 5+ rows)",
    )
    run.add_argument(
        "--min-match-rate",
        type=float,
        default=GuardConfig.min_match_rate,
        help="warn when fewer than this share of valid rows matched",
    )
    run.add_argument("--json", action="store_true", help="print the run summary as JSON")

    query = sub.add_parser("query", help="answer the brief's questions against local Parquet")
    query.add_argument("--out", default=Path("out"), type=Path)
    return parser


def _print_table(columns: Sequence[str], rows: Sequence[Sequence[object]]) -> None:
    cells = [[("" if v is None else str(v)) for v in row] for row in rows]
    widths = [len(c) for c in columns]
    for row in cells:
        for i, value in enumerate(row):
            widths[i] = min(max(widths[i], len(value)), 48)
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    print(fmt.format(*columns))
    print(fmt.format(*("-" * w for w in widths)))
    for row in cells:
        print(fmt.format(*(v[:48] for v in row)))
    print(f"({len(rows)} rows)")


def cmd_run(args: argparse.Namespace) -> int:
    from enrich_pipeline.runner import run_batch

    config = EnrichConfig(
        identify_min_score=args.identify_min_score,
        identify_min_margin=args.identify_min_margin,
        enrich_min_likelihood=args.enrich_min_likelihood,
        max_enrich_credits=args.max_enrich_credits,
        max_identify_credits=args.max_identify_credits,
        max_credits_per_batch=args.max_credits_per_batch,
        location_hint=args.location_hint,
    )
    guards = GuardConfig(
        max_invalid_fraction=args.max_invalid_fraction, min_match_rate=args.min_match_rate
    )
    try:
        summary = run_batch(
            args.input,
            provider=make_provider(
                args.provider, sandbox=args.sandbox, api_key_file=args.api_key_file
            ),
            out_dir=args.out,
            config=config,
            guards=guards,
            batch_id=args.batch_id,
        )
    except InputError as exc:
        print(f"input rejected: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(summary.to_json())
        return 0
    print(f"batch {summary.batch_id} ({summary.batch_date}) via {summary.provider}")
    print(f"rows: {summary.rows_valid} valid, {summary.rows_invalid} invalid")
    for status, count in summary.status_counts.items():
        print(f"  {status:<16} {count}")
    print(f"credits spent: {summary.credits_spent}")
    print(f"persons: {summary.persons}, employment rows: {summary.employment_rows}")
    for warning in summary.warnings:
        print(f"warning: {warning}")
    print(f"manifest: {summary.manifest}")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    try:
        import duckdb
    except ImportError:  # pragma: no cover
        raise SystemExit("duckdb is a dev dependency: run `uv sync` first") from None

    out = str(Path(args.out).resolve())
    con = duckdb.connect()
    for title, sql in QUESTIONS.items():
        print(f"\n== {title}")
        cursor = con.execute(sql.format(out=out))
        columns = [d[0] for d in cursor.description]
        _print_table(columns, cursor.fetchall())
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "query":
        return cmd_query(args)
    parser.print_help()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
