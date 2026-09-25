"""Small S3 helpers shared by the Lambda handlers."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any


def s3_uri(bucket: str, key: str) -> str:
    return f"s3://{bucket}/{key}"


def put_json(client: Any, bucket: str, key: str, payload: Any) -> str:
    body = json.dumps(payload, default=str, sort_keys=True).encode("utf-8")
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType="application/json")
    return s3_uri(bucket, key)


def get_json(client: Any, bucket: str, key: str) -> Any:
    response = client.get_object(Bucket=bucket, Key=key)
    return json.loads(response["Body"].read().decode("utf-8"))


def get_text(client: Any, bucket: str, key: str) -> str:
    response = client.get_object(Bucket=bucket, Key=key)
    return response["Body"].read().decode("utf-8-sig")


def list_keys(client: Any, bucket: str, prefix: str) -> Iterator[str]:
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for item in page.get("Contents", []):
            yield item["Key"]


def upload_file(client: Any, path: Path, bucket: str, key: str) -> str:
    client.upload_file(str(path), bucket, key)
    return s3_uri(bucket, key)
