#!/usr/bin/env bash
# Cut a release locally: bump the CalVer version, regenerate CHANGELOG.md,
# commit, and tag. It never pushes. Review the result, then push the commit and
# the tag; the Release workflow publishes the GitHub release when the tag lands.
#
# Usage: scripts/release.sh
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

# Untracked files (e.g. local editor settings) can't end up in the release.
if [[ -n "$(git status --porcelain --untracked-files=no)" ]]; then
  echo "error: working tree has uncommitted changes" >&2
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
# git-cliff leaves an extra blank line at the end; keep exactly one newline.
changelog="$(cat CHANGELOG.md)"
printf '%s\n' "$changelog" > CHANGELOG.md

git add pyproject.toml uv.lock custom_components/alliant_energy/manifest.json CHANGELOG.md
git commit --quiet -m "build(release): $version"
git tag -a "$version" -m "$version"

echo "Tagged $version. Review it, then publish with:"
echo "  git push origin HEAD $version"
