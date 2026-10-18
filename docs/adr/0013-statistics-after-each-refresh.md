# Start the statistics import after each sensor refresh

Refines [ADR 0003](0003-hourly-usage-as-external-statistics.md).

The statistics import ran on its own timer, on the same interval as the sensor
refresh. Each run fetched the bill summary again, so every interval made two
`bill/Current` requests, and the two runs handled login failures separately.
Now the import starts as a background task after each sensor refresh, priced
with the bill that refresh fetched or, when the refresh failed, with the last
bill fetched. The history backfill still runs off the setup path.

## Considered Options

- **Keep both timers and reuse a recently fetched bill.** Rejected: it adds a
  cache instead of removing a timer, and leaves two schedules.

## Consequences

- One `bill/Current` request per interval (ADR 0007), and statistics always
  use the same bill the sensors show.
- A failed refresh still imports, so a broken sensor endpoint (such as
  `bill/Current`) does not stop the Energy dashboard's usage. Only an auth
  failure, or a failure before any refresh has succeeded, imports nothing.
- A refresh that comes while an import is still running (a long backfill)
  starts no second import.
