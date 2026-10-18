# Import all the hourly history the API has, and rebuild once to get it

A fixed one-year backfill left older data unused. How far back Alliant's
hourly data goes is not documented and was never measured. Probing it on a
live account would add load and risk. So a meter's first import walks back
from today in 30-day chunks until two chunks in a row come back empty, which
marks the start of the history. Two, so that a short gap such as a meter swap
does not end the walk early. The history limit option (30 days to 10 years,
default 10 years) stops it in any case. If an
older chunk fails with an API error after data was found, the walk stops there
and keeps what it has, because the API may reject dates before the history
starts.

Statistics hold a running sum, so older hours cannot be added in front of
existing ones without rewriting every sum after them. A meter that was not
imported this way is therefore cleared and imported again once, in full. That
covers statistics from earlier versions (one year, gas in kWh), a missing
usage statistic, a cost rate that becomes available after usage was imported,
and a change to the history limit option. A Home Assistant store
(`alliant_energy.statistics`) records which meters have their full history and
the limit each was imported with.

## Consequences

- The first import makes one request per 30 days of history, with a pause
  between them, plus two empty requests at the end. After that, updates fetch
  only from the last imported day (ADR 0007).
- A rebuild fetches everything before it clears the old statistics, so a
  failed fetch keeps the existing data.
- Effective rates for a rebuild come from billing cycles back to the oldest
  hour, requested a year at a time, so old hours use their own cycle's rate.
- Deleting the store file forces a full rebuild of every meter on the next
  update.
- Changing the history limit rebuilds every meter once. Raising it fetches
  older history; lowering it removes history older than the new limit, so the
  dashboard always matches the setting.

See [ADR 0012](0012-price-old-hours-by-billing-cycle.md): a marginal rate covers only
its own billing cycle and later hours, and a rate change rebuilds cost only.
