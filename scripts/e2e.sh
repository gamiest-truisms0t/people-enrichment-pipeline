#!/usr/bin/env bash
# End-to-end: upload a CSV to the landing bucket and follow the Step Functions
# execution that the upload triggers through EventBridge.
# Usage: scripts/e2e.sh [path/to/names.csv]   (AWS_PROFILE/AWS_REGION from the Makefile)
set -euo pipefail

INPUT="${1:-data/sample/names.csv}"
TF="terraform -chdir=infra/envs/${ENV:-dev}"
LANDING="$($TF output -raw landing_bucket)"
DATA="$($TF output -raw data_bucket)"
STATE_MACHINE="$($TF output -raw state_machine_arn)"

latest_execution() {
  aws stepfunctions list-executions --state-machine-arn "$STATE_MACHINE" \
    --max-results 1 --no-paginate --query 'executions[0].executionArn' --output text
}

before="$(latest_execution)"
key="incoming/$(date -u +%Y%m%dT%H%M%SZ)/$(basename "$INPUT")"
echo "==> upload s3://$LANDING/$key"
aws s3 cp "$INPUT" "s3://$LANDING/$key" --only-show-errors

echo "==> waiting for EventBridge to start an execution"
execution=""
for _ in $(seq 1 30); do
  candidate="$(latest_execution)"
  if [ -n "$candidate" ] && [ "$candidate" != "None" ] && [ "$candidate" != "$before" ]; then
    input="$(aws stepfunctions describe-execution --execution-arn "$candidate" --query input --output text)"
    if grep -q "\"$key\"" <<<"$input"; then
      execution="$candidate"
      break
    fi
  fi
  sleep 2
done
if [ -z "$execution" ]; then
  echo "!! no execution started within 60 s. Is the EventBridge rule enabled?" >&2
  exit 1
fi
echo "    $execution"

echo "==> following the execution"
while :; do
  status="$(aws stepfunctions describe-execution --execution-arn "$execution" --query status --output text)"
  [ "$status" = "RUNNING" ] || break
  sleep 3
done
echo "    status: $status"

echo "==> states visited"
aws stepfunctions get-execution-history --execution-arn "$execution" --no-include-execution-data \
  --query 'events[?stateEnteredEventDetails].stateEnteredEventDetails.name' --output text \
  | tr '\t' '\n' | sort | uniq -c | awk '{printf "    %-22s x%s\n", $2, $1}'

if [ "$status" = "SUCCEEDED" ]; then
  echo "==> output"
  output="$(aws stepfunctions describe-execution --execution-arn "$execution" --query output --output text)"
  jq . <<<"$output"
  if [ -n "${E2E_OUTPUT_JSON:-}" ]; then
    printf '%s\n' "$output" > "$E2E_OUTPUT_JSON"
  fi
  batch_id="$(jq -r .batch_id <<<"$output")"
  batch_date="$(jq -r .batch_date <<<"$output")"
  echo "==> curated objects for batch $batch_id"
  aws s3 ls "s3://$DATA/curated/" --recursive | grep "$batch_id" | awk '{print "    " $3 " bytes  " $4}'

  # Every input row, valid or not, must be visible in Athena the moment the batch lands
  # (partition projection, no crawler). One tiny query, billed at the 10 MB minimum.
  echo "==> Athena: fact_lookup rows for the batch"
  database="$($TF output -raw glue_database)"
  workgroup="$($TF output -raw athena_workgroup)"
  expected="$(jq '.row_count + .invalid_count' <<<"$output")"
  query_id="$(aws athena start-query-execution --work-group "$workgroup" \
    --query-string "SELECT count(*) FROM \"$database\".fact_lookup WHERE batch_date = '$batch_date' AND batch_id = '$batch_id'" \
    --query QueryExecutionId --output text)"
  query_state="QUEUED"
  for _ in $(seq 1 30); do
    query_state="$(aws athena get-query-execution --query-execution-id "$query_id" \
      --query QueryExecution.Status.State --output text)"
    case "$query_state" in SUCCEEDED|FAILED|CANCELLED) break ;; esac
    sleep 2
  done
  if [ "$query_state" != "SUCCEEDED" ]; then
    echo "!! Athena query $query_id ended $query_state" >&2
    aws athena get-query-execution --query-execution-id "$query_id" \
      --query QueryExecution.Status.StateChangeReason --output text >&2
    exit 1
  fi
  actual="$(aws athena get-query-results --query-execution-id "$query_id" \
    --query 'ResultSet.Rows[1].Data[0].VarCharValue' --output text)"
  echo "    $actual rows (expected $expected: $(jq -r '.row_count' <<<"$output") valid + $(jq -r '.invalid_count' <<<"$output") invalid)"
  if [ "$actual" != "$expected" ]; then
    echo "!! Athena row count mismatch" >&2
    exit 1
  fi
else
  echo "==> error"
  aws stepfunctions describe-execution --execution-arn "$execution" --query '{error: error, cause: cause}' --output json
  exit 1
fi
