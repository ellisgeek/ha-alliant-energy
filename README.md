# Alliant Energy for Home Assistant

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories)

A custom Home Assistant integration that reads electricity and gas usage,
cost, and bills from an [Alliant Energy](https://myaccount.alliantenergy.com/)
customer account. Hourly usage goes into the Energy dashboard, with a year of
history imported on first setup.

> **Unofficial.** Alliant publishes no customer API. This integration uses the
> private backend of Alliant's customer portal, which can change or break
> without notice. It is not affiliated with or endorsed by Alliant Energy.

## Features

- Hourly electricity and gas usage in the Energy dashboard, with about one year
  of history imported on first setup.
- Hourly cost for each meter, priced from the rates on your latest bill.
- Sensors for the latest daily usage, the last bill cost per service, and the
  current bill: amount due, current charges, past due amount, previous balance,
  due date, and invoice date.
- Setup needs only your portal username and password. The integration finds
  your account, premise, and meters for you.

## Requirements

- Home Assistant 2026.10 or newer.
- An Alliant Energy online account (myaccount.alliantenergy.com) with a smart
  (AMI) meter. Hourly data needs a smart meter.

## Installation

### HACS (recommended)

1. In HACS, open the menu (three dots, top right) and choose **Custom
   repositories**.
2. Add `https://github.com/ellisgeek/ha-alliant-energy` with type **Integration**.
3. Search HACS for **Alliant Energy** and download it.
4. Restart Home Assistant.

### Manual

Copy `custom_components/alliant_energy` into the `custom_components` folder of
your Home Assistant configuration directory, then restart Home Assistant.

## Configuration

1. Go to **Settings > Devices & services > Add integration** and choose
   **Alliant Energy**.
2. Enter the username (or email) and password you use for the Alliant portal.
3. If the login has more than one account, pick the account to add.

To add the data to the Energy dashboard, go to **Settings > Dashboards >
Energy**. Add the `Alliant Energy electric ...` statistic under **Electricity
grid** and the `Alliant Energy gas ...` statistic under **Gas consumption**.
For cost, choose **Use an entity tracking the total costs** and pick the
matching `... cost` statistic.

The first import runs in the background and takes a few minutes. Hourly data
is about two days behind, so the newest days fill in on later updates.

### Options

**Configure** on the integration lets you set a fixed electricity rate ($/kWh)
or gas rate ($/therm). Leave a field blank to use the rate from your bill.

## How cost is calculated

Alliant's usage data only gives cost per billing cycle, rounded to whole
dollars. To get hourly cost, the integration multiplies each hour's usage by a
rate, chosen in this order:

1. The fixed rate from the options, if you set one.
2. The **marginal rate** read from your latest itemized bill PDF. For
   electric, that is the energy charge plus the fuel adjustment. For gas, it is
   the distribution charge plus the gas supply charges.
3. The **effective rate**: the billing cycle's amount divided by its usage.

The marginal rate leaves out fixed charges such as the daily customer charge,
fees, and taxes. Your Energy dashboard cost is therefore lower than your bill
total. The **Current charges** sensor shows the real bill total. See
[ADR 0005](docs/adr/0005-cost-rate-precedence.md) for the reasoning.

## Notes and limitations

- **Gas is shown in kWh in the Energy dashboard.** The dashboard does not
  accept therms, so gas is stored as energy (1 therm = 29.3001 kWh). The
  latest-day gas sensor still uses therms.
- **Two-factor sign-in.** If your login has two-factor enabled, the integration
  still works. Alliant enforces it only in the web portal. Each successful
  sign-in may show up as a new device on your account.
- **Be gentle with your account.** Repeated failed logins can lock your IP
  address. If your password changes, Home Assistant asks you to re-enter it and
  does not keep retrying the old one.
- Data updates every 6 hours.
- Hours that Alliant corrects after they were imported are not updated.

## How it works

Alliant's portal is a Smart Energy Water (SEW) "SmartCMobile" web app backed by
a JSON API at `alliant-svc.smartcmobile.com`. The integration makes the same
requests the portal makes. That API is behind Cloudflare bot protection, which
rejects ordinary Python HTTP clients, so the integration uses `curl_cffi` with
Chrome TLS impersonation.

| Module | Role |
| --- | --- |
| `api.py` | Low-level client: login, token refresh, request signing, discovery, usage, bills, bill PDF. No Home Assistant imports. |
| `billparser.py` | Reads marginal rates from the bill PDF (`pdfplumber`). |
| `statistics.py` | Imports hourly usage and cost as long-term statistics for the Energy dashboard. |
| `provider.py` | Connects the client to Home Assistant: owns the session, resolves rates, builds sensor data. |
| `coordinator.py`, `sensor.py` | Polls every 6 hours and exposes sensors. |
| `config_flow.py` | Login, account selection, reauthentication, two-factor fallback, rate options. |

More detail:

- [GLOSSARY.md](GLOSSARY.md): the terms used in code and docs.
- [docs/backend-api.md](docs/backend-api.md): what is known about Alliant's
  private API.
- [docs/adr/](docs/adr/): architecture decisions and the reasons for them.

## Development

Open the folder in VS Code and choose **Reopen in Container**. The devcontainer
has Python 3.14, `uv`, and the GitHub CLI. When it is created, it installs the
locked development dependencies and the [prek](https://github.com/j178/prek)
git hooks.

```sh
uv sync --group dev        # install dependencies
uv run prek install        # git hooks (the devcontainer does this for you)
uv run prek run -a         # all hooks: ruff, ty, actionlint, file checks
uv run pytest              # offline test suite (also runs on git push)
```

Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
with the [Angular types](https://github.com/angular/angular/blob/main/contributing-docs/commit-message-guidelines.md)
(`build`, `ci`, `docs`, `feat`, `fix`, `perf`, `refactor`, `test`). A prek
`commit-msg` hook checks this.

The tests use `pytest-homeassistant-custom-component` with a fake HTTP session.
They need no Alliant credentials and no network access. Do not test against a
live account more than necessary, because failed logins can lock the account.

### Releases

Releases use calendar versioning in Home Assistant's format, `YYYY.M.MICRO`
(for example `2026.10.0`), with no `v` prefix on tags. See
[ADR 0009](docs/adr/0009-calendar-versioning.md).

To release, run the **Release** workflow from the Actions tab. It runs the
checks, then `scripts/release.sh`:

1. [bumpver](https://github.com/mbarkhau/bumpver) picks the next version for
   today's date and updates `pyproject.toml` and `manifest.json`. The first
   release of an untagged version keeps the version as it is.
2. [git-cliff](https://git-cliff.org/) regenerates `CHANGELOG.md` in
   [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) format from the
   Conventional Commits. `feat` goes under Added, `fix` under Fixed, `perf`
   and breaking changes under Changed, and `fix(security)` under Security.
   Commits that start with "deprecate" or "remove" go under Deprecated or
   Removed. `docs`, `test`, `build`, `ci`, and `refactor` commits are left out
   unless they are breaking.
3. The script commits `build(release): <version>` and tags it.

The workflow then pushes the commit and tag and publishes a GitHub release
with the changelog entry and the `alliant_energy.zip` that HACS installs.

`scripts/release.sh` also works locally. It never pushes, so you can inspect
the result and then push the commit and tag yourself.

## License

[MIT](LICENSE)
