#!/usr/bin/env bash
# Rebuild the curated tables of every batch (or those whose id starts with PREFIX) from the
# stored results, without any provider call. This is how a transform change, a new column
# or a new quality rule reaches historical batches; every rewritten row carries the current
# pipeline_version. Batches are processed one at a time; each rebuild is idempotent.
# Usage: scripts/rebuild_all.sh [PREFIX]   (AWS_PROFILE/AWS_REGION from the Makefile)
set -euo pipefail

PREFIX="${1:-}"
TF="terraform -chdir=infra/envs/${ENV:-dev}"
DATA="$($TF output -raw data_bucket)"
FN_BUILD="$($TF output -json function_names | jq -r .build_curated)"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

batches="$(aws s3api list-objects-v2 --bucket "$DATA" --prefix "input/batch_id=$PREFIX" \
  --query 'Contents[].Key' --output text | tr '\t' '\n' | sed -nE 's#^input/batch_id=([^/]+)/input\.json$#\1#p' | sort)"
count="$(printf '%s\n' "$batches" | grep -c . || true)"
echo "==> rebuilding $count batch(es) from s3://$DATA/input/ (prefix '$PREFIX')"

ok=0; failed=0
for batch in $batches; do
  out="$work/$batch.json"
  status="$(aws lambda invoke --function-name "$FN_BUILD" --cli-binary-format raw-in-base64-out \
    --payload "$(jq -nc --arg b "$batch" '{batch_id:$b}')" --query 'FunctionError' --output text "$out")"
  if [ "$status" != "None" ]; then
    failed=$((failed + 1))
    echo "    $batch  FAILED: $(jq -r '.errorMessage // .' "$out" | head -c 200)"
    continue
  fi
  ok=$((ok + 1))
  jq -r --arg b "$batch" '"    \($b)  persons=\(.persons) rows=\(.rows_valid)+\(.rows_invalid) warnings=\(.quality.warning_count) version=\(.pipeline_version)"' "$out"
done
echo "==> done: $ok rebuilt, $failed failed"
[ "$failed" -eq 0 ]
