"""Execution-level idempotency, the per-batch credit cap, quarantine and rejected-row export."""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from enrich_pipeline.handlers import build_curated, common, enrich, validate_input
from enrich_pipeline.ingest import InputError

from .conftest import DATA, LANDING, SAMPLE, SAMPLE_KEY, FakeContext

DIRTY = SAMPLE.with_name("dirty.csv")


def _validate(key: str, ctx: FakeContext, **extra: Any) -> dict[str, Any]:
    return validate_input.handler({"bucket": LANDING, "key": key, **extra}, ctx)


# --------------------------------------------------------------------------- duplicates


def test_batch_id_is_deterministic_for_an_object_version() -> None:
    when = datetime(2026, 9, 26, 4, 5, 6, tzinfo=UTC)
    first = common.batch_id_for_object("b", "incoming/x.csv", "version-1", when)
    assert first == common.batch_id_for_object("b", "incoming/x.csv", "version-1", when)
    assert first.startswith("20260926T040506-") and len(first) == len("20260926T040506-") + 8
    assert first != common.batch_id_for_object("b", "incoming/x.csv", "version-2", when)
    assert first != common.batch_id_for_object("b", "incoming/y.csv", "version-1", when)


def test_duplicate_trigger_is_recognised_and_does_no_work(
    aws: dict[str, Any], lambda_context: FakeContext
) -> None:
    first = _validate(SAMPLE_KEY, lambda_context, execution_id="exec-1")
    second = _validate(SAMPLE_KEY, lambda_context, execution_id="exec-2")

    assert first["duplicate"] is False and first["owner_execution"] == ""
    assert first["row_count"] == 5
    assert second["duplicate"] is True
    assert second["owner_execution"] == "exec-1"
    assert second["batch_id"] == first["batch_id"]
    assert second["rows"] == [] and second["row_count"] == 0

    claim = aws["table"].get_item(Key={"pk": f"batch#{first['batch_id']}"})["Item"]
    assert claim["execution_id"] == "exec-1"
    assert claim["key"] == SAMPLE_KEY and claim["bucket"] == LANDING
    assert claim["ttl"] > 0

    # A different upload (another key) is another batch, however similar the content.
    aws["s3"].put_object(Bucket=LANDING, Key="incoming/other/names.csv", Body=SAMPLE.read_bytes())
    other = _validate("incoming/other/names.csv", lambda_context, execution_id="exec-3")
    assert other["duplicate"] is False
    assert other["batch_id"] != first["batch_id"]


# --------------------------------------------------------------------------- batch cap


