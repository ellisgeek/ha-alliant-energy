# Keep one utility's values in a profile; support only Alliant for now

Alliant's portal runs on Smart Energy Water's SmartCMobile platform
([ADR 0001](0001-use-the-portals-private-backend.md)), and other utilities run
the same product. Its endpoint paths, request signature, and response shapes
belong to the product, but each utility has its own deployment: its own host,
`uid` header, persona type, `meterType` values, and `Uom` codes. Its bill PDF
comes from its own eBill provider in its own layout.

Those per-utility values now live in one `Utility` profile (`utility.py`).
`AlliantEnergyApi` takes a profile. The profile is backend data only, so it
belongs with `api.py` to the backend client, which imports nothing from Home
Assistant or the rest of the integration. Which parser reads a utility's bill
PDFs is the integration's choice, made next to the profile in `const.py`; it
can be `None`. `ALLIANT_ENERGY` is the only profile. The integration is still
Alliant Energy: its domain, name, brand, and statistic IDs do not change.

## Considered Options

- **Generalize to any SmartCMobile utility now, like `opower`.** Rejected for
  now. Every value in `docs/backend-api.md` was confirmed against a live
  account, and the values most likely to differ between utilities are the
  ones that fail silently: a wrong `Uom` returns an empty result, not an
  error. Without an account at a second utility, its profile would be a
  guess. Most of the cost features (marginal rate, projected cost,
  adjustments) also come from the Alliant bill parser, so another utility
  would get much less. Renaming the domain would also need a migration of
  config entries and statistic IDs, or people lose their Energy dashboard
  history.
- **Leave the values as module constants.** Rejected: it is cheap to collect
  them now, and it shows what a second utility would have to confirm.

## Consequences

- To add a utility, someone with an account there confirms each profile value
  live (see `CONTRIBUTING.md`) and adds a profile. A config entry then needs to
  store which utility it uses; entries without one are Alliant.
- A utility without a bill parser still gets usage, billing-cycle cost, the
  bill summary, and the effective rate
  ([ADR 0005](0005-cost-rate-precedence.md)); no bill PDF is downloaded, and
  past bills are not listed ([ADR 0014](0014-price-each-billing-cycle-at-its-own-bill.md)).
- A test pins that `api.py` and `utility.py` import nothing from Home Assistant
  or the rest of the integration, so they can move to their own library.
- Water service is not covered: the client reads only the electric and gas
  usage endpoints.
- If more utilities are added, consider moving `api.py` and the profiles into
  their own library, as `opower` does, and keeping this integration thin.
