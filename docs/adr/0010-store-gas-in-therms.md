# Store gas usage statistics in therms

Supersedes [ADR 0004](0004-store-gas-as-energy-in-kwh.md).

Alliant bills gas in therms. Home Assistant now counts the US therm (`thm`) as
an energy unit, and the Energy dashboard accepts it for gas. So gas usage
statistics are stored in therms, as reported, with no conversion. The values
match the bill, and Home Assistant can still display them in another energy
unit.

## Consequences

- Version 2026.10.0 stored gas in kWh. The recorder can convert units only for
  statistics it owns, not for external ones like ours. So when the importer
  finds a gas statistic stored in kWh, it clears that statistic and imports it
  again in therms. The statistic id stays the same, so Energy dashboard
  settings keep working. History older than the backfill window (one year) is
  lost.
- Gas and gas cost now use the same unit, so cost is usage times $/therm with
  no conversion step.
