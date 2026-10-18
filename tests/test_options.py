"""Tests for reading the integration options."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.const import DOMAIN
from custom_components.alliant_energy.options import max_history_days, update_interval


def _entry(**options) -> MockConfigEntry:
    return MockConfigEntry(domain=DOMAIN, data={}, options=options)


def test_defaults() -> None:
    assert max_history_days(_entry()) == 3650
    assert update_interval(_entry()) == timedelta(hours=24)


def test_configured_values() -> None:
    entry = _entry(max_history_days=400, update_interval_hours=3)
    assert max_history_days(entry) == 400
    assert update_interval(entry) == timedelta(hours=3)


@pytest.mark.parametrize("value", [29, 3651, "400", None, True])
def test_invalid_history_falls_back_to_default(value) -> None:
    assert max_history_days(_entry(max_history_days=value)) == 3650


@pytest.mark.parametrize("value", [2, 25, "3", None, False])
def test_invalid_interval_falls_back_to_default(value) -> None:
    assert update_interval(_entry(update_interval_hours=value)) == timedelta(hours=24)
