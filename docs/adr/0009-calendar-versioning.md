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

- The release workflow rejects tags that are not CalVer and stamps the tag
  into `manifest.json`, which HACS shows as the installed version.
- Keep `pyproject.toml` `version` and `manifest.json` `version` at the next
  planned release.
- Breaking changes, such as a raised minimum Home Assistant version, go in the
  release notes, because the version number does not signal them.
- Switching back to SemVer later is impractical: any SemVer number sorts below
  the CalVer versions already published.
