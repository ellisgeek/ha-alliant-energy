# Support only current Home Assistant releases

Home Assistant changes its integration APIs often. Since 2024 it made
`OptionsFlow.config_entry` read-only (the old options flow crashed on open) and
deprecated `has_mean` in statistics metadata in favour of `mean_type` and
`unit_class`. Supporting old and new releases
together would need version checks throughout the code. The project instead
sets a recent minimum (`hacs.json` `homeassistant` key, Python 3.14 in
`pyproject.toml`) and tests against the matching
`pytest-homeassistant-custom-component` release.

## Consequences

Raise the minimum when an API changes, and do not add compatibility shims.
People on older Home Assistant releases must stay on an older integration
release.
