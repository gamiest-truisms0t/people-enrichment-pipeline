"""DynamoDB-backed lookup cache and credit budget.

One table, one partition key, prefixed items:

    lookup#<lookup_key>                          cached LookupResult (TTL)
    budget#<provider>#<YYYY-MM>#<kind>           credits spent this month per billable call kind
    budget#<provider>#<YYYY-MM>#exhausted#<kind> marker set after the provider returned HTTP 402
                                                 for that kind (PDL bills enrich and identify
                                                 from separate pools)
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

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
    """Monthly credit ceilings shared by every invocation of the enrich function."""

    KINDS: tuple[str, ...] = ("enrich", "identify")

    def __init__(
        self,
        table: Any,
        *,
        provider: str,
        month: str,
        limits: Mapping[str, int | None],
    ) -> None:
        self.table = table
        self.provider = provider
        self.month = month
        self.limits = dict(limits)

    def pk(self, kind: str) -> str:
        return f"budget#{self.provider}#{self.month}#{kind}"

    def spent(self, kind: str) -> int:
        item = self.table.get_item(Key={"pk": self.pk(kind)}, ConsistentRead=True).get("Item")
        return int(item.get("spent", 0)) if item else 0

    def allows(self, kind: str) -> bool:
        limit = self.limits.get(kind)
        return limit is None or self.spent(kind) < limit

    def record(self, kind: str, credits: int) -> None:
        if credits <= 0:
            return
        self.table.update_item(
            Key={"pk": self.pk(kind)},
            UpdateExpression=(
                "ADD spent :c SET provider = :p, #m = :mo, call_kind = :k, updated_at = :t, "
                "#ttl = if_not_exists(#ttl, :ttl)"
            ),
            ExpressionAttributeNames={"#m": "month", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":c": credits,
                ":p": self.provider,
                ":mo": self.month,
                ":k": kind,
                ":t": _now_iso(),
                ":ttl": int(time.time()) + 400 * DAY,
            },
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
