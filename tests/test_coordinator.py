"""Tests for utility data coordination."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.api import (
    AlliantEnergyAccountLockedError,
    AlliantEnergyAuthError,
    AlliantEnergyError,
)
from custom_components.alliant_energy.const import DOMAIN
from custom_components.alliant_energy.coordinator import EnergyCoordinator
from custom_components.alliant_energy.models import EnergyData, ServiceData


async def test_coordinator_refreshes_provider_data(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Coordinator stores provider readings after a refresh."""
    client = AsyncMock()
    data = EnergyData(
        services={"electric": ServiceData(latest_usage=12.5), "gas": ServiceData(latest_usage=3.2)},
        currency="USD",
    )
    client.async_fetch_data.return_value = data
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()

    assert coordinator.data is data
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


async def test_coordinator_locked_account_prompts_for_reauthentication(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    client = AsyncMock()
    client.async_fetch_data.side_effect = AlliantEnergyAccountLockedError("locked")
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, ConfigEntryAuthFailed)
    await hass.async_block_till_done()
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_reauthentication_prompt_explains_a_refused_refresh(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    client = AsyncMock()
    client.async_fetch_data.side_effect = AlliantEnergyAccountLockedError(
        "locked", portal_message="Locked for 24 hours."
    )
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    form = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    placeholders = form["description_placeholders"]
    assert placeholders is not None
    assert placeholders["reason"] == "Locked for 24 hours."


async def test_reauthentication_prompt_explains_a_refused_import(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    client = AsyncMock()
    client.async_fetch_data.return_value = EnergyData(services={}, currency="USD")
    client.async_update_statistics.side_effect = AlliantEnergyAuthError(
        "rejected", portal_message="Password expired."
    )
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()
    assert coordinator.statistics_import is not None
    await coordinator.statistics_import
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    form = await hass.config_entries.flow.async_configure(flows[0]["flow_id"])
    placeholders = form["description_placeholders"]
    assert placeholders is not None
    assert placeholders["reason"] == "Password expired."


async def test_coordinator_api_error_becomes_update_failed(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Non-auth API errors are reported as update failures."""
    client = AsyncMock()
    client.async_fetch_data.side_effect = AlliantEnergyError("boom")
    coordinator = EnergyCoordinator(hass, config_entry, client)

    await coordinator.async_refresh()

    assert isinstance(coordinator.last_exception, UpdateFailed)
