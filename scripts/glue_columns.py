"""Emit the Glue column definitions for the curated tables from `schema.py`.

schema.py is the single source of truth for the transform, the Parquet writer and the
Glue tables. This writes infra/modules/catalog/columns.json for Terraform to read; a unit
test fails when the committed file is stale. Run with `make glue-columns`.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from enrich_pipeline.schema import DATABASE_NAME, GLUE_TYPES, PARTITION_COLUMN, TABLES

OUT = Path("infra/modules/catalog/columns.json")


def render() -> dict[str, Any]:
    return {
        "database_base_name": DATABASE_NAME,
        "partition_column": PARTITION_COLUMN,
        "tables": {
            table: [
                {
                    "name": column.name,
                    "type": GLUE_TYPES[column.kind],
                    "comment": column.description,
                }
                for column in columns
            ]
            for table, columns in TABLES.items()
        },
    }


def render_text() -> str:
    return json.dumps(render(), indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args(argv)
    text = render_text()
    if args.check:
        if OUT.is_file() and OUT.read_text(encoding="utf-8") == text:
            return 0
        print(f"{OUT} is stale: run `make glue-columns`", file=sys.stderr)
        return 1
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
