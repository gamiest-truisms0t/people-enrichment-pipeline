#!/usr/bin/env bash
# Apply the repository's GitHub settings through the API, idempotently.
#
# Branch protection lives in .github/branch-protection.json (make branch-protection). This
# script covers the rest: description and topics, branch clean-up on merge, and the
# security features that are free on a public repository (secret scanning with push
# protection, Dependabot alerts and security updates, private vulnerability reporting,
# CodeQL default setup). Run it once per fork: `make repo-settings`.
set -euo pipefail

repo="${REPO:-$(gh repo view --json nameWithOwner --jq .nameWithOwner)}"
echo "repository: $repo"

step() { printf '  %-42s' "$1"; }
done_ok() { echo "ok"; }
# A step that the API refuses (feature unavailable, already set, plan limits) is reported
# and does not stop the rest; the summary at the end shows the resulting state.
attempt() { if "$@" >/dev/null 2>&1; then done_ok; else echo "not applied (API refused)"; fi; }

step "description, merge and tab settings"
attempt gh api -X PATCH "repos/$repo" \
  -f description='Serverless people-enrichment ETL on AWS: a CSV upload starts Step Functions and Lambda, People Data Labs enriches each person, Parquet lands in S3 for Athena. Terraform end to end, least privilege, runs on free credits.' \
  -F delete_branch_on_merge=true -F has_wiki=false -F has_projects=false \
  -F allow_squash_merge=true -F allow_merge_commit=true

step "topics"
attempt gh api -X PUT "repos/$repo/topics" \
  -f 'names[]=aws' -f 'names[]=terraform' -f 'names[]=serverless' -f 'names[]=step-functions' \
  -f 'names[]=aws-lambda' -f 'names[]=athena' -f 'names[]=data-pipeline' -f 'names[]=etl' \
  -f 'names[]=data-enrichment' -f 'names[]=python'

step "secret scanning + push protection"
attempt gh api -X PATCH "repos/$repo" --input - <<'JSON'
{"security_and_analysis": {"secret_scanning": {"status": "enabled"},
                           "secret_scanning_push_protection": {"status": "enabled"}}}
JSON

step "Dependabot alerts"
attempt gh api -X PUT "repos/$repo/vulnerability-alerts"

step "Dependabot security updates"
attempt gh api -X PUT "repos/$repo/automated-security-fixes"

step "private vulnerability reporting"
attempt gh api -X PUT "repos/$repo/private-vulnerability-reporting"

step "CodeQL default setup (python, actions)"
attempt gh api -X PATCH "repos/$repo/code-scanning/default-setup" --input - <<'JSON'
{"state": "configured", "query_suite": "default", "languages": ["python", "actions"]}
JSON

echo
echo "resulting state:"
gh api "repos/$repo" --jq '
  "  description:          " + (.description // "none"),
  "  topics:               " + (.topics | join(", ")),
  "  delete branch/merge:  " + (.delete_branch_on_merge | tostring),
  "  secret scanning:      " + (.security_and_analysis.secret_scanning.status // "n/a"),
  "  push protection:      " + (.security_and_analysis.secret_scanning_push_protection.status // "n/a"),
  "  security updates:     " + (.security_and_analysis.dependabot_security_updates.status // "n/a")'
printf '  Dependabot alerts:    '; gh api "repos/$repo/vulnerability-alerts" >/dev/null 2>&1 && echo enabled || echo disabled
printf '  private reporting:    '; gh api "repos/$repo/private-vulnerability-reporting" --jq '.enabled' 2>/dev/null || echo unknown
printf '  CodeQL default setup: '; gh api "repos/$repo/code-scanning/default-setup" --jq '.state + " (" + (.languages | join(", ")) + ")"' 2>/dev/null || echo "not configured"