def test_batch_credit_cap_defers_rows_across_invocations(
    aws: dict[str, Any], lambda_context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_CREDITS_PER_BATCH", "1")
    event = {"batch_id": "cap", "batch_date": "2026-10-01"}
    first = enrich.handler(
        {**event, "row": {"row_number": 1, "first_name": "John", "last_name": "Doe"}},
        lambda_context,
    )
    second = enrich.handler(
        {**event, "row": {"row_number": 2, "first_name": "Jane", "last_name": "Smith"}},
        lambda_context,
    )
    assert (first["status"], first["credits_consumed"]) == ("matched", 1)
    assert second["status"] == "budget_deferred"
    stored = json.loads(
        aws["s3"].get_object(Bucket=DATA, Key=common.result_key("cap", 2))["Body"].read()
    )
    assert stored["error_message"] == "credit budget: batch cap (1) reached"

    # The cap is per batch: another batch spends normally, and the counters are separate.
    other = enrich.handler(
        {
            "batch_id": "cap2",
            "batch_date": "2026-10-01",
            "row": {"row_number": 1, "first_name": "Jane", "last_name": "Smith"},
        },
        lambda_context,
    )
    assert other["status"] == "matched"
    assert int(aws["table"].get_item(Key={"pk": "budget#mock#batch#cap"})["Item"]["spent"]) == 1
    assert int(aws["table"].get_item(Key={"pk": "budget#mock#batch#cap2"})["Item"]["spent"]) == 1
    assert (
        int(aws["table"].get_item(Key={"pk": "budget#mock#2026-10#identify"})["Item"]["spent"]) == 2
    )


# --------------------------------------------------------------------------- quarantine


def test_rejected_upload_is_quarantined_with_its_reason(
    aws: dict[str, Any], lambda_context: FakeContext
) -> None:
    s3 = aws["s3"]
    junk = b"first_name,last_name\n" + b"test,test\n" * 6 + b"John,Doe\n" * 2
    s3.put_object(Bucket=LANDING, Key="incoming/junk/junk.csv", Body=junk)

    with pytest.raises(InputError, match=rf"file kept at s3://{DATA}/quarantine/files/"):
        _validate("incoming/junk/junk.csv", lambda_context)

    keys = [
        o["Key"] for o in s3.list_objects_v2(Bucket=DATA, Prefix="quarantine/files/")["Contents"]
    ]
    assert len(keys) == 1 and keys[0].endswith("-junk.csv")
    head = s3.head_object(Bucket=DATA, Key=keys[0])
    assert head["Metadata"]["reason"].startswith("6 of 8 rows rejected (75%)")
    assert head["Metadata"]["source"] == f"s3://{LANDING}/incoming/junk/junk.csv"
    assert s3.get_object(Bucket=DATA, Key=keys[0])["Body"].read() == junk

    # The claim outlives a deterministic rejection: a duplicate delivery of the same junk
    # file is ignored instead of being quarantined and emailed a second time.
    claims = [i for i in aws["table"].scan()["Items"] if i["pk"].startswith("batch#")]
    assert len(claims) == 1
    again = _validate("incoming/junk/junk.csv", lambda_context, execution_id="exec-dup")
    assert again["duplicate"] is True
    assert len(s3.list_objects_v2(Bucket=DATA, Prefix="quarantine/files/")["Contents"]) == 1


def test_claim_is_released_when_the_function_fails_unexpectedly(
    aws: dict[str, Any], lambda_context: FakeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: Any, **kwargs: Any) -> str:
        raise RuntimeError("simulated S3 outage")

    original = validate_input.put_json
    monkeypatch.setattr(validate_input, "put_json", boom)
    with pytest.raises(RuntimeError, match="simulated S3 outage"):
        _validate(SAMPLE_KEY, lambda_context, execution_id="exec-crash")
    assert [i for i in aws["table"].scan()["Items"] if i["pk"].startswith("batch#")] == []

    # The retry (same object version) claims the batch and completes normally.
    monkeypatch.setattr(validate_input, "put_json", original)
    retried = _validate(SAMPLE_KEY, lambda_context, execution_id="exec-retry")
    assert retried["duplicate"] is False and retried["row_count"] == 5
    claim = aws["table"].get_item(Key={"pk": f"batch#{retried['batch_id']}"})["Item"]
    assert claim["execution_id"] == "exec-retry"


def test_rejected_rows_are_exported_for_the_source_owner(
    aws: dict[str, Any], lambda_context: FakeContext
) -> None:
    s3 = aws["s3"]
    s3.put_object(Bucket=LANDING, Key="incoming/dirty/dirty.csv", Body=DIRTY.read_bytes())
    validated = _validate("incoming/dirty/dirty.csv", lambda_context)
    batch_id, batch_date = validated["batch_id"], validated["batch_date"]

    ref = validated["rejected_rows_ref"]
    assert ref == f"s3://{DATA}/quarantine/rows/{batch_id}.csv"
    body = s3.get_object(Bucket=DATA, Key=common.rejected_rows_key(batch_id))["Body"].read()
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8"))))
    assert [int(r["row_number"]) for r in rows] == [5, 6, 7, 8]
    assert rows[1]["reason"] == "first_name: contains digits; last_name: contains digits"
    assert (rows[1]["first_name"], rows[1]["last_name"]) == ("R2", "D2")

    for row in validated["rows"]:
        enrich.handler({"batch_id": batch_id, "batch_date": batch_date, "row": row}, lambda_context)
    manifest = build_curated.handler(
        {"batch_id": batch_id, "batch_date": batch_date}, lambda_context
    )
    assert manifest["rejected_rows_ref"] == ref

    # A clean file exports nothing and says so.
    clean = _validate(SAMPLE_KEY, lambda_context)
    assert clean["invalid_count"] == 1  # the sample's one invalid row is exported too
    assert clean["rejected_rows_ref"].endswith(f"/quarantine/rows/{clean['batch_id']}.csv")
