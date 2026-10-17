"""Tests for utility data coordination."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.api import (
    AlliantEnergyAuthError,
    AlliantEnergyError,
)
from custom_components.alliant_energy.const import DOMAIN
from custom_components.alliant_energy.coordinator import EnergyCoordinator
from custom_components.alliant_energy.models import EnergyData


async def test_coordinator_refreshes_provider_data(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Coordinator stores provider readings after a refresh."""
    client = AsyncMock()
    client.async_fetch_data.return_value = EnergyData(
        electricity_kwh=12.5,
        gas_therms=3.2,
        electricity_cost=4.1,
        gas_cost=1.8,
        currency="USD",
    )
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()

    assert coordinator.data.electricity_kwh == 12.5
    assert coordinator.data.gas_therms == 3.2
    client.async_fetch_data.assert_awaited_once()


async def test_coordinator_auth_error_becomes_auth_failed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Auth errors trigger Home Assistant reauth, not a transient failure."""
    client = AsyncMock()
    client.async_fetch_data.side_effect = AlliantEnergyAuthError("expired")
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_coordinator_api_error_becomes_update_failed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Non-auth API errors are reported as update failures."""
    client = AsyncMock()
    client.async_fetch_data.side_effect = AlliantEnergyError("boom")
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, UpdateFailed)
