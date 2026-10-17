# CLAUDE.md

Home Assistant custom integration for Alliant Energy (electric + gas usage,
cost, and bill data). The integration lives under
`custom_components/alliant_energy`. See `README.md` for architecture,
`docs/backend-api.md` for the reverse-engineered backend, and `docs/adr/` for
decisions.

Checks: `uv run prek run -a` (ruff, ruff format, ty, actionlint, file checks) and
`uv run pytest`; prek hooks run them on commit/push. Targets
current Home Assistant only (Python 3.14; ADR 0008).

Commits: Conventional Commits, Angular types only (`build`, `ci`, `docs`,
`feat`, `fix`, `perf`, `refactor`, `test`); enforced by a prek `commit-msg` hook.

Releases: CalVer `YYYY.M.MICRO` like Home Assistant (e.g. `2026.10.0`, no `v`
prefix; ADR 0009). `scripts/release.sh` (bumpver + git-cliff) bumps, writes
the changelog, commits, and tags locally; pushing the tag triggers the Release
workflow, which publishes. Never edit versions by hand. Never push, tag, or
publish a release unless asked.

## Agent skills

### Issue tracker

Issues and specs are tracked as GitHub issues via the `gh` CLI. See
`docs/agents/issue-tracker.md`. The repo is
[ellisgeek/ha-alliant-energy](https://github.com/ellisgeek/ha-alliant-energy);
`gh` is installed in the devcontainer and must be authenticated
(`gh auth login`) before issue operations work.

### Domain docs

Single-context: one `GLOSSARY.md` plus `docs/adr/` at the repo root (created
lazily by the domain-modeling skill). See `docs/agents/domain.md`.
