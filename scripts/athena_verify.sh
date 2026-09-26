#!/usr/bin/env bash
# Run the saved Athena queries (the brief's three questions plus the operational view)
# in the pipeline's workgroup and print the first rows of each result.
# Usage: scripts/athena_verify.sh [max_rows]   (AWS_PROFILE/AWS_REGION from the Makefile)
set -euo pipefail

MAX_ROWS="${1:-12}"
TF="terraform -chdir=infra/envs/${ENV:-dev}"
WORKGROUP="$($TF output -raw athena_workgroup)"
DATABASE="$($TF output -raw glue_database)"

echo "workgroup: $WORKGROUP   database: $DATABASE"
# One batch call fetches every saved query; sort by name so the questions print in order.
query_ids="$(aws athena list-named-queries --work-group "$WORKGROUP" --query 'NamedQueryIds[]' --output text)"
# shellcheck disable=SC2086
aws athena batch-get-named-query --named-query-ids $query_ids --output json \
  | jq -c '.NamedQueries | sort_by(.Name)[] | {name: .Name, sql: .QueryString}' \
  | while IFS= read -r item; do
  name="$(jq -r .name <<<"$item")"
  sql="$(jq -r .sql <<<"$item")"
  echo
  echo "==> $name"
  execution="$(aws athena start-query-execution --work-group "$WORKGROUP" \
    --query-execution-context "Database=$DATABASE" --query-string "$sql" \
    --query 'QueryExecutionId' --output text)"
  while :; do
    state="$(aws athena get-query-execution --query-execution-id "$execution" --query 'QueryExecution.Status.State' --output text)"
    case "$state" in
      SUCCEEDED) break ;;
      FAILED|CANCELLED)
        aws athena get-query-execution --query-execution-id "$execution" --query 'QueryExecution.Status.StateChangeReason' --output text >&2
        exit 1 ;;
      *) sleep 2 ;;
    esac
  done
  stats="$(aws athena get-query-execution --query-execution-id "$execution" \
    --query 'QueryExecution.Statistics.[DataScannedInBytes,TotalExecutionTimeInMillis]' --output text)"
  echo "    scanned $(echo "$stats" | cut -f1) bytes in $(echo "$stats" | cut -f2) ms"
  # First row is the header. Empty cells print as blanks; values may contain spaces.
  aws athena get-query-results --query-execution-id "$execution" --max-results "$((MAX_ROWS + 1))" \
    --no-paginate --output json \
    | jq -r '.ResultSet.Rows[] | [.Data[] | (.VarCharValue // "")] | join(" | ")' \
    | sed 's/^/    /'
done
