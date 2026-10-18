# Alliant backend API reference

What is known about the private JSON API behind myaccount.alliantenergy.com.
Nothing here is documented by Alliant or SEW. Everything was taken from the
portal's JavaScript and confirmed against live responses in October 2026. See
[ADR 0001](adr/0001-use-the-portals-private-backend.md) for why the integration
uses this API.

## Transport

All requests go to `https://alliant-svc.smartcmobile.com`. Cloudflare blocks
non-browser TLS fingerprints with HTTP 403, so requests must use `curl_cffi`
with `impersonate="chrome"` ([ADR 0002](adr/0002-curl-cffi-for-cloudflare.md)).

The API is split across service roots:

| Root | Used for |
| --- | --- |
| `UsermanagementAPI/api/1/` | login, token refresh, two-factor |
| `Services/api/1/` | accounts, meters, bills |
| `AccountManagementApi/api/1/` | account details (not used) |
| `UsageAPI/api/V1/` | usage readings |

### Headers

| Header | Value | When |
| --- | --- | --- |
| `uid` | `1` (utility id for Alliant) | every request |
| `Authorization` | `Bearer {accessToken}` | every request after login |
| `pt` | `1` (persona type) | usage requests |
| `pt` | `user.defaultPersona` | two-factor requests |
| `x-signature`, `x-inputchecksum` | request signature | requests with a JSON body |
| `Origin`, `Referer` | the portal URL | every request |

### Request signature

`x-signature` and `x-inputchecksum` both carry the uppercase hex MD5 of the
serialized JSON body, with the last two characters removed. The body must be
sent as exactly the bytes that were signed, so serialize it once with
`json.dumps(body, separators=(",", ":"))` and send that string.

### Response envelope

Most endpoints return `{"status": {"code": 200, "message": ...}, "data": ...}`.
A non-200 `status.code` is an error; 401 and 403 mean authentication failed.
Usage endpoints return a flatter `{"StatusCode": 200, "Result": {...}}`.

## Authentication

**`POST UsermanagementAPI/api/1/Login/auth`**

```json
{
  "username": "...",
  "password": "...",
  "guestToken": "",
  "customattributes": {
    "ip": "#1", "client": "Web", "version": "1.0",
    "deviceId": "", "deviceName": "", "deviceType": 0, "os": ""
  }
}
```

Returns `data.accessToken`, `data.refreshToken`, `data.expiresIn`,
`data.tokenType`, and `data.user` (`uuid`, `tfa`, `defaultPersona`). When
two-factor is enabled, `user.tfa` is true and `data.tfaDetails` has the masked
phone and email.

The web client attaches a reCAPTCHA v3 token, but the backend does not check
it. Repeated failed logins can lock the IP; see
[ADR 0007](adr/0007-minimise-logins-and-load.md).

**`POST UsermanagementAPI/api/1/Login/auth/refresh`** with
`{"refreshToken": "..."}` returns a new token pair in the same shape.

### Two-factor (fallback, unverified)

The backend does not require a completed challenge
([ADR 0006](adr/0006-username-password-without-two-factor.md)). These
endpoints exist for when it does:

- `POST users/SendTfa` with `{uuid, mode, CustomAttributes}`. The `mode`
  integer selects text, email, or voice; the mapping is unconfirmed.
- `POST users/VerifyTfa` with `{uuid, code, CustomAttributes}`.
- `GET users/TfaDevices/{uuid}` lists delivery devices without sending a code.

Remember-device is disabled on Alliant's deployment.

## Discovery

**`GET Services/api/1/Addresses/User/{uuid}`** returns `data[]`, one entry per
account, with `accountNumber`, `premiseNumber`, `premiseId`, `emailAddress`,
and `customerAttribute1` (`"RES"` for residential). This lets setup ask only
for username and password.

**`POST Services/api/1/Usages/GetMeterAndPremise`** with
`{accountNumber, premiseNumber}` returns `data[]` meters: `meterNumber`,
`meterType` (1 = electric, 2 = gas), `premiseNumber`, `amiMeter`, `address`.

## Usage

**`GET UsageAPI/api/V1/Electric`** and **`GET UsageAPI/api/V1/Gas`**

