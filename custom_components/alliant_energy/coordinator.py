"""Coordinator for utility readings."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, UPDATE_INTERVAL
from .models import EnergyData
from .provider import AlliantEnergyAuthError

if TYPE_CHECKING:
    from .provider import LocalUtilityClient

type AlliantEnergyConfigEntry = ConfigEntry[EnergyCoordinator]


class EnergyCoordinator(DataUpdateCoordinator[EnergyData]):
    """Fetch utility readings on a fixed schedule."""

    config_entry: AlliantEnergyConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: AlliantEnergyConfigEntry,
        client: LocalUtilityClient,
    ) -> None:
        super().__init__(
            hass,
            logger=logging.getLogger(__name__),
            config_entry=config_entry,
            name=DOMAIN,
            update_interval=UPDATE_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> EnergyData:
        """Fetch the latest readings from the utility client."""
        try:
            return await self.client.async_fetch_data()
        except AlliantEnergyAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except Exception as err:
            raise UpdateFailed(f"Unable to fetch utility readings: {err}") from err
