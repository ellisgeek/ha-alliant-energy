# Read data from the customer portal's private backend

Alliant Energy publishes no customer API and is not an Opower utility, so the
built-in `opower` integration cannot reach it. Its portal
(myaccount.alliantenergy.com) is a Smart Energy Water (SEW) "SmartCMobile"
single-page app backed by a JSON API at `alliant-svc.smartcmobile.com`. The
integration calls that JSON API directly and reproduces the portal's own
requests, including its request signature (`x-signature`, a truncated MD5 of
the body). The backend can change without notice, so parsing is defensive and
every endpoint shape was confirmed against live responses before use.

## Considered Options

- **Scrape the portal's HTML.** Rejected: the portal is a client-rendered SPA,
  so the HTML holds no data. Scraping would need a headless browser, which a
  Home Assistant integration cannot ship.
- **Green Button or file downloads.** Rejected: they need a manual export and
  do not give the hourly, unattended updates the Energy dashboard needs.

## Consequences

Field names and request shapes come from the portal's JavaScript and live
captures, not from documentation. `docs/backend-api.md` records what is known.
Unit codes are a trap: a wrong `Uom` value returns an empty result, not an
error.
