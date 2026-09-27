"""Right to erasure against real pipeline output.

The three handlers seed batches in the moto account exactly as Step Functions would, the
eraser removes one person, the real build-curated handler rebuilds, and nothing of the
person remains in any object version, the cache or the curated tables.
"""

from __future__ import annotations

import io
import json
from typing import Any

import pyarrow.parquet as pq
import pytest

from enrich_pipeline import cli
from enrich_pipeline.aws.s3 import list_keys
from enrich_pipeline.erasure import Eraser, Subject
from enrich_pipeline.handlers import build_curated, common, enrich, validate_input

from .conftest import DATA, LANDING, SAMPLE_KEY, TABLE, FakeContext

MESSY = (
    "first_name,last_name,email\n"
    "John,Doe,john@example.org\n"
    "Jane,Smith,\n"
    "test,test,jane@example.org\n"
    "R2,D2,r2@example.org\n"
)


def _run_batch(lambda_context: FakeContext, key: str) -> tuple[str, str]:
    validated = validate_input.handler({"bucket": LANDING, "key": key}, lambda_context)
    batch_id, batch_date = validated["batch_id"], validated["batch_date"]
    for row in validated["rows"]:
        enrich.handler({"batch_id": batch_id, "batch_date": batch_date, "row": row}, lambda_context)
    build_curated.handler({"batch_id": batch_id, "batch_date": batch_date}, lambda_context)
    return batch_id, batch_date


def _versions(s3: Any, key: str) -> int:
    response = s3.list_object_versions(Bucket=DATA, Prefix=key)
    items = response.get("Versions", []) + response.get("DeleteMarkers", [])
    return sum(1 for item in items if item["Key"] == key)


def _column(s3: Any, key: str, column: str) -> list[Any]:
    body = s3.get_object(Bucket=DATA, Key=key)["Body"].read()
    return pq.read_table(io.BytesIO(body)).column(column).to_pylist()


def _result(s3: Any, batch_id: str, row_number: int) -> dict[str, Any]:
    body = s3.get_object(Bucket=DATA, Key=common.result_key(batch_id, row_number))["Body"].read()
    return json.loads(body)


def _eraser(aws: dict[str, Any], lambda_context: FakeContext) -> Eraser:
    def rebuild(batch_id: str) -> None:
        build_curated.handler({"batch_id": batch_id}, lambda_context)

    return Eraser(s3=aws["s3"], table=aws["table"], bucket=DATA, rebuild=rebuild)


@pytest.fixture
def versioned(aws: dict[str, Any]) -> dict[str, Any]:
    aws["s3"].put_bucket_versioning(Bucket=DATA, VersioningConfiguration={"Status": "Enabled"})
    return aws


def test_subject_needs_an_identifier() -> None:
    with pytest.raises(ValueError, match="needs a person id"):
        Subject()
    with pytest.raises(ValueError, match="first and a last name"):
        Subject.from_name("Cher")
    subject = Subject.from_name("Mary Teresa Barra", email="mary@example.org")
    assert (subject.first_name, subject.last_name, subject.email) == (
        "Mary Teresa",
        "Barra",
        "mary@example.org",
    )
    assert (
        subject.digest()
        == Subject(first_name="mary teresa", last_name="BARRA", email="Mary@Example.org").digest()
    )


def test_dry_run_finds_the_person_and_changes_nothing(
    versioned: dict[str, Any], lambda_context: FakeContext
) -> None:
    s3 = versioned["s3"]
    batch_id, _ = _run_batch(lambda_context, SAMPLE_KEY)
    result_key = common.result_key(batch_id, 1)

    report = _eraser(versioned, lambda_context).erase(
        Subject.from_name("john DOE"), dry_run=True, actor="tests"
    )

    assert [b.batch_id for b in report.batches] == [batch_id]
    plan = report.batches[0]
    assert (plan.rows, plan.rejected_rows) == ([1], [])
    assert len(plan.lookup_keys) == 1
    assert result_key in plan.objects and any(k.startswith("raw/") for k in plan.objects)
    assert plan.rebuilt is False
    assert report.sources == [f"s3://{LANDING}/{SAMPLE_KEY}"]
    assert _versions(s3, result_key) == 1  # nothing touched
    assert not [i for i in versioned["table"].scan()["Items"] if i["pk"].startswith("erasure#")]


