"""The committed Glue column file must match schema.py, the single source of truth."""

import json
import subprocess
import sys
from pathlib import Path

from enrich_pipeline.schema import DATABASE_NAME, GLUE_TYPES, PARTITION_COLUMN, TABLES

ROOT = Path(__file__).resolve().parents[2]
COLUMNS = ROOT / "infra" / "modules" / "catalog" / "columns.json"


def test_columns_json_matches_schema() -> None:
    doc = json.loads(COLUMNS.read_text(encoding="utf-8"))
    assert doc["database_base_name"] == DATABASE_NAME
    assert doc["partition_column"] == PARTITION_COLUMN
    assert set(doc["tables"]) == set(TABLES)
    for table, columns in TABLES.items():
        assert [c["name"] for c in doc["tables"][table]] == [c.name for c in columns]
        assert [c["type"] for c in doc["tables"][table]] == [GLUE_TYPES[c.kind] for c in columns]


def test_partition_column_is_not_also_a_regular_column() -> None:
    for columns in TABLES.values():
        assert PARTITION_COLUMN not in {c.name for c in columns}


def test_generator_check_mode_passes_on_committed_file() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "glue_columns.py"), "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
