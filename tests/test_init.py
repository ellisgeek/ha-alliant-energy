"""Tests for config entry setup and unload."""

from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

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


async def test_update_interval_option_applies_to_sensors_and_statistics(
    hass: HomeAssistant, config_entry
) -> None:
    hass.config_entries.async_update_entry(config_entry, options={"update_interval_hours": 3})
    client = _client()
    with (
        patch("custom_components.alliant_energy.LocalUtilityClient", return_value=client),
        patch(
            "custom_components.alliant_energy.async_track_time_interval",
            wraps=async_track_time_interval,
        ) as track,
    ):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.runtime_data.update_interval == timedelta(hours=3)
    assert track.call_args.args[2] == timedelta(hours=3)


async def test_update_interval_defaults_to_a_day(hass: HomeAssistant, config_entry) -> None:
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=_client()):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.runtime_data.update_interval == timedelta(hours=24)
