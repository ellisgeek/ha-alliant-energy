# Price each billing cycle at its own bill's marginal rate

Refines [ADR 0012](0012-price-old-hours-by-billing-cycle.md).

ADR 0012 knew a marginal rate only for the latest bill, so hours older than its
billing cycle used their cycle's effective rate, which includes fixed charges
and runs higher. `bill/History` lists an account's past bills with their
invoice ids, so their PDFs can be read too. Now each billing cycle uses its own
bill's marginal rate, from its billing cycle's start until the next bill's.
Hours older than every bill read keep their cycle's effective rate, and a
static rate still prices every hour.

Past bills are read only when older hours need a price: on a full-history
import and on a cost reprice. Normal updates price recent hours with the
latest bill. Past bills never change, so each PDF is downloaded and parsed once
and its itemized bill saved per config entry (`alliant_energy.bills.<entry>`).
A past PDF that fails to read is left out, its cycle uses the effective rate,
and it is tried again on the next full import or reprice.

## Consequences

- The cost rate identity for derived rates is now `derived:2`, so every cost
  statistic priced under ADR 0012 is repriced once. That reprice reads the
  account's past bills: one PDF per bill in the imported history, paced, once.
  For ten years of history that is up to about 120 downloads.
- The step in cost where the marginal rate began (ADR 0012) now appears only
  before the oldest bill read.
