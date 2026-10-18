# Price old hours by billing cycle, and keep imported cost

Refines [ADR 0005](0005-cost-rate-precedence.md) and
[ADR 0011](0011-import-full-history.md).

Only the latest bill PDF is parsed, so a marginal rate is known for one
billing cycle only. Applying it to the whole history priced ten years of hours
at today's rate. So each hour of a cost statistic gets a cost rate this way: a
static rate wins for every hour; otherwise the latest bill's marginal rate
covers that service's billing cycle and every later hour, and older hours use
their own billing cycle's effective rate. A billing cycle reported with cost 0
counts as missing; an hour in a gap between cycles uses the cycle that ended
before it. If the bill gives no billing cycle start, the marginal rate covers
the hours after the latest billed cycle.

A cost statistic is append-only: hours keep the cost rate they were imported
with. When the next bill arrives, the previous cycle is not repriced, so each
cycle ends up near its own marginal rate. Only a change to the static rate
(set, changed, or cleared) rebuilds the cost statistic. A cost-only rebuild
reprices the hourly usage already in the usage statistic, without fetching
history from Alliant again.

## Considered Options

- **One marginal rate for all history.** Rejected: it misprices every older
  billing cycle.
- **A marginal rate per billing cycle from older bill PDFs** (`bill/History`).
  Most accurate, but it needs one PDF download per month of history, and it is
  not yet confirmed that `bill/History` returns invoice ids. Worth revisiting.
- **An effective rate with the customer charge taken out.** Rejected: taxes
  and fees stay in, and the cycle amount is rounded to whole dollars.
- **Rebuild when the marginal rate appears, changes, or disappears.** Rejected:
  a new bill every month would reprice history monthly, and one failed PDF
  parse would make the statistic flap.

## Consequences

- The cost statistic steps down at the start of the first billing cycle priced
  at a marginal rate, because older effective rates include fixed charges.
- Existing cost statistics, built under the old rule, are repriced once.
- If a static rate is cleared and no other rate is available, existing cost
  hours stay and no new ones are added. The warning is logged once, and the
  cost statistic is repriced when a rate is available again.
- A full-history rebuild (ADR 0011) with no rate available clears the cost
  statistic along with the usage, so both match the history limit.
