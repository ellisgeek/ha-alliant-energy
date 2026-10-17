# CLAUDE.md

Home Assistant custom integration for Alliant Energy (electric + gas usage,
cost, and bill data). The integration lives under
`custom_components/alliant_energy`. See `README.md` for architecture,
`docs/backend-api.md` for the reverse-engineered backend, and `docs/adr/` for
decisions.

Checks: `uv run ruff check .`, `uv run ty check`, `uv run pytest`. Targets
current Home Assistant only (Python 3.14; ADR 0008).

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
