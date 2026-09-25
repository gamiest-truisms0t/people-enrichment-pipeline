"""Raw layer: every provider response is persisted before interpretation.

Phase 2 adds an S3 implementation with the same key layout:
    raw/provider=<name>/batch_date=<YYYY-MM-DD>/batch_id=<id>/<lookup_key>.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Protocol


class RawStore(Protocol):
    def write(self, *, lookup_key: str, record: dict[str, Any]) -> str:
        """Persist one raw record and return a reference (path or S3 key)."""
        ...


def raw_key(provider: str, batch_date: str, batch_id: str, lookup_key: str) -> str:
    return f"raw/provider={provider}/batch_date={batch_date}/batch_id={batch_id}/{lookup_key}.json"


class LocalRawStore:
    def __init__(self, base_dir: Path, *, provider: str, batch_date: str, batch_id: str) -> None:
        self.base_dir = Path(base_dir)
        self.provider = provider
        self.batch_date = batch_date
        self.batch_id = batch_id

    def write(self, *, lookup_key: str, record: dict[str, Any]) -> str:
        key = raw_key(self.provider, self.batch_date, self.batch_id, lookup_key)
        path = self.base_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2, sort_keys=True, default=str), encoding="utf-8")
        return key


class S3RawStore:
    """Same layout as LocalRawStore, one object per lookup, in the data bucket."""

    def __init__(
        self, bucket: str, *, provider: str, batch_date: str, batch_id: str, client: Any
    ) -> None:
        self.bucket = bucket
        self.provider = provider
        self.batch_date = batch_date
        self.batch_id = batch_id
        self.client = client

    def write(self, *, lookup_key: str, record: dict[str, Any]) -> str:
        key = raw_key(self.provider, self.batch_date, self.batch_id, lookup_key)
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=json.dumps(record, sort_keys=True, default=str).encode("utf-8"),
            ContentType="application/json",
        )
        return f"s3://{self.bucket}/{key}"


class NullRawStore:
    def write(self, *, lookup_key: str, record: dict[str, Any]) -> str:
        return ""
