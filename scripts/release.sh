#!/usr/bin/env bash
# Cut a release locally: bump the CalVer version, regenerate CHANGELOG.md,
# commit, and tag. It never pushes; push the commit and tag yourself, or run
# the Release workflow on GitHub, which calls this script and then publishes.
#
# Usage: scripts/release.sh [--notes FILE]
#   --notes FILE   also write this release's notes (for the GitHub release)
set -euo pipefail

notes=""
if [[ "${1:-}" == "--notes" ]]; then
  notes="${2:?--notes needs a file path}"
fi

cd "$(git rev-parse --show-toplevel)"

if [[ -n "$(git status --porcelain)" ]]; then
  echo "error: working tree is not clean" >&2
  exit 1
fi

current="$(uv run --frozen bumpver show --no-fetch --environ | sed -n 's/^CURRENT_VERSION=//p')"

# Release the current version as-is if it was never tagged (e.g. the first
# release); otherwise let bumpver pick the next CalVer version for today.
if git rev-parse -q --verify "refs/tags/$current" >/dev/null; then
  uv run --frozen bumpver update --no-fetch
  version="$(uv run --frozen bumpver show --no-fetch --environ | sed -n 's/^CURRENT_VERSION=//p')"
  uv lock --quiet
else
  version="$current"
fi

uv run --frozen git-cliff --tag "$version" --output CHANGELOG.md
if [[ -n "$notes" ]]; then
  uv run --frozen git-cliff --tag "$version" --unreleased --strip all --output "$notes"
fi

git add pyproject.toml uv.lock custom_components/alliant_energy/manifest.json CHANGELOG.md
git commit --quiet -m "build(release): $version"
git tag -a "$version" -m "$version"

echo "$version"
