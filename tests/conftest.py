"""Shared test fixtures."""

import pytest
from homeassistant.components.recorder import Recorder
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.const import (
    CONF_ACCOUNT_ID,
    CONF_PASSWORD,
    CONF_PREMISE_ID,
    CONF_USERNAME,
    DOMAIN,
)


@pytest.fixture(autouse=True)
def auto_recorder(recorder_mock: Recorder) -> None:
    """Run a real recorder in every test; the integration depends on it.

    Autouse so it is set up before ``hass``, as the recorder fixture requires.
    """


@pytest.fixture
def config_entry(hass: HomeAssistant, enable_custom_integrations: None) -> MockConfigEntry:
    """A config entry for one account, registered with Home Assistant.

    Enables custom integrations so flows the entry triggers (such as reauth)
    can load this integration.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="A1",
        data={
            CONF_USERNAME: "user@example.com",
            CONF_PASSWORD: "hunter2",
            CONF_ACCOUNT_ID: "A1",
            CONF_PREMISE_ID: "P1",
        },
    )
    entry.add_to_hass(hass)
    return entry
