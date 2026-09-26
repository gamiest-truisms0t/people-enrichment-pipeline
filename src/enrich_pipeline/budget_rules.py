"""The two credit-budget rules, shared by the in-memory and the DynamoDB budgets.

Kept free of imports so both budget implementations (and their tests) can use them
without a dependency cycle.
"""

from __future__ import annotations


def monthly_ceiling_reason(kind: str, spent: int, limit: int | None) -> str | None:
    """Why a call of `kind` is blocked by its monthly ceiling, or None."""
    if limit is not None and spent >= limit:
        return f"{kind} credit budget: monthly ceiling ({limit}) reached"
    return None


def batch_cap_reason(spent: int, cap: int | None) -> str | None:
    """Why any further call is blocked by the per-batch cap, or None."""
    if cap is not None and spent >= cap:
        return f"credit budget: batch cap ({cap}) reached"
    return None
