"""Right to erasure: remove one person from everything the pipeline stored.

A deletion request (PDPA, GDPR article 17) has to reach every place a person ends up:

    input/batch_id=<id>/input.json        the parsed row, valid or rejected
    quarantine/rows/<id>.csv              the rejected row, when there was one
    results/batch_id=<id>/row=<n>.json    the lookup result with the provider's profile
    raw/.../<lookup_key>.json             the provider's raw response
    lookup#<lookup_key>                   the cache entry, which would otherwise repopulate
    curated/<table>/.../<id>.parquet      the rows, rebuilt without the person
    manifests/<id>.json                   the batch summary, rewritten by the rebuild

`Eraser.erase` finds every batch holding the subject (by provider person id, by email or
by name), deletes the objects and cache entries, rewrites the input document, invokes the
curated rebuild for each affected batch, and purges every noncurrent object version: the
data bucket is versioned, so a plain delete or overwrite would keep the old bytes. It
records a tombstone `erasure#<request id>` with counts and a hash of the identity, never
the identity itself. The operator's upload in the landing bucket is reported, not touched:
it is their file and it expires on its own.

Run it as a human with the deployer's permissions (`make erase ...`); the pipeline's own
roles cannot delete under raw/, by design.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from enrich_pipeline.aws.dynamo import DynamoCache
from enrich_pipeline.aws.s3 import get_json, list_keys, put_json
from enrich_pipeline.guards import fold_text
from enrich_pipeline.handlers.common import (
    curated_key,
    input_key,
    manifest_key,
    rejected_rows_key,
    results_prefix,
)
from enrich_pipeline.handlers.validate_input import rejected_rows_csv
from enrich_pipeline.models import InvalidRow
from enrich_pipeline.normalize import normalize_email
from enrich_pipeline.schema import TABLES

INPUT_PREFIX = "input/batch_id="
_BATCH_FROM_KEY = re.compile(r"^input/batch_id=([^/]+)/input\.json$")


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Subject:
    """Who to erase: any identifier the pipeline ever stored for the person."""

    person_id: str | None = None
    email: str | None = None
    first_name: str | None = None
    last_name: str | None = None

    def __post_init__(self) -> None:
        if not (self.person_id or self.email or (self.first_name and self.last_name)):
            raise ValueError("a subject needs a person id, an email, or a first and last name")

    @classmethod
    def from_name(cls, full_name: str, **extra: str | None) -> Subject:
        """'First Middle Last' splits on the last space; a single word is not enough."""
        parts = full_name.strip().split()
        if len(parts) < 2:
            raise ValueError("--name needs a first and a last name")
        return cls(first_name=" ".join(parts[:-1]), last_name=parts[-1], **extra)

    def matches_row(self, row: Mapping[str, Any]) -> bool:
        """An input row (valid or the raw dict of a rejected one)."""
        if self.email and normalize_email(row.get("email")) == normalize_email(self.email):
            return True
        if self.first_name and self.last_name:
            return fold_text(row.get("first_name")) == fold_text(self.first_name) and fold_text(
                row.get("last_name")
            ) == fold_text(self.last_name)
        return False

    def matches_result(self, result: Mapping[str, Any]) -> bool:
        """A lookup result: the provider's id, or the input row it came from."""
        profile = result.get("profile") or {}
        if self.person_id and profile.get("id") == self.person_id:
            return True
        return self.matches_row(result.get("row") or {})

    def digest(self) -> str:
        """What the tombstone stores instead of the identity."""
        canonical = "|".join(
            (
                self.person_id or "",
                normalize_email(self.email),
                fold_text(self.first_name),
                fold_text(self.last_name),
            )
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class BatchErasure:
    """What one batch holds about the subject, and what was done about it."""

    batch_id: str
    batch_date: str
    source: str = ""
    rows: list[int] = field(default_factory=list)
    rejected_rows: list[int] = field(default_factory=list)
    lookup_keys: list[str] = field(default_factory=list)
    objects: list[str] = field(default_factory=list)
    rebuilt: bool = False

    @property
    def found(self) -> bool:
        return bool(self.rows or self.rejected_rows)


@dataclass
class ErasureReport:
    request_id: str
    subject_digest: str
    dry_run: bool
    actor: str
    at: str
    batches: list[BatchErasure]
    cache_items_deleted: int = 0
    versions_deleted: int = 0

    @property
    def sources(self) -> list[str]:
        """Operator uploads in the landing bucket that still hold the person."""
        return sorted({batch.source for batch in self.batches if batch.source})

    @property
    def rows_erased(self) -> int:
        return sum(len(batch.rows) + len(batch.rejected_rows) for batch in self.batches)

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "sources": self.sources, "rows_erased": self.rows_erased}


