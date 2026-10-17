# Use curl_cffi with browser TLS impersonation, not Home Assistant's aiohttp session

The backend is behind Cloudflare bot management, which returns HTTP 403
"Access Denied" to `aiohttp`, `requests`, and plain `curl` regardless of
headers. The block matches the TLS/HTTP fingerprint (JA3/JA4) before the
request reaches the application. `curl_cffi` with `impersonate="chrome"`
passes. The API client therefore owns its own `curl_cffi` `AsyncSession`
instead of using Home Assistant's shared `aiohttp` session, which goes against
the usual Home Assistant convention.

## Considered Options

- **HA's shared aiohttp session.** Rejected: blocked (verified).
- **A headless browser.** Rejected: too heavy for an integration, and not
  installable through `manifest.json` requirements.

## Consequences

`curl_cffi` is a binary-wheel dependency, so it must have wheels for every
platform Home Assistant runs on. If Cloudflare starts rejecting the
impersonated fingerprint, update `curl_cffi` or change the `impersonate`
target first.
