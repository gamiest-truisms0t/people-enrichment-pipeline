"""DynamoDB-backed lookup cache and credit budget.

One table, one partition key, prefixed items:

    lookup#<lookup_key>                          cached LookupResult (TTL)
    budget#<provider>#<YYYY-MM>#<kind>           credits spent this month per billable call kind
    budget#<provider>#<YYYY-MM>#exhausted#<kind> marker set after the provider returned HTTP 402
                                                 for that kind (PDL bills enrich and identify
                                                 from separate pools)
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from enrich_pipeline.budget_rules import batch_cap_reason, monthly_ceiling_reason
from enrich_pipeline.models import LookupResult

DAY = 86_400


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class DynamoCache:
    """Idempotency cache: a lookup key is paid for at most once per TTL window."""

    def __init__(self, table: Any, *, ttl_days: int = 90) -> None:
        self.table = table
        self.ttl_days = ttl_days

    @staticmethod
    def pk(key: str) -> str:
        return f"lookup#{key}"

    def get(self, key: str) -> LookupResult | None:
        item = self.table.get_item(Key={"pk": self.pk(key)}).get("Item")
        if not item or "result" not in item:
            return None
        return LookupResult.model_validate_json(item["result"])

    def put(self, key: str, result: LookupResult) -> None:
        self.table.put_item(
            Item={
                "pk": self.pk(key),
                "kind": "lookup",
                "status": result.status.value,
                "provider": result.provider,
                "person_id": result.person_id or "",
                "result": result.model_dump_json(),
                "updated_at": _now_iso(),
                "ttl": int(time.time()) + self.ttl_days * DAY,
            }
        )


class DynamoBudget:
    """Credit ceilings shared by every invocation of the enrich function.

    Monthly counters per call kind (`budget#<provider>#<YYYY-MM>#<kind>`) and, when a
    batch id and cap are given, one counter for the batch as a whole
    (`budget#<provider>#batch#<batch_id>`), so a single oversized upload cannot spend
    the month's pool.
    """

    KINDS: tuple[str, ...] = ("enrich", "identify")

    def __init__(
        self,
        table: Any,
        *,
        provider: str,
        month: str,
        limits: Mapping[str, int | None],
        batch_id: str | None = None,
        batch_limit: int | None = None,
    ) -> None:
        self.table = table
        self.provider = provider
        self.month = month
        self.limits = dict(limits)
        self.batch_id = batch_id
        self.batch_limit = batch_limit if batch_id is not None else None
        # Last total this instance observed per kind, from a read or from the counter
        # returned by an update; lets used() skip a second consistent read.
        self._seen: dict[str, int] = {}

    def pk(self, kind: str) -> str:
        return f"budget#{self.provider}#{self.month}#{kind}"

    def batch_pk(self) -> str:
        return f"budget#{self.provider}#batch#{self.batch_id}"

    def _read(self, pk: str) -> int:
        item = self.table.get_item(Key={"pk": pk}, ConsistentRead=True).get("Item")
        return int(item.get("spent", 0)) if item else 0

    def spent(self, kind: str) -> int:
        """Current month-to-date total (one strongly consistent read)."""
        self._seen[kind] = self._read(self.pk(kind))
        return self._seen[kind]

    def batch_spent(self) -> int:
        """Credits this batch has spent so far, across both kinds."""
        return self._read(self.batch_pk()) if self.batch_id is not None else 0

    def used(self, kind: str) -> int:
        """Month-to-date total as last observed by this instance; reads only if never seen."""
        return self._seen[kind] if kind in self._seen else self.spent(kind)

    def blocked_reason(self, kind: str) -> str | None:
        reason = monthly_ceiling_reason(kind, self.spent(kind), self.limits.get(kind))
        if reason is None and self.batch_limit is not None:
            reason = batch_cap_reason(self.batch_spent(), self.batch_limit)
        return reason

    def allows(self, kind: str) -> bool:
        return self.blocked_reason(kind) is None

    def record(self, kind: str, credits: int) -> None:
        if credits <= 0:
            return
        response = self._add(
            self.pk(kind),
            credits,
            {"call_kind": kind, "#m": self.month},
            ttl_days=400,
        )
        self._seen[kind] = int(response["Attributes"]["spent"])
        if self.batch_id is not None:
            self._add(self.batch_pk(), credits, {"batch_id": self.batch_id}, ttl_days=90)

    def _add(self, pk: str, credits: int, labels: Mapping[str, str], *, ttl_days: int) -> Any:
        """Atomic ADD on a counter item, creating it (with labels and a TTL) on first use.

        Label keys are attribute names; a key starting with '#' is a placeholder for a
        reserved word (only `#m` -> month is used). DynamoDB rejects placeholders that are
        declared but unused, so the name map is built from the labels actually present.
        """
        names = {"#ttl": "ttl"}
        sets = ["provider = :p", "updated_at = :t", "#ttl = if_not_exists(#ttl, :ttl)"]
        values: dict[str, Any] = {
            ":c": credits,
            ":p": self.provider,
            ":t": _now_iso(),
            ":ttl": int(time.time()) + ttl_days * DAY,
        }
        for index, (label, value) in enumerate(labels.items()):
            placeholder = f":l{index}"
            if label == "#m":
                names["#m"] = "month"
            sets.append(f"{label} = {placeholder}")
            values[placeholder] = value
        return self.table.update_item(
            Key={"pk": pk},
            UpdateExpression="ADD spent :c SET " + ", ".join(sets),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues="UPDATED_NEW",
        )

    def mark_exhausted(self, kind: str) -> None:
        self.table.put_item(
            Item={
                "pk": self.pk(f"exhausted#{kind}"),
                "kind": "budget",
                "exhausted": True,
                "call_kind": kind,
                "provider": self.provider,
                "month": self.month,
                "updated_at": _now_iso(),
                "ttl": int(time.time()) + 45 * DAY,
            }
        )

    def is_exhausted(self, kind: str) -> bool:
        item = self.table.get_item(
            Key={"pk": self.pk(f"exhausted#{kind}")}, ConsistentRead=True
        ).get("Item")
        return bool(item and item.get("exhausted"))

    @property
    def total_spent(self) -> int:
        return sum(self.spent(kind) for kind in self.KINDS)


class BatchRegistry:
    """One execution per uploaded object version.

    S3 notifications and EventBridge deliver at least once, so the same upload can start
    two executions. The first one to claim `batch#<batch_id>` (a conditional put) owns the
    batch; a later claimant learns who owns it and ends its execution without doing work.
    """

    def __init__(self, table: Any, *, ttl_days: int = 90) -> None:
        self.table = table
        self.ttl_days = ttl_days

    def pk(self, batch_id: str) -> str:
        return f"batch#{batch_id}"

    def claim(self, batch_id: str, *, execution_id: str, source: Mapping[str, Any]) -> str | None:
        """Claim the batch for `execution_id`. Returns None on success, else the owner's id."""
        try:
            self.table.put_item(
                Item={
                    "pk": self.pk(batch_id),
                    "kind": "batch",
                    "batch_id": batch_id,
                    "execution_id": execution_id,
                    **{k: v for k, v in source.items() if v is not None},
                    "claimed_at": _now_iso(),
                    "ttl": int(time.time()) + self.ttl_days * DAY,
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except self.table.meta.client.exceptions.ConditionalCheckFailedException:
            item = self.table.get_item(Key={"pk": self.pk(batch_id)}, ConsistentRead=True).get(
                "Item", {}
            )
            return str(item.get("execution_id") or "unknown")
        return None

    def release(self, batch_id: str, *, execution_id: str) -> None:
        """Give the claim back if `execution_id` holds it (a failed claimant retrying)."""
        # A failed condition means someone else owns it now; nothing to release.
        with contextlib.suppress(self.table.meta.client.exceptions.ConditionalCheckFailedException):
            self.table.delete_item(
                Key={"pk": self.pk(batch_id)},
                ConditionExpression="execution_id = :e",
                ExpressionAttributeValues={":e": execution_id},
            )
