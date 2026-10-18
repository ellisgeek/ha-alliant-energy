---
status: superseded by ADR 0010
---

# Store gas usage statistics in kWh

Alliant bills gas in therms. The Energy dashboard accepts gas only as volume
(m³, ft³, CCF) or energy (kWh, MWh, ...), not therms. The bill converts
metered CCF to therms with a heat factor that changes over time, so converting
back to volume would be inexact. Therms are a unit of energy (1 therm =
29.3001 kWh, exact by definition), so gas usage statistics are stored in kWh.

## Consequences

The Energy dashboard shows gas in kWh, not therms or CCF. Gas cost is still
priced per therm, and the conversion happens only when the usage statistic is
written. The "latest day" gas sensor keeps therms because it is not an Energy
dashboard source.
