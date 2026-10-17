"""Tests for config entry setup and unload."""

from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.alliant_energy.models import EnergyData


def _client() -> AsyncMock:
    client = AsyncMock()
    client.async_fetch_data.return_value = EnergyData(
        electricity_kwh=12.5, amount_due=101.25, previous_balance=88.0, currency="USD"
    )
    return client


async def test_setup_and_unload(hass: HomeAssistant, config_entry) -> None:
    """Setup creates the sensors and starts statistics; unload closes the client."""
    client = _client()
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=client):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    amount_due = hass.states.get("sensor.alliant_energy_amount_due")
    previous_balance = hass.states.get("sensor.alliant_energy_previous_balance")
    assert amount_due is not None and amount_due.state == "101.25"
    assert previous_balance is not None and previous_balance.state == "88.0"
    client.async_update_statistics.assert_awaited_once()

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    client.async_close.assert_awaited_once()


async def test_setup_retries_and_closes_client_on_failure(
    hass: HomeAssistant, config_entry
) -> None:
    """A failed first refresh retries later and does not leak the HTTP session."""
    client = _client()
    client.async_fetch_data.side_effect = RuntimeError("down")
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=client):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    client.async_close.assert_awaited_once()
