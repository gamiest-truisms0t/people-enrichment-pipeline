"""Per-row report for one batch straight from S3.

Reads the manifest and the fact_lookup Parquet, prints each row's outcome, and pulls the
provider's credit headers out of the raw layer so spend can be reconciled against the
provider's own remaining-credit counter. Run with `make report BATCH=<batch_id>`.
"""

from __future__ import annotations

import argparse
import io
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import boto3
import pyarrow.parquet as pq

from enrich_pipeline.aws.s3 import get_json, list_keys


def _fmt(value: Any, width: int) -> str:
    text = "" if value is None else str(value)
    return text[:width].ljust(width)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True, help="data bucket name")
    parser.add_argument("--batch", required=True, help="batch id, e.g. 20260925T131039-c4d542")
    args = parser.parse_args(argv)

    s3 = boto3.client("s3")
    manifest = get_json(s3, args.bucket, f"manifests/{args.batch}.json")
    batch_date, provider = manifest["batch_date"], manifest["provider"]

    lookup_key = f"curated/fact_lookup/batch_date={batch_date}/{args.batch}.parquet"
    body = s3.get_object(Bucket=args.bucket, Key=lookup_key)["Body"].read()
    rows = sorted(pq.read_table(io.BytesIO(body)).to_pylist(), key=lambda r: r["row_number"])

    print(f"batch {args.batch} ({batch_date}) provider={provider} source={manifest.get('source')}")
    print(
        f"{'row':>3} {_fmt('name', 24)} {_fmt('status', 16)} {_fmt('method', 13)} "
        f"{'score':>5} {'http':>4} {'cred':>4} {'att':>3}  note"
    )
    for r in rows:
        name = f"{r['input_first_name'] or ''} {r['input_last_name'] or ''}".strip()
        if r.get("input_company"):
            name = f"{name} @ {r['input_company']}"
        score = "" if r["likelihood"] is None else f"{r['likelihood']:g}"
        note = ""
        if r["status"] == "ambiguous":
            note = f"{r['candidates']} candidates"
        elif r["status"] in ("error", "invalid_input", "budget_deferred"):
            note = (r["error_message"] or "")[:60]
        print(
            f"{r['row_number']:>3} {_fmt(name, 24)} {_fmt(r['status'], 16)} "
            f"{_fmt(r['lookup_method'] or '-', 13)} {score:>5} {_fmt(r['http_status'], 4)} "
            f"{r['credits_consumed']:>4} {r['attempts']:>3}  {note}"
        )

    print(f"\nstatus counts: {manifest['status_counts']}")
    print(f"persons: {manifest['persons']}, employment rows: {manifest['employment_rows']}")
    print(
        f"credits consumed (from x-call-credits-spent): {sum(r['credits_consumed'] for r in rows)}"
    )

    # Credit headers are per product (PDL bills enrich and identify from separate pools),
    # so remaining credits are reported per x-call-credits-type.
    raw_prefix = f"raw/provider={provider}/batch_date={batch_date}/batch_id={args.batch}/"
    remaining: dict[str, int] = {}
    calls: dict[str, int] = {}
    keys = list(list_keys(s3, args.bucket, raw_prefix))
    with ThreadPoolExecutor(max_workers=16) as pool:
        records = list(pool.map(lambda key: get_json(s3, args.bucket, key), keys))
    for record in records:
        headers = {k.lower(): v for k, v in record["response"]["headers"].items()}
        credit_type = headers.get("x-call-credits-type") or record.get("kind") or "unknown"
        calls[credit_type] = calls.get(credit_type, 0) + 1
        value = headers.get("x-totallimit-remaining", "")
        if value.isdigit():
            remaining[credit_type] = min(int(value), remaining.get(credit_type, int(value)))
    print(f"provider calls recorded by credit type: {calls}")
    if remaining:
        for credit_type, left in sorted(remaining.items()):
            print(f"provider credits remaining for {credit_type}: {left}")
    else:
        print("provider did not report x-totallimit-remaining (expected for mock/sandbox)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
