# Feed the Energy dashboard from imported statistics, not sensor state

Hourly usage readings arrive about two days late, and years of history are
available. Sensor state can only record "now". So the integration imports
each meter's hourly usage readings as external long-term statistics
(`alliant_energy:{account}_{service}_{meter}`) with a running sum, following
the built-in `opower` integration. Each meter then appears as an Energy
dashboard source with correct timestamps. The sensors only show headline
figures (latest daily usage, bill summary) and are not Energy dashboard
sources.

## Consequences

- Statistics import runs as a background task on its own timer, separate from
  the coordinator, so the history backfill (ADR 0011) does not block setup.
- The backfill fetches history in 30-day chunks with a pause between them, to
  stay gentle on the account (see ADR 0007).
- Imports continue from the last stored sum. Hours already imported are not
  rewritten, so a reading Alliant corrects later is not picked up.