def _uri(source: Mapping[str, Any] | None) -> str:
    if source and source.get("bucket") and source.get("key"):
        return f"s3://{source['bucket']}/{source['key']}"
    return ""


def _key_in_bucket(uri: str | None, bucket: str) -> str | None:
    prefix = f"s3://{bucket}/"
    if uri and uri.startswith(prefix):
        return uri[len(prefix) :]
    return None


class Eraser:
    def __init__(
        self,
        *,
        s3: Any,
        table: Any,
        bucket: str,
        rebuild: Callable[[str], Any],
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.s3 = s3
        self.table = table
        self.bucket = bucket
        self.rebuild = rebuild
        self.clock = clock

    # ------------------------------------------------------------------ discovery

    def batch_ids(self) -> list[str]:
        ids = []
        for key in list_keys(self.s3, self.bucket, INPUT_PREFIX):
            if match := _BATCH_FROM_KEY.match(key):
                ids.append(match.group(1))
        return sorted(ids)

    def find(self, subject: Subject) -> list[BatchErasure]:
        """Every batch that holds the subject, with the rows, keys and objects involved."""
        plans = (self._inspect(batch_id, subject) for batch_id in self.batch_ids())
        return [plan for plan in plans if plan.found]

    def _inspect(self, batch_id: str, subject: Subject) -> BatchErasure:
        doc = get_json(self.s3, self.bucket, input_key(batch_id))
        plan = BatchErasure(
            batch_id=batch_id, batch_date=doc.get("batch_date", ""), source=_uri(doc.get("source"))
        )
        rows = {int(r["row_number"]) for r in doc.get("rows", []) if subject.matches_row(r)}
        rejected = {
            int(i["row_number"])
            for i in doc.get("invalid", [])
            if subject.matches_row(i.get("raw") or {})
        }
        # A result can match by provider id even when the input spelled the name differently.
        for key in sorted(list_keys(self.s3, self.bucket, results_prefix(batch_id))):
            result = get_json(self.s3, self.bucket, key)
            row_number = int((result.get("row") or {}).get("row_number") or 0)
            if row_number in rows or subject.matches_result(result):
                rows.add(row_number)
                plan.objects.append(key)
                # A cached row points at the raw object of the lookup that paid for it,
                # possibly in another batch; two rows can share one, hence the dedupe.
                raw = _key_in_bucket(result.get("raw_ref"), self.bucket)
                if raw and raw not in plan.objects:
                    plan.objects.append(raw)
                lookup_key = result.get("lookup_key")
                if lookup_key and lookup_key not in plan.lookup_keys:
                    plan.lookup_keys.append(lookup_key)
        plan.rows = sorted(rows)
        plan.rejected_rows = sorted(rejected)
        return plan

    # ------------------------------------------------------------------ execution

    def erase(self, subject: Subject, *, dry_run: bool = False, actor: str = "") -> ErasureReport:
        now = self.clock()
        report = ErasureReport(
            request_id=f"{now:%Y%m%dT%H%M%S}-{uuid4().hex[:6]}",
            subject_digest=subject.digest(),
            dry_run=dry_run,
            actor=actor,
            at=now.isoformat(),
            batches=self.find(subject),
        )
        if dry_run:
            return report
        for plan in report.batches:
            self._apply(plan, report)
        self._tombstone(report)
        return report

    def _apply(self, plan: BatchErasure, report: ErasureReport) -> None:
        # 1. Results and raw provider responses: gone, every version.
        for key in list(plan.objects):
            report.versions_deleted += self._delete_all_versions(key)

        # 2. The parsed input without the person; the old versions still held it.
        key = input_key(plan.batch_id)
        doc = get_json(self.s3, self.bucket, key)
        gone, gone_rejected = set(plan.rows), set(plan.rejected_rows)
        doc["rows"] = [r for r in doc.get("rows", []) if int(r["row_number"]) not in gone]
        doc["invalid"] = [
            i for i in doc.get("invalid", []) if int(i["row_number"]) not in gone_rejected
        ]
        doc.setdefault("erasures", []).append(
            {
                "request_id": report.request_id,
                "rows": plan.rows,
                "rejected_rows": plan.rejected_rows,
                "at": report.at,
            }
        )
        put_json(self.s3, self.bucket, key, doc)
        report.versions_deleted += self._purge_noncurrent(key)
        plan.objects.append(key)

        # 3. The rejected-rows export, rewritten or removed.
        if plan.rejected_rows:
            key = rejected_rows_key(plan.batch_id)
            if doc["invalid"]:
                body = rejected_rows_csv([InvalidRow.model_validate(i) for i in doc["invalid"]])
                self.s3.put_object(
                    Bucket=self.bucket, Key=key, Body=body.encode("utf-8"), ContentType="text/csv"
                )
                report.versions_deleted += self._purge_noncurrent(key)
            else:
                report.versions_deleted += self._delete_all_versions(key)
            plan.objects.append(key)

        # 4. The cache, or the next upload would bring the profile back for free. Batches
        #    share keys (a cached row reuses the paying lookup's), so count real deletions.
        for lookup_key in plan.lookup_keys:
            response = self.table.delete_item(
                Key={"pk": DynamoCache.pk(lookup_key)}, ReturnValues="ALL_OLD"
            )
            if response.get("Attributes"):
                report.cache_items_deleted += 1

        # 5. Curated tables and manifest rebuilt from what remains; old versions purged.
        self.rebuild(plan.batch_id)
        plan.rebuilt = True
        for table in TABLES:
            report.versions_deleted += self._purge_noncurrent(
                curated_key(table, plan.batch_date, plan.batch_id)
            )
        report.versions_deleted += self._purge_noncurrent(manifest_key(plan.batch_id))

    def _tombstone(self, report: ErasureReport) -> None:
        self.table.put_item(
            Item={
                "pk": f"erasure#{report.request_id}",
                "kind": "erasure",
                "subject_digest": report.subject_digest,
                "batches": [batch.batch_id for batch in report.batches],
                "rows_erased": report.rows_erased,
                "objects": sum(len(batch.objects) for batch in report.batches),
                "cache_items_deleted": report.cache_items_deleted,
                "versions_deleted": report.versions_deleted,
                "actor": report.actor,
                "at": report.at,
            }
        )

    # ------------------------------------------------------------------ versions

    def _versions(self, key: str) -> list[dict[str, Any]]:
        found: list[dict[str, Any]] = []
        paginator = self.s3.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=key):
            for item in page.get("Versions", []) + page.get("DeleteMarkers", []):
                if item["Key"] == key:
                    found.append(item)
        return found

    def _delete_all_versions(self, key: str) -> int:
        versions = self._versions(key)
        for version in versions:
            self.s3.delete_object(Bucket=self.bucket, Key=key, VersionId=version["VersionId"])
        return len(versions)

    def _purge_noncurrent(self, key: str) -> int:
        """Delete every version but the current object (delete markers included)."""
        deleted = 0
        for version in self._versions(key):
            if version.get("IsLatest") and "ETag" in version:
                continue
            self.s3.delete_object(Bucket=self.bucket, Key=key, VersionId=version["VersionId"])
            deleted += 1
        return deleted
