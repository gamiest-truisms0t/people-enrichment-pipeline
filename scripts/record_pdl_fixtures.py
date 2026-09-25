"""Record synthetic People Data Labs sandbox responses as test fixtures.

Zero credits, but the sandbox allows 5 calls per minute, so this paces itself and
takes about 90 seconds. Contact-style fields are stripped even though the data is
synthetic. Run with `make record-fixtures`.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from enrich_pipeline.providers.base import ProviderResponse
from enrich_pipeline.providers.pdl import PdlProvider

KEY_FILE = Path.home() / ".config" / "people-enrichment" / "pdl_api_key"
OUT_DIR = Path("tests/fixtures/pdl")
PAUSE_SECONDS = 13

STRIP_KEYS = {
    "emails",
    "phone_numbers",
    "personal_emails",
    "mobile_phone",
    "work_email",
    "recommended_personal_email",
    "street_addresses",
    "profiles",
    "birth_date",
    "birth_year",
    "sex",
}
KEEP_HEADER_PREFIXES = ("x-", "retry-after", "content-type")

# Synthetic people known to exist in the sandbox; see the recorded search sample in
# src/enrich_pipeline/providers/mock_data/sandbox_search_sample.json.
CASES: list[tuple[str, str, dict[str, str]]] = [
    # The sandbox resolves strong identifiers (profile URL, pdl_id, email) but answers 404
    # to name-based lookups, so the 200 fixtures use a synthetic LinkedIn URL.
    (
        "enrich_200",
        "enrich",
        {"profile": "linkedin.com/in/aa73", "min_likelihood": "6"},
    ),
    (
        "enrich_404",
        "enrich",
        {
            "first_name": "nobody",
            "last_name": "nowhere",
            "company": "acme corp",
            "min_likelihood": "6",
        },
    ),
    ("enrich_400", "enrich", {"first_name": "ashley", "last_name": "armstrong"}),
    ("identify_200", "identify", {"profile": "linkedin.com/in/aa73"}),
    ("identify_404", "identify", {"first_name": "zqxjv", "last_name": "wkplmt"}),
]


def scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items() if k not in STRIP_KEYS}
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def record(name: str, kind: str, params: dict[str, str], response: ProviderResponse) -> None:
    headers = {
        k.lower(): v
        for k, v in response.headers.items()
        if k.lower().startswith(KEEP_HEADER_PREFIXES)
    }
    document = {
        "request": {"kind": kind, "params": params, "sandbox": True},
        "status": response.status,
        "headers": headers,
        "body": scrub(response.body),
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{name}.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(
        f"{name}: HTTP {response.status}, credits spent {response.credits_spent}, "
        f"remaining {response.total_credits_remaining}, reset {response.rate_limit_reset_seconds}"
    )


def main() -> int:
    key = os.environ.get("PDL_API_KEY") or KEY_FILE.read_text(encoding="utf-8").strip()
    provider = PdlProvider(key, sandbox=True)
    for index, (name, kind, params) in enumerate(CASES):
        if index:
            time.sleep(PAUSE_SECONDS)
        response = provider.enrich(params) if kind == "enrich" else provider.identify(params)
        record(name, kind, params, response)
    provider.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