| Parameter | Value |
| --- | --- |
| `AccountNumber` | `"{premiseNumber}-{accountNumber}"` (both, joined) |
| `MeterNumber` | from `GetMeterAndPremise` |
| `From`, `To` | `MM/DD/YYYY` |
| `Periodicity` | `HH` hourly, `DA` daily, `MO` billing cycle |
| `Uom` | see below |

`IN` (interval) returns HTTP 500. `D` is not valid.

**The unit code depends on service and periodicity.** A wrong code returns an
empty result set, not an error:

| Service | `HH` / `DA` | `MO` |
| --- | --- | --- |
| Electric | `kWh` | `kWh` |
| Gas | `Therm` | `TH` |

Records are in `Result.electricUsages` or `Result.gasUsages`:

- `consumption`: usage in the native unit.
- `amount`: cost. Always 0 for `HH` and `DA`. For `MO` it is the billing cycle
  cost, rounded to whole dollars. A cycle can have 0 consumption and a
  non-zero amount (fixed charges only).
- `readDate`: the hour or day for `HH`/`DA`. It carries a fixed, wrong UTC
  offset; treat the wall-clock time as local time.
- `readingFrom`, `readingTo`: the billing cycle for `MO`; `0001-01-01` for
  `HH`/`DA`.
- `uom`: `"KWH"` or `"THERM"`.

Hourly data lags about two days. A 7-day `HH` request returns about 162 hours.

## Bills

**`POST Services/api/1/bill/Current`** with `{"accountNumbers": [...]}` returns
the bill summary: `totalAmountDue`, `currentCharges`, `pastDueAmount`,
`previousBalance`, `netDueDate` and `invoiceDate` (`MM-DD-YYYY`), and
`invoiceId`.

`bill/History` (needs `startDate`/`endDate`) lists past bills. Its amounts are
account totals for all services together.

**`POST Services/api/1/bill/GetBillPdf`** with
`{accountNumber, invoiceId, documentType: "BILL"}` returns `data.billPdf`, a
signed short-lived URL on a third-party eBill host (KUBRA, `i-doxs.net`). A GET
on that URL (following redirects) returns the PDF.

### Bill PDF contents

The PDF has exact amounts in cents and per-component rates that the JSON API
does not expose. Charge lines look like `Energy Charge 412.000 kWhX$0.15847 $65.29`.

| Service | Line | Unit | Used in marginal rate |
| --- | --- | --- | --- |
| Electric | Energy Charge | $/kWh | yes |
| Electric | Fuel Adjustment | $/kWh (changes monthly, can be negative) | yes |
| Electric | Customer Charge | $/day | no |
| Electric | State-Wide Low-Income Assistance Fee | $ | no |
| Gas | Distribution Charge | $/therm | yes |
| Gas | Gas Supply Base Rate | $/therm | yes |
| Gas | Gas Supply Market Adj | $/therm (changes, can be negative) | yes |
| Gas | Customer Charge | $/day | no |

Each service section starts with its rate line, for example
`Rate: RG1 - Residential Electric Service Billed for: 31 Days`. A service can
have more than one section on a bill (for example a short period when service
started); the integration uses the section with the latest period. pdfplumber
can interleave text from side columns (the message board) with the charges, so
parse one line at a time.

Other lines the integration reads:

| Line | Example | Notes |
| --- | --- | --- |
| Bill date | `Bill Date Feb 07, 2027` | Gives the year for the billing period |
| Next read | `Next Meter Reading: Mar 06, 2027 - Mar 09, 2027` | A window, not a date |
| Meter, electric | `Jan 06 - Feb 06 <meter> 41,736 41,324 412 kWh 1 412 kWh` | Period (no year), meter, current and previous register, units, multiplier, usage |
| Meter, gas | `Jan 06 - Feb 06 <meter> 3,165 3,118 47 CCF 1 1.04 49therms` | Adds the heat factor; no space before `therms` |
| Weather | `Avg. Temp 60°F`, `Degree Days 157` | Per bill, repeated in each section |
| Proration | `Bill Factor Proration = 19 Days/31 Days = 0.6129032` | When a rate changes mid-period; the newest adjustment line comes last |

`tests/fixtures/bill_2026_10.pdf` is a real bill with personal details
replaced; usage, meter readings, rates, and dates made up (dollar amounts
recomputed to match); images and insert pages removed; and metadata cleared.
`tests/fixtures/bill_2026_10.txt` is its original text layer, changed the same
way. The examples above come from these fixtures.
