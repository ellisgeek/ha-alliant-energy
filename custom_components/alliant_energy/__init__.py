"""Alliant Energy integration setup."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback

from .bill import Bill, async_forget_bill
from .coordinator import AlliantEnergyConfigEntry, EnergyCoordinator
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

    async def _import_statistics(bill: Bill) -> None:
        """Import long-term statistics; runs off the setup and refresh path."""
        try:
            await client.async_update_statistics(bill)
        except AlliantEnergyAuthError:
            entry.async_start_reauth(hass)
        except Exception as err:  # noqa: BLE001 - best-effort, never crash the task
            _LOGGER.warning("Alliant Energy statistics import failed: %s", err)

    @callback
    def _start_import() -> None:
        """After each successful refresh, import statistics with its bill (ADR 0013).

        A long backfill can still be running at the next refresh; that refresh
        then imports nothing, and a later one continues from the last hour.
        """
        if not coordinator.last_update_success or coordinator.data is None:
            return
        running = coordinator.statistics_import
        if running is not None and not running.done():
            _LOGGER.debug("Statistics import still running; skipping this refresh")
            return
        coordinator.statistics_import = entry.async_create_background_task(
            hass, _import_statistics(coordinator.data.bill), "alliant_energy_statistics"
        )

    _start_import()  # the first refresh is already done
    entry.async_on_unload(coordinator.async_add_listener(_start_import))
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
