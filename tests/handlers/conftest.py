"""Shared fixtures: a moto-mocked account with the two buckets and the state table."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boto3
import pytest
from moto import mock_aws

from enrich_pipeline.handlers import common

REGION = "ap-southeast-1"
LANDING = "landing-bucket"
DATA = "data-bucket"
TABLE = "state-table"
SAMPLE = Path(__file__).resolve().parents[2] / "data" / "sample" / "names.csv"
SAMPLE_KEY = "incoming/demo/names.csv"


@dataclass
class FakeContext:
    function_name: str = "test-function"
    memory_limit_in_mb: int = 512
    invoked_function_arn: str = f"arn:aws:lambda:{REGION}:123456789012:function:test-function"
    aws_request_id: str = "00000000-0000-0000-0000-000000000000"


@pytest.fixture
def lambda_context() -> FakeContext:
    return FakeContext()


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[dict[str, Any]]:
    for name, value in {
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "DATA_BUCKET": DATA,
        "LANDING_BUCKET": LANDING,
        "STATE_TABLE": TABLE,
        "PROVIDER": "mock",
        "MAX_ROWS": "500",
        "POWERTOOLS_METRICS_NAMESPACE": "PeopleEnrichment",
        "POWERTOOLS_TRACE_DISABLED": "1",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    with mock_aws():
        common.reset_clients()
        s3 = boto3.client("s3", region_name=REGION)
        for bucket in (LANDING, DATA):
            s3.create_bucket(
                Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": REGION}
            )
        dynamodb = boto3.resource("dynamodb", region_name=REGION)
        table = dynamodb.create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        s3.put_object(Bucket=LANDING, Key=SAMPLE_KEY, Body=SAMPLE.read_bytes())
        yield {"s3": s3, "table": table}
    common.reset_clients()
