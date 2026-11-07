#!/usr/bin/env python3
"""Verify manifest.json requirements and version match pyproject.toml."""

import json
import re
import sys
import tomllib
from pathlib import Path

root = Path(__file__).parent.parent

manifest = json.loads((root / "custom_components/alliant_energy/manifest.json").read_text())
pyproject = tomllib.loads((root / "pyproject.toml").read_text())

errors: list[str] = []


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# version must match
manifest_version = manifest["version"]
pyproject_version = pyproject["project"]["version"]
if manifest_version != pyproject_version:
    errors.append(
        f"version: manifest.json={manifest_version!r}  pyproject.toml={pyproject_version!r}"
    )

# collect every dep string from project + all dependency-groups
all_deps: list[str] = list(pyproject["project"].get("dependencies", []))
for group in pyproject.get("dependency-groups", {}).values():
    for item in group:
        if isinstance(item, str):
            all_deps.append(item)

# normalise dep names for lookup: "curl-cffi==0.16.3" and "curl_cffi==0.16.3" are the same
normalized_deps = {_normalize(re.split(r"[><=!~\[]", d)[0]): d for d in all_deps}

for req in manifest.get("requirements", []):
    pkg = _normalize(re.split(r"[><=!~\[]", req)[0])
    if pkg not in normalized_deps:
        errors.append(f"manifest requirement {req!r} missing from pyproject.toml")
    elif normalized_deps[pkg] != req:
        errors.append(f"manifest requirement {req!r} != pyproject.toml {normalized_deps[pkg]!r}")

if errors:
    for e in errors:
        print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)
