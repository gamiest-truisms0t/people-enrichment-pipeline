#!/usr/bin/env bash
# Publish a release: create the annotated tag if it does not exist, push it, and create or
# update the GitHub Release whose notes are the tag's section of CHANGELOG.md.
#
#   scripts/release.sh v1.2.4          (or: make release TAG=v1.2.4)
#   BACKFILL=1 scripts/release.sh v0.4.0   publish an older tag without making it "Latest"
set -euo pipefail

tag="${1:?usage: scripts/release.sh vX.Y.Z}"
version="${tag#v}"
changelog="$(git rev-parse --show-toplevel)/CHANGELOG.md"

# The section runs from its "## [version]" heading to the next "## " heading. The heading
# itself and leading blank lines are dropped; trailing ones go with the $(...) capture.
notes="$(awk -v v="$version" '
  index($0, "## [" v "]") == 1 { in_section = 1; next }
  /^## / { in_section = 0 }
  in_section' "$changelog" | sed '/./,$!d')"

if [ -z "$notes" ]; then
  echo "error: CHANGELOG.md has no section '## [$version]'" >&2
  exit 1
fi

summary="$(printf '%s\n' "$notes" | head -1)"

if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
  echo "tag $tag exists locally"
else
  git tag -a "$tag" -m "$tag: $summary"
  echo "tagged $tag at $(git rev-parse --short HEAD)"
fi
git push origin "refs/tags/$tag"

# GitHub marks the most recently created release "Latest". Backfilling an old tag
# (BACKFILL=1) must not steal that badge from the current version.
if [ "${BACKFILL:-0}" = 1 ]; then latest="--latest=false"; else latest="--latest"; fi

if gh release view "$tag" >/dev/null 2>&1; then
  gh release edit "$tag" --title "$tag" --notes "$notes" "$latest" >/dev/null
  echo "release $tag updated: $(gh release view "$tag" --json url --jq .url)"
else
  gh release create "$tag" --title "$tag" --notes "$notes" --verify-tag "$latest" >/dev/null
  echo "release $tag published: $(gh release view "$tag" --json url --jq .url)"
fi