def test_erase_by_name_removes_every_trace_and_rebuilds(
    versioned: dict[str, Any], lambda_context: FakeContext
) -> None:
    s3, table = versioned["s3"], versioned["table"]
    batch_id, batch_date = _run_batch(lambda_context, SAMPLE_KEY)
    john = _result(s3, batch_id, 1)
    raw_key = john["raw_ref"].removeprefix(f"s3://{DATA}/")
    lookups_before = {i["pk"] for i in table.scan()["Items"] if i["pk"].startswith("lookup#")}
    person_key = common.curated_key("dim_person", batch_date, batch_id)
    lookup_key = common.curated_key("fact_lookup", batch_date, batch_id)
    assert "john doe" in _column(s3, person_key, "full_name")
    assert _versions(s3, common.input_key(batch_id)) == 1

    report = _eraser(versioned, lambda_context).erase(Subject.from_name("John Doe"), actor="tests")

    # Objects: the result and the raw response are gone in every version.
    assert _versions(s3, common.result_key(batch_id, 1)) == 0
    assert _versions(s3, raw_key) == 0
    # The input document no longer holds the row, and its old version is gone too.
    doc = json.loads(s3.get_object(Bucket=DATA, Key=common.input_key(batch_id))["Body"].read())
    assert [r["row_number"] for r in doc["rows"]] == [2, 3, 4, 5]
    assert doc["erasures"][0]["rows"] == [1]
    assert doc["erasures"][0]["request_id"] == report.request_id
    assert _versions(s3, common.input_key(batch_id)) == 1
    # The cache forgot the lookup that paid for the profile; the others remain.
    lookups_after = {i["pk"] for i in table.scan()["Items"] if i["pk"].startswith("lookup#")}
    assert lookups_before - lookups_after == {f"lookup#{john['lookup_key']}"}
    # Curated tables were rebuilt without the person and the old versions purged.
    assert report.batches[0].rebuilt is True
    assert "john doe" not in _column(s3, person_key, "full_name")
    assert 1 not in _column(s3, lookup_key, "row_number")
    assert len(_column(s3, lookup_key, "row_number")) == 5  # 4 valid rows + 1 rejected
    for key in (person_key, lookup_key, common.manifest_key(batch_id)):
        assert _versions(s3, key) == 1
    # The tombstone records what happened, not who.
    tombstone = table.get_item(Key={"pk": f"erasure#{report.request_id}"})["Item"]
    assert tombstone["batches"] == [batch_id]
    assert (int(tombstone["rows_erased"]), int(tombstone["cache_items_deleted"])) == (1, 1)
    assert tombstone["actor"] == "tests"
    assert "john" not in json.dumps(tombstone, default=str).lower()
    assert report.versions_deleted > 0


def test_erase_by_provider_id_reaches_every_batch_and_a_cached_row(
    versioned: dict[str, Any], lambda_context: FakeContext
) -> None:
    s3 = versioned["s3"]
    first, first_date = _run_batch(lambda_context, SAMPLE_KEY)
    s3.put_object(
        Bucket=LANDING,
        Key="incoming/second/names.csv",
        Body=s3.get_object(Bucket=LANDING, Key=SAMPLE_KEY)["Body"].read(),
    )
    second, second_date = _run_batch(lambda_context, "incoming/second/names.csv")
    jane = _result(s3, first, 2)
    assert _result(s3, second, 2)["status"] == "cached"

    report = _eraser(versioned, lambda_context).erase(
        Subject(person_id=jane["profile"]["id"]), actor="tests"
    )

    assert [b.batch_id for b in report.batches] == sorted([first, second])
    assert all(b.rows == [2] and b.rebuilt for b in report.batches)
    for batch_id, batch_date in ((first, first_date), (second, second_date)):
        assert _versions(s3, common.result_key(batch_id, 2)) == 0
        names = _column(s3, common.curated_key("dim_person", batch_date, batch_id), "full_name")
        assert "jane smith" not in names and "john doe" in names
    assert _versions(s3, jane["raw_ref"].removeprefix(f"s3://{DATA}/")) == 0
    assert report.cache_items_deleted == 1  # both rows shared one lookup key


