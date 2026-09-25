"""Local batch runner: CSV in, raw JSON + curated Parquet + manifest out.

This is the same sequence the Step Functions workflow performs in AWS
(validate -> enrich each row -> build curated tables), run in-process so the
whole pipeline can be exercised and inspected without an AWS account.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from enrich_pipeline.enricher import CreditBudget, EnrichConfig, Enricher, InMemoryCache
from enrich_pipeline.ingest import parse_csv
from enrich_pipeline.parquet import write_tables
from enrich_pipeline.providers.base import Provider
from enrich_pipeline.raw_store import LocalRawStore
from enrich_pipeline.transform import build_tables


@dataclass
class RunSummary:
    batch_id: str
    batch_date: str
    provider: str
    input_file: str
    out_dir: str
    rows_valid: int
    rows_invalid: int
    status_counts: dict[str, int] = field(default_factory=dict)
    credits_spent: int = 0
    persons: int = 0
    employment_rows: int = 0
    files: dict[str, str] = field(default_factory=dict)
    manifest: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True)


def new_batch_id(now: datetime) -> str:
    return f"{now:%Y%m%dT%H%M%S}-{uuid4().hex[:6]}"


def run_batch(
    input_path: Path,
    *,
    provider: Provider,
    out_dir: Path,
    config: EnrichConfig | None = None,
    batch_id: str | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> RunSummary:
    config = config or EnrichConfig()
    started = clock()
    batch_date = started.date().isoformat()
    batch_id = batch_id or new_batch_id(started)
    out_dir = Path(out_dir)

    parsed = parse_csv(input_path)

    raw_store = LocalRawStore(
        out_dir, provider=provider.name, batch_date=batch_date, batch_id=batch_id
    )
    budget = CreditBudget.from_config(config)
    enricher = Enricher(
        provider,
        config=config,
        cache=InMemoryCache(),
        budget=budget,
        raw_store=raw_store,
        clock=clock,
    )
    results = [enricher.lookup(row) for row in parsed.rows]

    tables = build_tables(
        results, batch_id=batch_id, invalid=parsed.invalid, provider=provider.name, at=started
    )
    files = write_tables(tables, out_dir=out_dir, batch_date=batch_date, batch_id=batch_id)

    counts = Counter(r.status.value for r in results)
    if parsed.invalid:
        counts["invalid_input"] += len(parsed.invalid)

    summary = RunSummary(
        batch_id=batch_id,
        batch_date=batch_date,
        provider=provider.name,
        input_file=str(input_path),
        out_dir=str(out_dir),
        rows_valid=len(parsed.rows),
        rows_invalid=len(parsed.invalid),
        status_counts=dict(sorted(counts.items())),
        credits_spent=budget.total_spent,
        persons=len(tables["dim_person"]),
        employment_rows=len(tables["fact_employment"]),
        files={name: str(path) for name, path in files.items()},
    )
    manifest_path = out_dir / "manifests" / f"{batch_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    summary.manifest = str(manifest_path)
    manifest_path.write_text(summary.to_json(), encoding="utf-8")
    return summary
