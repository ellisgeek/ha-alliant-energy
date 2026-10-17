# Sign in with username and password only; two-factor is a fallback

A login can have two-factor sign-in enabled. Verified live (2026-10-17): the
portal's web UI enforces it, but the backend does not. The access token from
`Login/auth` reads data without a completed challenge, and refreshing the token
never triggers one. reCAPTCHA v3 is also present in the web client but not
enforced by the backend. So the config flow asks only for username and
password. It shows a two-factor challenge step only if data access is refused
while the login reports two-factor enabled.

## Consequences

The challenge path (`users/SendTfa`, `users/VerifyTfa`) was coded from the
portal's JavaScript and has never run against a live challenge. The delivery
mode integers (text, email, voice) are unconfirmed. If Alliant starts enforcing
two-factor in the backend, test that path first.
