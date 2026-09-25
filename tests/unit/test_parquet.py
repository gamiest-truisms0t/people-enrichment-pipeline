from datetime import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from enrich_pipeline.parquet import write_tables
from enrich_pipeline.schema import TABLES


def _blank(table: str) -> dict[str, object]:
    return {c.name: None for c in TABLES[table]}


def test_write_tables_roundtrip(tmp_path: Path) -> None:
    rows = {
        "dim_person": [
            _blank("dim_person")
            | {
                "person_id": "p1",
                "batch_id": "b",
                "input_row_number": 1,
                "match_likelihood": 8.0,
                "enriched_at": datetime(2026, 10, 1, 12, 0),
            }
        ],
        "fact_employment": [
            _blank("fact_employment")
            | {
                "person_id": "p1",
                "batch_id": "b",
                "sequence_no": 0,
                "title_levels": ["senior"],
                "is_current": True,
                "start_year": 2022,
            }
        ],
        "fact_lookup": [],
    }
    files = write_tables(rows, out_dir=tmp_path, batch_date="2026-10-01", batch_id="b")

    assert set(files) == set(TABLES)
    assert files["dim_person"] == (
        tmp_path / "curated" / "dim_person" / "batch_date=2026-10-01" / "b.parquet"
    )

    person = pq.read_table(files["dim_person"]).to_pylist()
    assert person[0]["person_id"] == "p1"
    assert person[0]["enriched_at"] == datetime(2026, 10, 1, 12, 0)

    employment = pq.read_table(files["fact_employment"])
    levels_type = employment.schema.field("title_levels").type
    assert pa.types.is_list(levels_type)
    assert levels_type.value_type == pa.string()
    assert employment.schema.field("sequence_no").type == pa.int32()
    assert employment.to_pylist()[0]["title_levels"] == ["senior"]

    assert pq.read_table(files["fact_lookup"]).num_rows == 0