def test_erase_a_rejected_row_rewrites_or_removes_the_export(
    versioned: dict[str, Any], lambda_context: FakeContext
) -> None:
    s3 = versioned["s3"]
    key = "incoming/messy/registrants.csv"
    s3.put_object(Bucket=LANDING, Key=key, Body=MESSY.encode("utf-8"))
    batch_id, batch_date = _run_batch(lambda_context, key)
    export = common.rejected_rows_key(batch_id)
    assert "jane@example.org" in s3.get_object(Bucket=DATA, Key=export)["Body"].read().decode()
    eraser = _eraser(versioned, lambda_context)

    # First erasure: one of two rejected rows; the export is rewritten without it.
    report = eraser.erase(Subject(email="jane@example.org"), actor="tests")
    assert (report.batches[0].rows, report.batches[0].rejected_rows) == ([], [3])
    body = s3.get_object(Bucket=DATA, Key=export)["Body"].read().decode()
    assert "jane@example.org" not in body and "r2@example.org" in body
    assert _versions(s3, export) == 1
    lookup = common.curated_key("fact_lookup", batch_date, batch_id)
    assert 3 not in _column(s3, lookup, "row_number")

    # Second erasure: the last rejected row; the export is removed entirely.
    report = eraser.erase(Subject(email="r2@example.org"), actor="tests")
    assert report.batches[0].rejected_rows == [4]
    assert _versions(s3, export) == 0
    assert sorted(_column(s3, lookup, "row_number")) == [1, 2]


def test_cli_erase_wires_the_deployed_stack(
    versioned: dict[str, Any],
    lambda_context: FakeContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    batch_id, _ = _run_batch(lambda_context, SAMPLE_KEY)
    invoked: list[str] = []

    def fake_invoke(client: Any, function_name: str, rebuild_id: str) -> None:
        invoked.append(f"{function_name}:{rebuild_id}")
        build_curated.handler({"batch_id": rebuild_id}, lambda_context)

    monkeypatch.setattr(cli, "_invoke_rebuild", fake_invoke)
    base = ["erase", "--bucket", DATA, "--table", TABLE, "--rebuild-function", "build-fn"]

    assert cli.main([*base, "--name", "John Doe", "--dry-run", "--json"]) == 0
    # Powertools writes one-line JSON log records to stdout too; the report is the
    # multi-line document that remains once those are dropped.
    out = capsys.readouterr().out
    preview = json.loads("\n".join(line for line in out.splitlines() if not line.startswith('{"')))
    assert preview["dry_run"] is True and preview["rows_erased"] == 1
    assert invoked == []

    assert cli.main([*base, "--name", "John Doe"]) == 0
    out = capsys.readouterr().out
    assert "erased 1 row(s) across 1 batch(es)" in out and "rebuilt" in out
    assert f"still holds the person (the operator's upload, not touched): s3://{LANDING}/" in out
    assert invoked == [f"build-fn:{batch_id}"]

    assert cli.main([*base, "--name", "John Doe"]) == 1  # nothing left to erase
    with pytest.raises(SystemExit, match="first and a last name"):
        cli.main([*base, "--name", "Cher"])


def test_nothing_found_still_leaves_a_tombstone(
    versioned: dict[str, Any], lambda_context: FakeContext
) -> None:
    _run_batch(lambda_context, SAMPLE_KEY)
    report = _eraser(versioned, lambda_context).erase(
        Subject(email="nobody@example.org"), actor="tests"
    )
    assert report.batches == [] and report.rows_erased == 0
    item = versioned["table"].get_item(Key={"pk": f"erasure#{report.request_id}"})["Item"]
    assert item["batches"] == [] and int(item["rows_erased"]) == 0
    assert list(list_keys(versioned["s3"], DATA, "results/"))  # untouched
