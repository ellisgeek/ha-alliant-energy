"""Alliant Energy integration setup."""

from __future__ import annotations

import logging
from datetime import datetime

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval

from .coordinator import AlliantEnergyConfigEntry, EnergyCoordinator
from .options import update_interval
from .provider import AlliantEnergyAuthError, LocalUtilityClient

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

    async def _import_statistics(_now: datetime | None = None) -> None:
        """Import long-term statistics; runs off the setup path."""
        try:
            await client.async_update_statistics()
        except AlliantEnergyAuthError:
            entry.async_start_reauth(hass)
        except Exception as err:  # noqa: BLE001 - best-effort, never crash the task
            _LOGGER.warning("Alliant Energy statistics import failed: %s", err)

    # Backfill history now (in the background so setup stays fast), then refresh
    # on the normal interval.
    entry.async_create_background_task(
        hass, _import_statistics(), "alliant_energy_statistics_initial"
    )
    entry.async_on_unload(
        async_track_time_interval(hass, _import_statistics, update_interval(entry))
    )
    entry.async_on_unload(entry.add_update_listener(_async_reload_on_options))
    return True


async def _async_reload_on_options(hass: HomeAssistant, entry: AlliantEnergyConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: AlliantEnergyConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.async_close()
    return unloaded
