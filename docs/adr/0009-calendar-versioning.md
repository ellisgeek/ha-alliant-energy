# Version releases with CalVer, matching Home Assistant

Releases use calendar versioning in Home Assistant's own format,
`YYYY.M.MICRO`: the year, the month without zero padding, and a counter that
starts at 0 for each month (`2026.10.0`, `2026.10.1`, `2026.11.0`). Tags carry
no `v` prefix. The integration only supports current Home Assistant releases
(ADR 0008), so a version that reads like the Home Assistant release it was
built against tells users more than a SemVer number would. The backend is
unofficial and can break without notice, so most releases are fixes anyway,
and SemVer's major/minor/patch split would carry little meaning.

## Consequences

- bumpver (`YYYY.MM.INC0`) computes the next version and writes it to
  `pyproject.toml` and `manifest.json`. HACS shows the manifest version as the
  installed version. Do not edit versions by hand.
- git-cliff builds `CHANGELOG.md` in Keep a Changelog format from
  Conventional Commits, so commit subjects are the release notes. Write them
  for users.
- Breaking changes, such as a raised minimum Home Assistant version, go in the
  release notes, because the version number does not signal them.
- Switching back to SemVer later is impractical: any SemVer number sorts below
  the CalVer versions already published.
