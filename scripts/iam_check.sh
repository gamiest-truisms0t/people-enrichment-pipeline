#!/usr/bin/env bash
# Least-privilege pass over every IAM role this project creates: list each inline policy's
# statements that use a wildcard resource, and run IAM Access Analyzer's policy validation
# (free) over the policy document. Exits non-zero on ERROR or SECURITY_WARNING findings.
# Usage: scripts/iam_check.sh [role-name-prefix]   (the Makefile passes people-enrichment-$(ENV))
set -euo pipefail

PREFIX="${1:-people-enrichment-dev}"
status=0

roles="$(aws iam list-roles --query "Roles[?starts_with(RoleName, '$PREFIX')].RoleName" --output text)"
[ -n "$roles" ] || { echo "no roles found with prefix $PREFIX" >&2; exit 1; }

for role in $roles; do
  for policy in $(aws iam list-role-policies --role-name "$role" --query 'PolicyNames[]' --output text); do
    document="$(aws iam get-role-policy --role-name "$role" --policy-name "$policy" --query PolicyDocument --output json)"
    wildcards="$(jq -r '
      [.Statement[]
        | select(((.Resource // []) | if type == "array" then . else [.] end) | index("*"))
        | (.Sid // "(no sid)") + " [" + ((.Action | if type == "array" then . else [.] end) | join(",")) + "]"]
      | join("; ")' <<<"$document")"
    printf '%-46s %-10s wildcard-resource statements: %s\n' "$role" "$policy" "${wildcards:-none}"
    findings="$(aws accessanalyzer validate-policy --policy-type IDENTITY_POLICY --policy-document "$document" \
      --query 'findings[?findingType==`ERROR` || findingType==`SECURITY_WARNING`].[findingType, issueCode, findingDetails]' \
      --output text)"
    if [ -n "$findings" ]; then
      echo "$findings" | sed 's/^/    !! /'
      status=1
    fi
  done
done

if [ "$status" -eq 0 ]; then
  echo "PASS: no ERROR or SECURITY_WARNING findings from IAM Access Analyzer"
fi
exit "$status"
