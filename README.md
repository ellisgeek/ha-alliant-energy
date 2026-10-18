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

### 1. Add the integration

1. Go to **Settings > Devices & services > Add integration** and choose
   **Alliant Energy**.
2. Enter the username (or email) and password you use for the Alliant portal.
3. If the login has more than one account, pick the account to add.

The integration then imports about a year of hourly history in the
background. This takes a few minutes. The statistics do not appear in the
Energy dashboard pickers until the first import is done.

### 2. Add your meters to the Energy dashboard

The integration creates two statistics per meter. The meter number is in the
name:

| Statistic | What it holds |
| --- | --- |
| `Alliant Energy electric <meter>` | Hourly electricity use, in kWh |
| `Alliant Energy electric <meter> cost` | Hourly electricity cost |
| `Alliant Energy gas <meter>` | Hourly gas use, in kWh (see below) |
| `Alliant Energy gas <meter> cost` | Hourly gas cost |

These are imported statistics, not sensors. In the Energy dashboard dialogs
they show the label "Alliant Energy" under the name.

**Electricity**

1. Go to **Settings > Dashboards > Energy**.
2. Under **Electricity grid**, add a grid connection.
3. In the energy imported field, pick `Alliant Energy electric <meter>`.
4. Leave **Energy exported to grid** empty. Alliant reports only imported
   energy.
5. Optional: set **Display name** to something readable, for example
   "Alliant". Otherwise the dialog can show the raw statistic id.
6. Under **Cost tracking**, choose **Use an entity tracking the total costs**,
   then pick `Alliant Energy electric <meter> cost`.
7. Select **Save**.

**Gas**

1. Under **Gas consumption**, add a gas source.
2. In **Gas consumption**, pick `Alliant Energy gas <meter>`.
3. Leave **Gas flow rate** empty.
4. Under the cost options, choose **Use an entity tracking the total costs**,
   then pick `Alliant Energy gas <meter> cost`.
5. Select **Save**.

> **Why "Use an entity with current price" and "Use a static price" are
> greyed out:** Home Assistant allows these two options only for live sensors.
> It calculates their cost from state changes as they happen. Imported
> statistics have no live state, so Home Assistant turns these options off.
> The integration calculates the cost itself and imports it as the `... cost`
> statistic, which is why you pick **Use an entity tracking the total costs**.
> To use a fixed price instead, set it in the integration's options (see
> below).

Gas is stored as energy (kWh), not volume, because the Energy dashboard does
not accept therms. Home Assistant does not let you mix gas volume sources and
gas energy sources. If you already track gas by volume (for example with a
CCF or ft³ meter sensor), use one or the other.

Hourly data is about two days behind, so the newest days fill in on later
updates. The dashboard shows nothing for the last day or two.

### Options

To use a fixed price, open **Settings > Devices & services > Alliant Energy**
and select **Configure**. Enter an electricity rate ($/kWh), a gas rate
($/therm), or both. Leave a field blank to use the rate from your bill. When
you save, the integration reloads and imports new hours at that rate. Hours
that were already imported keep their old cost.

### Troubleshooting

- **No `Alliant Energy ...` statistics in the picker:** the first import may
  still be running. Wait a few minutes, then reopen the dialog. To check,
  search for `alliant` in **Developer tools > Statistics**.
- **No `... cost` statistic:** the integration could not find a rate. Look in
  **Settings > System > Logs** for `Could not read rates from the bill PDF` or
  `Alliant Energy statistics import failed`. As a workaround, set a fixed rate
  in the options.
- **Asked to sign in again:** your Alliant password changed, or the session
  could not be renewed. Re-enter the password when Home Assistant asks. The
  integration does not retry an old password, so repeated failed logins don't
  lock your account.

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

See [CONTRIBUTING.md](CONTRIBUTING.md) for the git workflow, live-account
testing, and known traps.

### Releases

Releases use calendar versioning in Home Assistant's format, `YYYY.M.MICRO`
(for example `2026.10.0`), with no `v` prefix on tags. See
[ADR 0009](docs/adr/0009-calendar-versioning.md).

To cut a release, run `scripts/release.sh` on a clean `main`:

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
3. The script commits `build(release): <version>` and tags it. It never
   pushes.

Review the commit and changelog, then push both:

```sh
git push origin HEAD 2026.10.1   # the version the script printed
```

Pushing the tag starts the **Release** workflow. It checks that the tag is
CalVer and matches `manifest.json`, runs the checks, and publishes a GitHub
release with the changelog entry as notes and the `alliant_energy.zip` that
HACS installs.

## License

[MIT](LICENSE)
