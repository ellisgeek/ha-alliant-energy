"""Coordinator for utility readings."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .bill import Bill
from .const import DOMAIN
from .models import EnergyData
from .options import update_interval
from .provider import AlliantEnergyAuthError

if TYPE_CHECKING:
    from .provider import LocalUtilityClient

_LOGGER = logging.getLogger(__name__)

type AlliantEnergyConfigEntry = ConfigEntry[EnergyCoordinator]


class EnergyCoordinator(DataUpdateCoordinator[EnergyData]):
    """Fetch utility readings on a fixed schedule, and start statistics imports."""

    config_entry: AlliantEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AlliantEnergyConfigEntry,
        client: LocalUtilityClient,
    ) -> None:
        super().__init__(
            hass,
            logger=_LOGGER,
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=update_interval(config_entry),
        )
        self.client = client
        # The statistics import started by the latest refresh, if any.
        self.statistics_import: asyncio.Task[None] | None = None

    async def _async_update_data(self) -> EnergyData:
        """Fetch the latest readings, then start the statistics import (ADR 0013).

        The import starts after every refresh, priced with the bill this
        refresh fetched or, when it failed, with the last one fetched. So a
        broken sensor endpoint does not stop the Energy dashboard's usage.
        """
        try:
            data = await self.client.async_fetch_data()
        except AlliantEnergyAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            if self.data is not None:
                self._start_statistics_import(self.data.bill)
            raise UpdateFailed(f"Unable to fetch utility readings: {err}") from err
        self._start_statistics_import(data.bill)
        return data

    def _start_statistics_import(self, bill: Bill) -> None:
        """Import statistics in the background, unless an import is still running.

        A long backfill can still be running at the next refresh; that refresh
        then imports nothing, and a later one continues from the last hour.
        """
        if self.statistics_import is not None and not self.statistics_import.done():
            _LOGGER.debug("Statistics import still running; skipping this refresh")
            return
        self.statistics_import = self.config_entry.async_create_background_task(
            self.hass, self._async_import_statistics(bill), "alliant_energy_statistics"
        )

    async def _async_import_statistics(self, bill: Bill) -> None:
        try:
            await self.client.async_update_statistics(bill)
        except AlliantEnergyAuthError:
            self.config_entry.async_start_reauth(self.hass)
        except Exception as err:  # noqa: BLE001 - best-effort, never crash the task
            _LOGGER.warning("Alliant Energy statistics import failed: %s", err)
