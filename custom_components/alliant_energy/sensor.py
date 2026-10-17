"""Sensors for utility readings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import AlliantEnergyConfigEntry, EnergyCoordinator
from .models import EnergyData


@dataclass(frozen=True, kw_only=True)
class AlliantSensorDescription(SensorEntityDescription):
    """Describes a sensor and how to read its value from the coordinator data."""

    value_fn: Callable[[EnergyData], float | date | None]


# These are informational "latest reading" sensors. The data that feeds the
# Energy dashboard is imported as long-term statistics (see statistics.py), not
# from these entities, so they deliberately avoid energy/gas device classes
# (gas is reported in therms, which the gas device class rejects).
SENSOR_DESCRIPTIONS: tuple[AlliantSensorDescription, ...] = (
    AlliantSensorDescription(
        key="electricity_usage",
        name="Electricity usage (latest day)",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.electricity_kwh,
    ),
    AlliantSensorDescription(
        key="gas_usage",
        name="Gas usage (latest day)",
        native_unit_of_measurement="therm",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.gas_therms,
    ),
    AlliantSensorDescription(
        key="electricity_cost",
        name="Electricity last bill cost",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.electricity_cost,
    ),
    AlliantSensorDescription(
        key="gas_cost",
        name="Gas last bill cost",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.gas_cost,
    ),
    # Account-level bill summary.
    AlliantSensorDescription(
        key="amount_due",
        name="Amount due",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.amount_due,
    ),
    AlliantSensorDescription(
        key="current_charges",
        name="Current charges",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.current_charges,
    ),
    AlliantSensorDescription(
        key="past_due_amount",
        name="Past due amount",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.past_due_amount,
    ),
    AlliantSensorDescription(
        key="previous_balance",
        name="Previous balance",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.previous_balance,
    ),
    AlliantSensorDescription(
        key="due_date",
        name="Payment due date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.due_date,
    ),
    AlliantSensorDescription(
        key="invoice_date",
        name="Invoice date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.invoice_date,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: AlliantEnergyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up utility sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        UtilitySensor(coordinator, description, entry.entry_id)
        for description in SENSOR_DESCRIPTIONS
    )


class UtilitySensor(CoordinatorEntity[EnergyCoordinator], SensorEntity):
    """Represent one usage or cost reading."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: EnergyCoordinator,
        description: AlliantSensorDescription,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        # Typed handle on the description; HA declares entity_description as the
        # base SensorEntityDescription.
        self._description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)},
            name="Alliant Energy",
            manufacturer="Alliant Energy",
        )
        self._update_from_data()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh the reading when the coordinator has new data."""
        self._update_from_data()
        super()._handle_coordinator_update()

    def _update_from_data(self) -> None:
        data = self.coordinator.data
        self._attr_native_value = self._description.value_fn(data)
        # Monetary sensors use the utility currency; others use their own unit.
        if self._description.device_class == SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = data.currency
        else:
            self._attr_native_unit_of_measurement = self._description.native_unit_of_measurement
