"""Parquet writer for the curated layer.

pyarrow is imported lazily: locally it comes from the dev dependency group, in
Lambda from the AWS-managed pandas/pyarrow layer, and the enrichment handlers
never need it at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from enrich_pipeline.schema import PARTITION_COLUMN, TABLES, Column, ColumnKind


def _arrow_type(kind: ColumnKind) -> Any:
    import pyarrow as pa

    return {
        "string": pa.string(),
        "int": pa.int32(),
        "double": pa.float64(),
        "boolean": pa.bool_(),
        "timestamp": pa.timestamp("ms"),
        "string_list": pa.list_(pa.string()),
    }[kind]


def arrow_schema(columns: tuple[Column, ...]) -> Any:
    import pyarrow as pa

    return pa.schema([pa.field(c.name, _arrow_type(c.kind), nullable=True) for c in columns])


def curated_path(base: Path, table: str, batch_date: str, batch_id: str) -> Path:
    return base / "curated" / table / f"{PARTITION_COLUMN}={batch_date}" / f"{batch_id}.parquet"


def write_table(rows: list[dict[str, Any]], *, table: str, path: Path) -> Path:
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = arrow_schema(TABLES[table])
    arrow_table = pa.Table.from_pylist(rows, schema=schema)
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(
        arrow_table,
        path,
        compression="snappy",
        coerce_timestamps="ms",
        allow_truncated_timestamps=True,
    )
    return path


def write_tables(
    tables: dict[str, list[dict[str, Any]]], *, out_dir: Path, batch_date: str, batch_id: str
) -> dict[str, Path]:
    """Write every curated table (including empty ones, so readers never miss a partition)."""
    written: dict[str, Path] = {}
    for table in TABLES:
        path = curated_path(out_dir, table, batch_date, batch_id)
        written[table] = write_table(tables.get(table, []), table=table, path=path)
    return written
