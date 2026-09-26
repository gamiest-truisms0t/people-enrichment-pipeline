"""The matching ladder plus everything that protects the credit budget.

    email -> LinkedIn profile -> name + company/location -> name only

Strong identifiers use the provider's enrich call (billed only on a match).
Name-only rows use identify (billed on every call), so it has a separate budget
and a confidence gate. Retries handle 429/5xx; a 402 marks that call kind's budget
exhausted so the rest of the batch is deferred rather than failed. Providers bill
enrich and identify from separate pools, which is why exhaustion is tracked per kind.

Cache, budget and raw store are protocols: in-memory/local for the CLI and
tests, DynamoDB/S3 in Lambda (see `enrich_pipeline.aws`).
"""

from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from enrich_pipeline.breaker import Breaker, MemoryBreaker, unavailable_message
from enrich_pipeline.budget_rules import batch_cap_reason, monthly_ceiling_reason
from enrich_pipeline.guards import match_flags
from enrich_pipeline.models import (
    InputRow,
    LookupMethod,
    LookupResult,
    LookupStatus,
    PersonProfile,
)
from enrich_pipeline.normalize import lookup_key
from enrich_pipeline.providers.base import (
    Provider,
    ProviderError,
    ProviderResponse,
    ResponseKind,
)
from enrich_pipeline.raw_store import NullRawStore, RawStore

CACHEABLE = frozenset({LookupStatus.MATCHED, LookupStatus.NOT_FOUND, LookupStatus.AMBIGUOUS})


@dataclass(frozen=True)
class EnrichConfig:
    identify_min_score: int = 70
    identify_min_margin: int = 20
    # PDL scores correct name + company matches of well-known people around 4 on its 1-10
    # scale (measured 2026-09-25); 6 discarded most true matches. The score is kept on every
    # person row so analysts can filter more strictly downstream.
    enrich_min_likelihood: int = 4
    max_attempts: int = 4
    max_wait_seconds: float = 60.0
    max_enrich_credits: int | None = None
    max_identify_credits: int | None = None
    location_hint: str | None = None
    # Credits one batch may spend in total, so a single oversized upload cannot burn the
    # month's pool. None = only the monthly ceilings apply.
    max_credits_per_batch: int | None = None
    # Circuit breaker: consecutive 5xx/transport failures that open it, and for how long.
    breaker_threshold: int = 3
    breaker_cooldown_seconds: int = 300


class Budget(Protocol):
    """Credit ceilings per billable call kind plus a per-kind provider-exhausted marker."""

    def allows(self, kind: str) -> bool: ...

    def blocked_reason(self, kind: str) -> str | None:
        """Why a billable call of this kind may not be made now, or None if it may."""
        ...

    def record(self, kind: str, credits: int) -> None: ...

    def used(self, kind: str) -> int: ...

    def mark_exhausted(self, kind: str) -> None: ...

    def is_exhausted(self, kind: str) -> bool: ...

    @property
    def total_spent(self) -> int: ...


class CreditBudget:
    """In-memory budget for a single local run."""

    def __init__(
        self,
        *,
        enrich: int | None = None,
        identify: int | None = None,
        per_batch: int | None = None,
    ) -> None:
        self.limits: dict[str, int | None] = {"enrich": enrich, "identify": identify}
        self.per_batch = per_batch
        self.spent: Counter[str] = Counter()
        self._exhausted: set[str] = set()

    @classmethod
    def from_config(cls, config: EnrichConfig) -> CreditBudget:
        return cls(
            enrich=config.max_enrich_credits,
            identify=config.max_identify_credits,
            per_batch=config.max_credits_per_batch,
        )

    def blocked_reason(self, kind: str) -> str | None:
        return monthly_ceiling_reason(
            kind, self.spent[kind], self.limits.get(kind)
        ) or batch_cap_reason(self.total_spent, self.per_batch)

    def allows(self, kind: str) -> bool:
        return self.blocked_reason(kind) is None

    def record(self, kind: str, credits: int) -> None:
        self.spent[kind] += credits

    def used(self, kind: str) -> int:
        return self.spent[kind]

    def mark_exhausted(self, kind: str) -> None:
        self._exhausted.add(kind)

    def is_exhausted(self, kind: str) -> bool:
        return kind in self._exhausted

    @property
    def total_spent(self) -> int:
        return sum(self.spent.values())


class LookupCache(Protocol):
    def get(self, key: str) -> LookupResult | None: ...

    def put(self, key: str, result: LookupResult) -> None: ...


class InMemoryCache:
    def __init__(self) -> None:
        self._items: dict[str, LookupResult] = {}

    def get(self, key: str) -> LookupResult | None:
        return self._items.get(key)

    def put(self, key: str, result: LookupResult) -> None:
        self._items[key] = result


@dataclass(frozen=True)
class LookupPlan:
    method: LookupMethod
    kind: ResponseKind
    params: dict[str, str]
    key: str


