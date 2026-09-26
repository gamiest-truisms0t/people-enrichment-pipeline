"""Emit the input data contract (docs/input-contract.json) from the ingestion code.

The contract is what a registration-system owner needs to produce a file the pipeline
accepts: required and optional columns with their recognised header spellings, the
field rules, the file-level rules and thresholds. It is generated from `ingest.py` and
`guards.py` so it cannot drift from what the code enforces; a unit test fails when the
committed file is stale. `enrich validate --input file.csv` is the same contract as an
executable dry run. Run with `make input-contract`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from enrich_pipeline import __version__, guards, ingest

OUT = Path("docs/input-contract.json")


def render() -> dict[str, Any]:
    config = guards.GuardConfig()
    return {
        "contract_version": __version__,
        "format": {
            "type": "CSV with a header row",
            "encodings": [
                "UTF-8 (with or without BOM)",
                "UTF-16 with BOM",
                "Windows-1252 (accepted with a warning)",
            ],
            "delimiters": [",", ";", "tab", "|"],
            "max_bytes": config.max_input_bytes,
            "max_rows": ingest.DEFAULT_MAX_ROWS,
        },
        "columns": {
            "required": list(ingest.REQUIRED_COLUMNS),
            "optional": list(ingest.OPTIONAL_COLUMNS),
            "header_aliases": {
                name: sorted(aliases) for name, aliases in ingest.HEADER_ALIASES.items()
            },
            "unknown_columns": "ignored, listed as a warning",
        },
        "row_rules": {
            "first_name / last_name": [
                f"1 to {guards.NAME_MAX_LENGTH} characters",
                "at least one letter, no digits",
                "not an email address",
                "not a placeholder (e.g. test, n/a, unknown, a repeated header)",
                "failing any of these rejects the row (status invalid_input, flag input.rejected)",
            ],
            "email": [
                f"local@domain.tld, at most {guards.EMAIL_MAX_LENGTH} characters",
                "otherwise dropped with note input.email_invalid; the row continues",
            ],
            "linkedin_url": [
                "linkedin.com/in/<slug> or /pub/<slug>, any scheme, www or regional subdomain",
                "canonicalised; otherwise dropped with note input.linkedin_url_invalid",
            ],
            "company / location": [
                f"at most {guards.FIELD_MAX_LENGTH} characters",
                "not a placeholder such as self-employed, student, n/a",
                "otherwise dropped with note input.company_placeholder / _too_long",
            ],
            "consent": [
                "yes/no spellings: "
                + ", ".join(sorted(guards.CONSENT_YES))
                + " | "
                + ", ".join(sorted(guards.CONSENT_NO)),
                "an explicit no rejects the row (consent: withheld)",
                "with require_consent on, a missing or unrecognised answer rejects the row too",
            ],
        },
        "file_rules": {
            "rejected_when": [
                "no header, or first_name/last_name missing from the header",
                "not text (NUL bytes) or above max_bytes",
                "a header but no data rows, or every data row rejected",
                f"more than {config.max_invalid_fraction:.0%} of "
                f"{config.min_rows_for_fraction}+ rows rejected",
            ],
            "warned_when": [
                "decoded as UTF-16 or Windows-1252",
                "delimiter other than comma",
                "unknown or duplicate columns",
                f"{config.warn_invalid_fraction:.0%} or more rows rejected",
                f"fewer than {config.min_match_rate:.0%} of valid rows matched",
            ],
        },
        "outputs": {
            "rejected_rows": "quarantine/rows/<batch_id>.csv (row_number, reason, raw fields)",
            "rejected_files": (
                "quarantine/files/<timestamp>-<name> with the reason as object metadata"
            ),
            "dry_run": "enrich validate --input file.csv (exit 0 accepted, 2 rejected)",
        },
    }


def render_text() -> str:
    return json.dumps(render(), indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)
    text = render_text()
    if args.check:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale: run `make input-contract`", file=sys.stderr)
            return 1
        return 0
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
