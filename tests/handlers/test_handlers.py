"""The three Lambda handlers chained by hand, the way Step Functions will drive them."""

from __future__ import annotations

import io
import json
from typing import Any

import pyarrow.parquet as pq
import pytest

from enrich_pipeline.aws.s3 import list_keys
from enrich_pipeline.enricher import EnrichConfig, plan_lookup
from enrich_pipeline.handlers import build_curated, common, enrich, validate_input
from enrich_pipeline.ingest import InputError
from enrich_pipeline.models import InputRow

from .conftest import DATA, LANDING, SAMPLE_KEY, FakeContext


def _keys(s3: Any, prefix: str) -> list[str]:
    return sorted(list_keys(s3, DATA, prefix))


def _emf_records(stdout: str) -> list[dict[str, Any]]:
    """Powertools flushes one CloudWatch EMF JSON document per invocation to stdout."""
    records = []
    for line in stdout.splitlines():
        if line.startswith("{"):
            record = json.loads(line)
            if "_aws" in record:
                records.append(record)
    return records


def test_validate_then_enrich_then_build(
    aws: dict[str, Any], lambda_context: FakeContext, capsys: pytest.CaptureFixture[str]
) -> None:
    s3 = aws["s3"]

    validated = validate_input.handler({"bucket": LANDING, "key": SAMPLE_KEY}, lambda_context)
    assert validated["row_count"] == 5
    assert validated["invalid_count"] == 1
    assert len(validated["rows"]) == 5
    batch_id, batch_date = validated["batch_id"], validated["batch_date"]

    stored = json.loads(s3.get_object(Bucket=DATA, Key=common.input_key(batch_id))["Body"].read())
    assert len(stored["invalid"]) == 1
    assert stored["source"] == {"bucket": LANDING, "key": SAMPLE_KEY}

    outcomes = [
        enrich.handler({"batch_id": batch_id, "batch_date": batch_date, "row": row}, lambda_context)
        for row in validated["rows"]
    ]
    assert [o["status"] for o in outcomes] == [
        "matched",
        "matched",
        "ambiguous",
        "not_found",
        "cached",
    ]
    assert sum(o["credits_consumed"] for o in outcomes) == 3
    assert all(o["result_ref"].startswith(f"s3://{DATA}/results/") for o in outcomes)

    # The month-to-date gauges the credit alarms in monitoring.tf watch, under the
    # service=enrich dimension, climb 1, 2, 3 on the billed rows and hold on the rest.
    gauges = [
        r for r in _emf_records(capsys.readouterr().out) if "IdentifyCreditsUsedThisMonth" in r
    ]
    # EMF stores each metric's values as a list, one entry per add_metric call.
    assert [r["IdentifyCreditsUsedThisMonth"] for r in gauges] == [[1], [2], [3], [3], [3]]
    assert [r["EnrichCreditsUsedThisMonth"] for r in gauges] == [[0]] * 5
    assert all(r["service"] == "enrich" for r in gauges)
    metric_names = {m["Name"] for m in gauges[-1]["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert {
        "EnrichCreditsUsedThisMonth",
        "IdentifyCreditsUsedThisMonth",
        "CreditsSpent",
    } <= metric_names
    assert outcomes[0]["raw_ref"].startswith(f"s3://{DATA}/raw/provider=mock/")
    assert outcomes[4]["raw_ref"] == outcomes[2]["raw_ref"]  # cached row points at the original

    assert len(_keys(s3, f"raw/provider=mock/batch_date={batch_date}/batch_id={batch_id}/")) == 4
    assert len(_keys(s3, common.results_prefix(batch_id))) == 5

    items = aws["table"].scan()["Items"]
    lookups = [i for i in items if i["pk"].startswith("lookup#")]
    budgets = [i for i in items if i["pk"].startswith("budget#mock#")]
    assert len(lookups) == 4  # john, jane, alex, josé; the duplicate alex is a hit
    assert {i["call_kind"] for i in budgets} == {"identify"}
    assert int(budgets[0]["spent"]) == 3

    manifest = build_curated.handler({"batch_id": batch_id}, lambda_context)
    assert manifest["batch_date"] == batch_date
    assert manifest["persons"] == 2
    assert manifest["employment_rows"] == 5
    assert manifest["credits_spent"] == 3
    assert manifest["status_counts"] == {
        "ambiguous": 1,
        "cached": 1,
        "invalid_input": 1,
        "matched": 2,
        "not_found": 1,
    }
    assert set(manifest["files"]) == {"dim_person", "fact_employment", "fact_lookup"}

    person_key = common.curated_key("dim_person", batch_date, batch_id)
    table = pq.read_table(io.BytesIO(s3.get_object(Bucket=DATA, Key=person_key)["Body"].read()))
    assert sorted(table.column("full_name").to_pylist()) == ["jane smith", "john doe"]

    lookup_key = common.curated_key("fact_lookup", batch_date, batch_id)
    lookups_table = pq.read_table(
        io.BytesIO(s3.get_object(Bucket=DATA, Key=lookup_key)["Body"].read())
    )
    assert lookups_table.num_rows == 6  # five valid rows plus the invalid one

    stored_manifest = json.loads(
        s3.get_object(Bucket=DATA, Key=common.manifest_key(batch_id))["Body"].read()
    )
    assert stored_manifest["batch_id"] == batch_id


def test_budget_is_shared_across_invocations(
    aws: dict[str, Any], lambda_context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_IDENTIFY_CREDITS", "1")
    event = {"batch_id": "b", "batch_date": "2026-10-01"}
    first = enrich.handler(
        {**event, "row": {"row_number": 1, "first_name": "John", "last_name": "Doe"}},
        lambda_context,
    )
    second = enrich.handler(
        {**event, "row": {"row_number": 2, "first_name": "Alex", "last_name": "Lee"}},
        lambda_context,
    )
    assert first["status"] == "matched"
    assert second["status"] == "budget_deferred"
    assert second["credits_consumed"] == 0


def test_402_exhaustion_persists_between_invocations(
    aws: dict[str, Any], lambda_context: FakeContext
) -> None:
    event = {"batch_id": "b", "batch_date": "2026-10-01"}
    first = enrich.handler(
        {
            **event,
            "row": {
                "row_number": 1,
                "first_name": "Budget",
                "last_name": "Exhausted",
                "company": "x",
            },
        },
        lambda_context,
    )
    second = enrich.handler(
        {
            **event,
            "row": {
                "row_number": 2,
                "first_name": "Jane",
                "last_name": "Smith",
                "company": "Globex",
            },
        },
        lambda_context,
    )
    assert first["status"] == "budget_deferred"
    assert first["http_status"] == 402
    assert second["status"] == "budget_deferred"
    assert second["attempts"] == 0

    marker = aws["table"].get_item(Key={"pk": "budget#mock#2026-10#exhausted#enrich"}).get("Item")
    assert marker and marker["exhausted"] is True

    # Identify is a separate credit pool and is unaffected by the enrich 402.
    third = enrich.handler(
        {**event, "row": {"row_number": 3, "first_name": "John", "last_name": "Doe"}},
        lambda_context,
    )
    assert third["status"] == "matched"


def _lookup_rows(s3: Any, batch_date: str, batch_id: str) -> list[dict[str, Any]]:
    key = common.curated_key("fact_lookup", batch_date, batch_id)
    return pq.read_table(io.BytesIO(s3.get_object(Bucket=DATA, Key=key)["Body"].read())).to_pylist()


def test_build_curated_records_rows_without_results(
    aws: dict[str, Any], lambda_context: FakeContext
) -> None:
    """A row whose enrich invocation crashed has no result object. The curated step still
    gives it a fact_lookup row, carrying the Step Functions Catch output as its message."""
    s3 = aws["s3"]
    validated = validate_input.handler({"bucket": LANDING, "key": SAMPLE_KEY}, lambda_context)
    batch_id, batch_date = validated["batch_id"], validated["batch_date"]
    crashed = validated["rows"][3]  # never enriched: a timeout after the Map's retries
    for row in validated["rows"]:
        if row["row_number"] != crashed["row_number"]:
            enrich.handler(
                {"batch_id": batch_id, "batch_date": batch_date, "row": row}, lambda_context
            )

    row_errors = [
        {
            "row_number": crashed["row_number"],
            "status": "error",
            "error": "States.Timeout",
            "cause": "Task timed out after 90 seconds",
        }
    ]
    manifest = build_curated.handler(
        {"batch_id": batch_id, "batch_date": batch_date, "row_errors": row_errors},
        lambda_context,
    )
    assert manifest["rows_valid"] == 5
    assert manifest["rows_unrecorded"] == 1
    assert manifest["status_counts"]["error"] == 1

    lookups = _lookup_rows(s3, batch_date, batch_id)
    assert len(lookups) == 6  # one row per input row, invalid one included
    (error_row,) = [r for r in lookups if r["row_number"] == crashed["row_number"]]
    assert error_row["status"] == "error"
    assert error_row["error_message"] == "States.Timeout: Task timed out after 90 seconds"
    assert error_row["credits_consumed"] == 0
    expected = plan_lookup(InputRow.model_validate(crashed), EnrichConfig())
    assert error_row["lookup_key"] == expected.key
    assert error_row["lookup_method"] == expected.method.value

    # `make rebuild` invokes the step without the Catch output: the row stays, with a
    # generic message.
    build_curated.handler({"batch_id": batch_id}, lambda_context)
    (error_row,) = [
        r
        for r in _lookup_rows(s3, batch_date, batch_id)
        if r["row_number"] == crashed["row_number"]
    ]
    assert error_row["status"] == "error"
    assert error_row["error_message"] == build_curated.UNRECORDED_MESSAGE

    # A Catch record without a usable row_number is logged and ignored, never matched.
    input_doc = json.loads(
        s3.get_object(Bucket=DATA, Key=common.input_key(batch_id))["Body"].read()
    )
    synthesized = build_curated.unrecorded_rows(
        input_doc,
        [],
        [{"status": "error", "error": "States.Timeout"}, {"row_number": "n/a"}],
        provider="mock",
        config=EnrichConfig(),
        at=common.utcnow(),
    )
    assert [r.row.row_number for r in synthesized] == [r["row_number"] for r in input_doc["rows"]]
    assert {r.error_message for r in synthesized} == {build_curated.UNRECORDED_MESSAGE}


def test_validate_rejects_a_bad_header(aws: dict[str, Any], lambda_context: FakeContext) -> None:
    aws["s3"].put_object(Bucket=LANDING, Key="incoming/bad/bad.csv", Body=b"name\nJohn Doe\n")
    with pytest.raises(InputError, match="missing required"):
        validate_input.handler({"bucket": LANDING, "key": "incoming/bad/bad.csv"}, lambda_context)