def plan_lookup(row: InputRow, config: EnrichConfig) -> LookupPlan:
    """Choose the ladder rung, the provider parameters and the cache key for one row.

    A module-level function so the curated step can reproduce the exact key and method
    of a row whose enrich invocation never recorded a result.
    """
    location = row.location or config.location_hint
    if row.email:
        method, kind = LookupMethod.EMAIL, "enrich"
        params = {"email": row.email}
    elif row.linkedin_url:
        method, kind = LookupMethod.LINKEDIN, "enrich"
        params = {"profile": row.linkedin_url}
    elif row.company or location:
        method, kind = LookupMethod.NAME_CONTEXT, "enrich"
        params = {"first_name": row.first_name, "last_name": row.last_name}
        if row.company:
            params["company"] = row.company
        if location:
            params["location"] = location
    else:
        method, kind = LookupMethod.NAME_ONLY, "identify"
        params = {"first_name": row.first_name, "last_name": row.last_name}

    if kind == "enrich":
        params["min_likelihood"] = str(config.enrich_min_likelihood)

    # Anything that changes how an answer is produced or judged is part of the key, so
    # tuning a threshold re-queries instead of replaying a cached outcome: the enrich
    # likelihood is applied by the provider, the identify gate by us.
    if kind == "enrich":
        extra = f"min_likelihood={params['min_likelihood']}"
    else:
        extra = (
            f"identify_min_score={config.identify_min_score}"
            f";identify_min_margin={config.identify_min_margin}"
        )
    key = lookup_key(
        row.first_name,
        row.last_name,
        email=row.email,
        company=row.company,
        location=location if method is LookupMethod.NAME_CONTEXT else None,
        linkedin_url=row.linkedin_url,
        extra=extra,
    )
    return LookupPlan(method=method, kind=kind, params=params, key=key)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Enricher:
    def __init__(
        self,
        provider: Provider,
        *,
        config: EnrichConfig | None = None,
        cache: LookupCache | None = None,
        budget: Budget | None = None,
        raw_store: RawStore | None = None,
        breaker: Breaker | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        self.provider = provider
        self.config = config or EnrichConfig()
        self.cache = cache or InMemoryCache()
        self.budget = budget or CreditBudget.from_config(self.config)
        self.raw_store = raw_store or NullRawStore()
        self.sleep = sleep
        self.clock = clock
        self.breaker = breaker or MemoryBreaker(
            threshold=self.config.breaker_threshold,
            cooldown_seconds=self.config.breaker_cooldown_seconds,
            clock=clock,
        )

    def provider_exhausted(self, kind: ResponseKind) -> bool:
        return self.budget.is_exhausted(kind)

    # ------------------------------------------------------------------ planning

    def plan(self, row: InputRow) -> LookupPlan:
        return plan_lookup(row, self.config)

    # ------------------------------------------------------------------ lookup

    def lookup(self, row: InputRow) -> LookupResult:
        plan = self.plan(row)
        requested_at = self.clock()
        base: dict[str, Any] = {
            "row": row,
            "lookup_key": plan.key,
            "provider": self.provider.name,
            "method": plan.method,
            "requested_at": requested_at,
        }

        cached = self.cache.get(plan.key)
        if cached is not None:
            return cached.model_copy(
                update={
                    **base,
                    "status": LookupStatus.CACHED,
                    "credits_consumed": 0,
                    "attempts": 0,
                    "http_status": None,
                }
            )

        if self.budget.is_exhausted(plan.kind):
            return LookupResult(
                **base,
                status=LookupStatus.BUDGET_DEFERRED,
                http_status=402,
                error_message=f"provider reported {plan.kind} credits exhausted (HTTP 402)",
                attempts=0,
            )
        if (blocked := self.budget.blocked_reason(plan.kind)) is not None:
            return LookupResult(
                **base,
                status=LookupStatus.BUDGET_DEFERRED,
                error_message=blocked,
                attempts=0,
            )
        if (until := self.breaker.open_until()) is not None:
            return LookupResult(
                **base,
                status=LookupStatus.PROVIDER_UNAVAILABLE,
                error_message=unavailable_message(until),
                attempts=0,
            )

        try:
            response, attempts = self._call(plan.kind, plan.params)
        except ProviderError as exc:
            self.breaker.record_failure()
            return LookupResult(
                **base,
                status=LookupStatus.ERROR,
                error_message=f"transport error: {exc}",
                attempts=self.config.max_attempts,
            )
        if response.status >= 500:
            self.breaker.record_failure()
        else:
            self.breaker.record_success()

        result = self._interpret(base, plan, response, attempts)
        self.budget.record(plan.kind, result.credits_consumed)

        raw_ref = self.raw_store.write(
            lookup_key=plan.key,
            record={
                "lookup_key": plan.key,
                "provider": self.provider.name,
                "method": plan.method.value,
                "kind": plan.kind,
                "request": plan.params,
                "response": {
                    "status": response.status,
                    "headers": response.headers,
                    "body": response.body,
                },
                "attempts": attempts,
                "requested_at": requested_at.isoformat(),
            },
        )
        if raw_ref:
            result = result.model_copy(update={"raw_ref": raw_ref})

        if result.status in CACHEABLE:
            self.cache.put(plan.key, result)
        return result

    # ------------------------------------------------------------------ retries

    def _call(self, kind: ResponseKind, params: dict[str, str]) -> tuple[ProviderResponse, int]:
        attempts = max(1, self.config.max_attempts)
        response: ProviderResponse | None = None
        for attempt in range(1, attempts + 1):
            response = (
                self.provider.enrich(params) if kind == "enrich" else self.provider.identify(params)
            )
            last = attempt == attempts
            if response.status == 429 and not last:
                wait = response.rate_limit_reset_seconds
                if wait is None:
                    wait = 2.0**attempt
                self.sleep(min(wait, self.config.max_wait_seconds))
                continue
            if response.status >= 500 and not last:
                self.sleep(min(2.0**attempt, self.config.max_wait_seconds))
                continue
            return response, attempt
        assert response is not None  # the loop always runs at least once
        return response, attempts

    # ------------------------------------------------------------------ interpretation

    def _interpret(
        self, base: dict[str, Any], plan: LookupPlan, response: ProviderResponse, attempts: int
    ) -> LookupResult:
        common: dict[str, Any] = {
            **base,
            "http_status": response.status,
            "credits_consumed": response.credits_spent,
            "attempts": attempts,
        }
        status = response.status
        if status == 402:
            self.budget.mark_exhausted(plan.kind)
            return LookupResult(
                **common,
                status=LookupStatus.BUDGET_DEFERRED,
                error_message=response.error_message or "payment required",
            )
        if status == 404:
            return LookupResult(**common, status=LookupStatus.NOT_FOUND)
        if status != 200:
            return LookupResult(
                **common,
                status=LookupStatus.ERROR,
                error_message=response.error_message or f"HTTP {status}",
            )

        body = response.body or {}
        if response.kind == "identify":
            return self._interpret_identify(common, body)
        return self._interpret_enrich(common, body)

    def _interpret_enrich(self, common: dict[str, Any], body: dict[str, Any]) -> LookupResult:
        data = body.get("data")
        if not isinstance(data, dict):
            return LookupResult(
                **common, status=LookupStatus.ERROR, error_message="200 response without data"
            )
        try:
            profile = PersonProfile.model_validate(data)
        except ValidationError as exc:
            return LookupResult(
                **common, status=LookupStatus.ERROR, error_message=f"unparseable profile: {exc}"
            )
        likelihood = float(body["likelihood"]) if body.get("likelihood") is not None else None
        return LookupResult(
            **common,
            status=LookupStatus.MATCHED,
            profile=profile,
            likelihood=likelihood,
            candidates=1,
            quality_flags=self._flags(common["row"], profile, likelihood, "enrich"),
        )

    def _flags(
        self, row: InputRow, profile: PersonProfile, likelihood: float | None, kind: ResponseKind
    ) -> list[str]:
        return match_flags(
            input_first_name=row.first_name,
            input_last_name=row.last_name,
            profile=profile,
            likelihood=likelihood,
            # Identify has its own confidence gate; only enrich matches sit on a floor.
            likelihood_floor=self.config.enrich_min_likelihood if kind == "enrich" else None,
        )

    def _interpret_identify(self, common: dict[str, Any], body: dict[str, Any]) -> LookupResult:
        matches = body.get("matches") or []
        matches = sorted(matches, key=lambda m: m.get("match_score") or 0, reverse=True)
        if not matches:
            return LookupResult(**common, status=LookupStatus.NOT_FOUND, candidates=0)
        top_score = float(matches[0].get("match_score") or 0)
        second_score = float(matches[1].get("match_score") or 0) if len(matches) > 1 else 0.0
        confident = (
            top_score >= self.config.identify_min_score
            and top_score - second_score >= self.config.identify_min_margin
        )
        if not confident:
            return LookupResult(
                **common,
                status=LookupStatus.AMBIGUOUS,
                likelihood=top_score,
                candidates=len(matches),
            )
        try:
            profile = PersonProfile.model_validate(matches[0].get("data") or {})
        except ValidationError as exc:
            return LookupResult(
                **common, status=LookupStatus.ERROR, error_message=f"unparseable profile: {exc}"
            )
        return LookupResult(
            **common,
            status=LookupStatus.MATCHED,
            profile=profile,
            likelihood=top_score,
            candidates=len(matches),
            quality_flags=self._flags(common["row"], profile, top_score, "identify"),
        )
