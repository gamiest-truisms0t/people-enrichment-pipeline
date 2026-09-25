"""End-to-end: the sample CSV through the mock provider, verified with DuckDB."""

import json
from pathlib import Path

import duckdb
import pytest

from enrich_pipeline.cli import main
from enrich_pipeline.providers.mock import MockProvider
from enrich_pipeline.runner import run_batch

SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample" / "names.csv"


def _parquet(out: Path, table: str) -> str:
    return f"read_parquet('{out}/curated/{table}/*/*.parquet', hive_partitioning = true)"


def test_run_batch_on_the_sample_file(tmp_path: Path) -> None:
    summary = run_batch(SAMPLE, provider=MockProvider(), out_dir=tmp_path, batch_id="test-batch")

    assert summary.rows_valid == 5
    assert summary.rows_invalid == 1
    assert summary.status_counts == {
        "ambiguous": 1,
        "cached": 1,
        "invalid_input": 1,
        "matched": 2,
        "not_found": 1,
    }
    assert summary.credits_spent == 3  # three identify calls; the 404 enrich is free
    assert summary.persons == 2
    assert summary.employment_rows == 5

    manifest = json.loads(Path(summary.manifest).read_text(encoding="utf-8"))
    assert manifest["batch_id"] == "test-batch"
    assert set(manifest["files"]) == {"dim_person", "fact_employment", "fact_lookup"}

    raw_files = sorted((tmp_path / "raw").rglob("*.json"))
    assert len(raw_files) == 4  # one per provider round-trip; cached and invalid rows write none
    record = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert {"request", "response", "lookup_key", "provider"} <= set(record)


def test_the_three_questions_with_duckdb(tmp_path: Path) -> None:
    run_batch(SAMPLE, provider=MockProvider(), out_dir=tmp_path, batch_id="q")
    con = duckdb.connect()
    person = _parquet(tmp_path, "dim_person")
    employment = _parquet(tmp_path, "fact_employment")

    who = con.execute(
        f"SELECT full_name, current_company_name FROM {person} ORDER BY full_name"
    ).fetchall()
    assert who == [("jane smith", "globex corporation"), ("john doe", "northwind analytics")]

    companies = con.execute(
        f"""
        SELECT e.company_name
        FROM {employment} e JOIN {person} p USING (person_id, batch_id)
        WHERE p.full_name = 'john doe' ORDER BY e.sequence_no
        """
    ).fetchall()
    assert [c[0] for c in companies] == [
        "northwind analytics",
        "contoso logistics",
        "fabrikam retail",
    ]

    roles = con.execute(
        f"""
        SELECT e.title_name, e.is_current
        FROM {employment} e JOIN {person} p USING (person_id, batch_id)
        WHERE p.full_name = 'jane smith' ORDER BY e.sequence_no
        """
    ).fetchall()
    assert roles == [("head of product", True), ("product manager", False)]

    partitions = con.execute(
        f"SELECT DISTINCT batch_date FROM {_parquet(tmp_path, 'fact_lookup')}"
    ).fetchall()
    assert len(partitions) == 1


def test_cli_run_then_query(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        [
            "run",
            "--input",
            str(SAMPLE),
            "--out",
            str(tmp_path),
            "--provider",
            "mock",
            "--batch-id",
            "cli",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "batch cli" in out
    assert "matched" in out

    assert main(["query", "--out", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "Who are the individuals identified" in out
    assert "john doe" in out
    assert "northwind analytics" in out


def test_cli_json_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    main(["run", "--input", str(SAMPLE), "--out", str(tmp_path), "--json", "--batch-id", "j"])
    summary = json.loads(capsys.readouterr().out)
    assert summary["batch_id"] == "j"
    assert summary["credits_spent"] == 3
