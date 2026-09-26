#!/usr/bin/env bash
# Quarantine helpers for uploads the pipeline could not process as a whole and for the
# rejected-row exports of batches that did run.
#   list               quarantined uploads with their rejection reason, and rejected-row exports
#   get KEY [DEST]     download one quarantined object (default DEST: ./)
#   redrive KEY        copy a quarantined upload back under incoming/redrive/<timestamp>/,
#                      which starts a new execution (a new object version, so never a duplicate)
# Usage: scripts/quarantine.sh list|get|redrive ...   (AWS_PROFILE/AWS_REGION from the Makefile)
set -euo pipefail

TF="terraform -chdir=infra/envs/${ENV:-dev}"
DATA="$($TF output -raw data_bucket)"

cmd="${1:-list}"
case "$cmd" in
  list)
    echo "==> quarantined uploads (s3://$DATA/quarantine/files/)"
    aws s3api list-objects-v2 --bucket "$DATA" --prefix quarantine/files/ \
      --query 'Contents[].[Key, Size, LastModified]' --output text 2>/dev/null \
      | while IFS=$'\t' read -r key size when; do
          [ -n "$key" ] || continue
          reason="$(aws s3api head-object --bucket "$DATA" --key "$key" --query 'Metadata.reason' --output text)"
          source="$(aws s3api head-object --bucket "$DATA" --key "$key" --query 'Metadata.source' --output text)"
          printf '    %s  (%s bytes, %s)\n      from   %s\n      reason %s\n' "$key" "$size" "$when" "$source" "$reason"
        done
    echo "==> rejected-row exports (s3://$DATA/quarantine/rows/)"
    aws s3 ls "s3://$DATA/quarantine/rows/" 2>/dev/null | awk '{print "    " $3 " bytes  " $4}' || true
    ;;
  get)
    key="${2:?usage: quarantine.sh get KEY [DEST]}"
    dest="${3:-.}"
    aws s3 cp "s3://$DATA/$key" "$dest/" --only-show-errors
    echo "downloaded $key to $dest/$(basename "$key")"
    ;;
  redrive)
    key="${2:?usage: quarantine.sh redrive KEY}"
    LANDING="$($TF output -raw landing_bucket)"
    name="$(basename "$key")"
    name="${name#*-}"  # drop the quarantine timestamp prefix
    target="incoming/redrive/$(date -u +%Y%m%dT%H%M%SZ)/$name"
    aws s3 cp "s3://$DATA/$key" "s3://$LANDING/$target" --only-show-errors
    echo "redriven as s3://$LANDING/$target (a new execution starts; follow it with 'make executions')"
    ;;
  *)
    echo "usage: quarantine.sh list|get KEY [DEST]|redrive KEY" >&2
    exit 2
    ;;
esac
