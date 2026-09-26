#!/usr/bin/env bash
# Smoke test for the deployed functions: invoke them directly, in the order the state
# machine does, and print each step's result. Bypasses EventBridge and Step Functions on
# purpose, so a broken function is isolated from a broken trigger; `make e2e` covers the
# whole path.
#   upload CSV -> validate-input -> enrich (per row) -> build-curated
# Usage: scripts/smoke.sh [path/to/names.csv]   (AWS_PROFILE/AWS_REGION from the Makefile)
set -euo pipefail

INPUT="${1:-data/sample/names.csv}"
TF="terraform -chdir=infra/envs/${ENV:-dev}"
LANDING="$($TF output -raw landing_bucket)"
DATA="$($TF output -raw data_bucket)"
FN_VALIDATE="$($TF output -json function_names | jq -r .validate_input)"
FN_ENRICH="$($TF output -json function_names | jq -r .enrich)"
FN_BUILD="$($TF output -json function_names | jq -r .build_curated)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

invoke() { # invoke <function> <payload-json> -> prints response JSON, fails on FunctionError
  local fn="$1" payload="$2" out="$work/$RANDOM.json" status
  status="$(aws lambda invoke --function-name "$fn" --cli-binary-format raw-in-base64-out \
    --payload "$payload" --query 'FunctionError' --output text "$out")"
  if [ "$status" != "None" ]; then
    echo "!! $fn failed ($status):" >&2
    cat "$out" >&2
    exit 1
  fi
  cat "$out"
}

key="incoming/$(date -u +%Y%m%dT%H%M%SZ)/$(basename "$INPUT")"
echo "==> upload s3://$LANDING/$key"
aws s3 cp "$INPUT" "s3://$LANDING/$key" --only-show-errors

echo "==> validate-input"
validated="$(invoke "$FN_VALIDATE" "$(jq -nc --arg b "$LANDING" --arg k "$key" '{bucket:$b,key:$k}')")"
batch_id="$(jq -r .batch_id <<<"$validated")"
batch_date="$(jq -r .batch_date <<<"$validated")"
echo "    batch $batch_id ($batch_date): $(jq -r '"\(.row_count) valid, \(.invalid_count) invalid"' <<<"$validated")"

echo "==> enrich (one invocation per row)"
jq -c '.rows[]' <<<"$validated" | while read -r row; do
  payload="$(jq -nc --arg b "$batch_id" --arg d "$batch_date" --argjson r "$row" '{batch_id:$b,batch_date:$d,row:$r}')"
  invoke "$FN_ENRICH" "$payload" | jq -r '"    row \(.row_number): \(.status) (\(.method // "-"), credits \(.credits_consumed), attempts \(.attempts))"'
done

echo "==> build-curated"
manifest="$(invoke "$FN_BUILD" "$(jq -nc --arg b "$batch_id" --arg d "$batch_date" '{batch_id:$b,batch_date:$d}')")"
jq '{persons, employment_rows, credits_spent, status_counts}' <<<"$manifest"

echo "==> curated objects"
aws s3 ls "s3://$DATA/curated/" --recursive | grep "$batch_id" | awk '{print "    " $3 " bytes  " $4}'
echo "==> manifest: $(jq -r .manifest_ref <<<"$manifest")"
