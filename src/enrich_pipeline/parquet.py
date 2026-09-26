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
    verify_written(written, tables)
    return written


def verify_written(files: dict[str, Path], tables: dict[str, list[dict[str, Any]]]) -> None:
    """Read each file's footer back and compare row counts and schema with what was written.

    Raises OutputCheckError so a truncated or mis-typed file never reaches the curated
    prefix. The check reads only Parquet metadata, not the row groups.
    """
    import pyarrow.parquet as pq

    from enrich_pipeline.guards import OutputCheckError

    for table, path in files.items():
        metadata = pq.read_metadata(path)
        expected_rows = len(tables.get(table, []))
        if metadata.num_rows != expected_rows:
            raise OutputCheckError(
                f"{table}: {path.name} holds {metadata.num_rows} rows, expected {expected_rows}"
            )
        names = pq.read_schema(path).names
        if names != [c.name for c in TABLES[table]]:
            raise OutputCheckError(f"{table}: {path.name} columns differ from schema.py")
