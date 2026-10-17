# Project guidance

- This is a Home Assistant custom integration for Alliant Energy
  (myaccount.alliantenergy.com), whose portal runs on the SEW "SmartCMobile"
  platform. Integration code lives under `custom_components/alliant_energy`.
- The low-level backend client is in `api.py` and has no Home Assistant
  dependencies. It owns a `curl_cffi` session (Cloudflare blocks aiohttp; see
  `docs/adr/0002-curl-cffi-for-cloudflare.md`) and accepts an injected fake
  session in tests, so keep it unit-testable offline.
  `provider.py` adapts it to the coordinator's `EnergyData` model.
- The backend is unofficial and reverse-engineered. Request signing
  (`x-signature`), header names, and response field names mirror the portal's
  own JavaScript. Confirm any new endpoint against real responses before
  relying on its shape; prefer defensive parsing and logging.
- Do not place credentials in source control. Store them via the config entry.
- Run `uv run ruff check .`, `uv run ty check`, and `uv run pytest`; keep async
  Home Assistant patterns.
- Read `GLOSSARY.md`, `docs/backend-api.md`, and `docs/adr/` before changing
  behaviour.
