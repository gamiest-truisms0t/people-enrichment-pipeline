#!/usr/bin/env bash
# Idempotency proof: push the same CSV through the deployed pipeline twice. The second
# run must serve every valid row from the DynamoDB cache and spend zero credits.
# Usage: scripts/idempotency_proof.sh [path/to/names.csv]
set -euo pipefail

INPUT="${1:-data/demo/idempotency.csv}"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

run() { # run <label> <json-out>: stop at the first execution that does not succeed
  echo "==> $1"
  if ! E2E_OUTPUT_JSON="$2" scripts/e2e.sh "$INPUT" > "$work/e2e.log" 2>&1; then
    tail -n 20 "$work/e2e.log"
    echo "!! $1 did not succeed; not uploading again" >&2
    exit 1
  fi
  grep -E '^\s+status:|"credits_spent"|"persons"|"status_counts"' "$work/e2e.log" || true
  [ -s "$2" ] || { echo "!! $1 succeeded but wrote no execution output" >&2; exit 1; }
}

run "first run" "$work/first.json"
run "second run, same file" "$work/second.json"

first_credits="$(jq '.curated.credits_spent' "$work/first.json")"
second_credits="$(jq '.curated.credits_spent' "$work/second.json")"
first_persons="$(jq '.curated.persons' "$work/first.json")"
second_persons="$(jq '.curated.persons' "$work/second.json")"
echo
echo "first run:  credits=$first_credits persons=$first_persons statuses=$(jq -c '.curated.status_counts' "$work/first.json")"
echo "second run: credits=$second_credits persons=$second_persons statuses=$(jq -c '.curated.status_counts' "$work/second.json")"

fail=0
[ "$second_credits" -eq 0 ] || { echo "!! second run spent $second_credits credits"; fail=1; }
[ "$second_persons" -eq "$first_persons" ] || { echo "!! second run produced $second_persons persons, first run $first_persons"; fail=1; }
extra="$(jq -r '.curated.status_counts | keys - ["cached", "invalid_input"] | join(",")' "$work/second.json")"
[ -z "$extra" ] || { echo "!! second run had non-cached statuses: $extra"; fail=1; }

if [ "$fail" -eq 0 ]; then
  echo "PASS: the re-upload was served entirely from cache at zero credits with identical output"
fi
exit "$fail"
