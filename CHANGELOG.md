# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses calendar versioning, `YYYY.M.MICRO`, like Home Assistant
(see [ADR 0009](docs/adr/0009-calendar-versioning.md)).

## [2026.11.0] - 2026-11-07

### Added

- Price each billing cycle at its own bill's marginal rate
- Fetch Alliant's own usage projection (#20) (#25)

### Fixed

- Close the session when a flow is abandoned
- Ignore a saved bill that is missing its invoice
- Re-login automatically on token expiry (#23) (#24)

### Security

- Update certifi, homeassistant, and uv in lock file (#26)

## [2026.10.2] - 2026-10-18

### Fixed

- Price old hours by their own billing cycle
- **Breaking:** Report gas usage in thm and the meter reading in the bill's unit
  - Home Assistant raises a repair for the gas usage sensor's changed unit and stops recording its long-term statistics until it is fixed. In Developer tools > Statistics, update the unit of the existing history.
- Log in once when updates overlap
- Serialize statistics store updates
- Stop the import before closing, and never retry a rejected login

## [2026.10.1] - 2026-10-18

### Added

- Store gas usage in therms
- Import all the hourly history the API has
- Add rate and rate plan sensors
- Add projected bill and sensors read from the bill PDF
- Use the Alliant Energy logo
- Add maximum history and update interval options

### Changed

- Fetch only hourly data that is not imported yet
- Download the bill PDF only once per bill

## [2026.10.0] - 2026-10-17

### Added

- Add Alliant Energy Home Assistant integration

### Fixed

- Declare recorder as a dependency

[2026.11.0]: https://github.com/ellisgeek/ha-alliant-energy/compare/2026.10.2...2026.11.0
[2026.10.2]: https://github.com/ellisgeek/ha-alliant-energy/compare/2026.10.1...2026.10.2
[2026.10.1]: https://github.com/ellisgeek/ha-alliant-energy/compare/2026.10.0...2026.10.1
[2026.10.0]: https://github.com/ellisgeek/ha-alliant-energy/releases/tag/2026.10.0
