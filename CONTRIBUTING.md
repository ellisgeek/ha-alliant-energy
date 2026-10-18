# Contributing

How to work on this integration: setup, checks, git workflow, testing against
a live account, and the traps already found. The README covers install and
use; `docs/adr/` covers design decisions; `docs/backend-api.md` covers
Alliant's API.

## Setup and checks

Open the folder in VS Code and choose **Reopen in Container**. The
devcontainer (Debian base image) installs `uv`, which installs the Python
pinned in `.python-version` (3.14, required by current Home Assistant). It
then syncs the dev dependencies and installs the prek git hooks.

```sh
uv run prek run -a   # ruff (lint + format), ty, actionlint, file checks
uv run pytest        # offline tests; also run by the pre-push hook
```

The tests use `pytest-homeassistant-custom-component` and fake API sessions.
They need no credentials and no network.

## Git workflow

- Make real changes on a branch and open a pull request. Do not push to `main`
  directly.
- Commit messages are Conventional Commits with Angular's types only: `build`,
  `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `test`. There is no `style`
  or `chore`; formatting-only changes are `refactor`. A prek `commit-msg` hook
  rejects anything else. Commit subjects become the changelog, so write them
  for users.
- prek refuses to commit while `.pre-commit-config.yaml` has unstaged changes.
  Commit hook configuration changes first, or together with the change.
- To change an older commit that is not pushed yet, commit the fix with the
  subject `amend! <subject of the commit to change>`, then squash it in:

  ```sh
  SKIP=conventional-pre-commit git commit -m "amend! feat: ..."   # body = new message
  GIT_SEQUENCE_EDITOR=true git rebase -i --autosquash --autostash <base>
  ```

  `SKIP` is needed only because the temporary `amend!` subject is not a
  Conventional Commit; the squashed result is.
- Releases are cut with `scripts/release.sh` and published by pushing the tag
  (see the README's Releases section and ADR 0009).

## Testing against a live account

Avoid it when the offline tests can answer the question. Every successful
login shows up as a device on the account, and repeated failed logins lock the
IP address (the portal has a lock counter).

- Keep credentials in `.env.secret` at the repo root, as `ALLIANT_USERNAME=...`
  and `ALLIANT_PASSWORD=...`. It is git-ignored (`*.secret`).
- Read that file literally, for example with a few lines of Python. Do not
  `source` it in a shell: a `$` inside a double-quoted password is expanded,
  and the wrong password is sent.
- Log in once per run and reuse the session. After a failed login, find the
  cause before trying again; do not retry blindly.
- Never commit anything from a live response without removing personal data:
  name, address, email, account, premise, and meter numbers, and usage.

## Things that are easy to get wrong

- **HTTP client.** The API rejects `aiohttp` and `requests` with HTTP 403
  (Cloudflare checks the TLS fingerprint). Use the `curl_cffi` session in
  `api.py`, not Home Assistant's shared session (ADR 0002).
- **Unit codes.** A wrong `Uom` value returns an empty result, not an error.
  Gas needs `Therm` for hourly and daily data but `TH` for billing cycles
  (`docs/backend-api.md`).
- **Flow schemas.** Home Assistant now uses `probatio`, a drop-in replacement
  for `voluptuous`; `config_flow.py` imports it as `vol`.
- **Recorder in tests.** The manifest depends on `recorder`, so every test sets
  up a real recorder through an autouse fixture in `tests/conftest.py`. The
  recorder fixture must be set up before `hass`, so fixtures that need `hass`
  cannot be autouse.
- **Dependencies.** Home Assistant installs the packages in `manifest.json`
  `requirements`. `pdfplumber` is listed there and in the `dev` group (for
  tests and ty), not in `[project] dependencies`: adding it there once made
  uv drop the recorder's test-only packages (`psutil-home-assistant`,
  `fnv-hash-fast`).
- **Brand icon.** `brand/icon.png` is a generic lightning bolt on purpose. Do
  not replace it with Alliant's logo, which is their trademark.

## Workflow testing

actionlint checks the GitHub Actions workflows statically on every commit.
Running them locally with `act` was tried and dropped: it needs Docker
through docker-outside-of-docker, which did not work in this devcontainer.
Test workflow changes on a pull request instead.
