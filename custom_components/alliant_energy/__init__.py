"""Alliant Energy integration setup."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .bill import async_forget_bill
from .coordinator import AlliantEnergyConfigEntry, EnergyCoordinator
from .provider import LocalUtilityClient

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: AlliantEnergyConfigEntry) -> bool:
    """Set up Alliant Energy from a config entry."""
    client = LocalUtilityClient(hass, entry)
    coordinator = EnergyCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await client.async_close()
        raise

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: AlliantEnergyConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AlliantEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = entry.runtime_data
        # Stop a running import before closing its session; otherwise it fails
        # mid-request and a backfill starts over after a reload.
        if (running := coordinator.statistics_import) is not None and not running.done():
            running.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await running
        await coordinator.client.async_close()
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: AlliantEnergyConfigEntry) -> None:
    """Delete the saved bill when the integration is removed."""
    await async_forget_bill(hass, entry.entry_id)
