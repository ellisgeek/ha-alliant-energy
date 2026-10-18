"""Sensors for utility readings."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy, UnitOfTemperature, UnitOfTime, UnitOfVolume
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import SERVICE_ELECTRIC, SERVICE_GAS
from .const import DOMAIN, USAGE_UNIT
from .coordinator import AlliantEnergyConfigEntry, EnergyCoordinator
from .models import EnergyData


@dataclass(frozen=True, kw_only=True)
class AlliantSensorDescription(SensorEntityDescription):
    """Describes a sensor and how to read its value from the coordinator data."""

    value_fn: Callable[[EnergyData], float | date | str | None]
    # Unit computed from the data (e.g. "USD/kWh"); overrides the static unit.
    unit_fn: Callable[[EnergyData], str | None] | None = None
    attrs_fn: Callable[[EnergyData], dict[str, Any]] | None = None


def _price_unit(data: EnergyData, service: str) -> str | None:
    return f"{data.currency}/{USAGE_UNIT[service]}" if data.currency else None


@dataclass(frozen=True)
class _Service:
    """How one service's sensors are keyed and named."""

    service: str
    key: str  # unique id prefix, e.g. "electricity"
    name: str  # e.g. "Electricity"
    meter_unit: str  # how the meter registers when the bill doesn't say
    adjustment_key: str
    adjustment_name: str


_ELECTRIC = _Service(
    SERVICE_ELECTRIC,
    "electricity",
    "Electricity",
    meter_unit=UnitOfEnergy.KILO_WATT_HOUR,
    adjustment_key="fuel_adjustment",
    adjustment_name="fuel adjustment",
)
_GAS = _Service(
    SERVICE_GAS,
    "gas",
    "Gas",
    meter_unit=UnitOfVolume.CENTUM_CUBIC_FEET,
    adjustment_key="supply_market_adjustment",
    adjustment_name="supply market adjustment",
)


# Home Assistant units for the meter units a bill shows.
_METER_UNITS: dict[str | None, str] = {
    "kWh": UnitOfEnergy.KILO_WATT_HOUR,
    "CCF": UnitOfVolume.CENTUM_CUBIC_FEET,
    "therm": UnitOfEnergy.THERM,
}


def _section_value(data: EnergyData, service: str, attr: str) -> Any:
    section = data.service(service).section
    return getattr(section, attr) if section else None


def _service_sensors(svc: _Service) -> tuple[AlliantSensorDescription, ...]:
    """The sensors every service has, keyed and named for this one."""
    s = svc.service
    return (
        # Informational "latest reading" sensors. The data that feeds the
        # Energy dashboard is imported as long-term statistics (see
        # statistics.py), so they deliberately avoid energy/gas device classes.
        AlliantSensorDescription(
            key=f"{svc.key}_usage",
            name=f"{svc.name} usage (latest day)",
            native_unit_of_measurement=USAGE_UNIT[svc.service],
            state_class=SensorStateClass.MEASUREMENT,
            value_fn=lambda data: data.service(s).latest_usage,
        ),
        AlliantSensorDescription(
            key=f"{svc.key}_cost",
            name=f"{svc.name} last bill cost",
            device_class=SensorDeviceClass.MONETARY,
            value_fn=lambda data: data.service(s).last_bill_cost,
        ),
        AlliantSensorDescription(
            key=f"{svc.key}_projected_cost",
            name=f"{svc.name} projected cost",
            device_class=SensorDeviceClass.MONETARY,
            suggested_display_precision=2,
            value_fn=lambda data: data.service(s).projected_cost,
            attrs_fn=lambda data: {
                "usage_to_date": data.service(s).usage_to_date,
                "cost_to_date": data.service(s).cost_to_date,
                "days_to_date": data.service(s).days_to_date,
                "cycle_days": data.service(s).cycle_days,
            },
        ),
        # Rate used for cost (options, bill PDF, or latest billing cycle) and
        # the rate schedule from the bill.
        AlliantSensorDescription(
            key=f"{svc.key}_rate",
            name=f"{svc.name} rate",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=5,
            value_fn=lambda data: data.service(s).rate,
            unit_fn=lambda data: _price_unit(data, s),
            attrs_fn=lambda data: {
                "source": data.service(s).rate_source,
                "rate_plan": _section_value(data, s, "rate_plan"),
                "customer_charge_per_day": _section_value(data, s, "customer_charge_per_day"),
            },
        ),
        AlliantSensorDescription(
            key=f"{svc.key}_rate_plan",
            name=f"{svc.name} rate plan",
            value_fn=lambda data: _section_value(data, s, "rate_plan"),
            attrs_fn=lambda data: {"name": _section_value(data, s, "rate_plan_name")},
        ),
        # A charge that changes from bill to bill.
        AlliantSensorDescription(
            key=f"{svc.key}_{svc.adjustment_key}",
            name=f"{svc.name} {svc.adjustment_name}",
            state_class=SensorStateClass.MEASUREMENT,
            suggested_display_precision=5,
            value_fn=lambda data: data.service(s).adjustment,
            unit_fn=lambda data: _price_unit(data, s),
        ),
        # The meter register reading from the bill. No energy/gas device class
        # or state class: like the usage sensors, not an Energy dashboard source.
        AlliantSensorDescription(
            key=f"{svc.key}_meter_reading",
            name=f"{svc.name} meter reading",
            value_fn=lambda data: _section_value(data, s, "meter_reading"),
            unit_fn=lambda data: _METER_UNITS.get(
                _section_value(data, s, "meter_unit"), svc.meter_unit
            ),
            attrs_fn=lambda data: {
                "meter_number": _section_value(data, s, "meter_number"),
                "previous_reading": _section_value(data, s, "previous_meter_reading"),
                "read_date": data.billing_period.period_end if data.billing_period else None,
            },
        ),
    )


def _period(attr: str) -> Callable[[EnergyData], Any]:
    return lambda data: getattr(data.billing_period, attr) if data.billing_period else None


def _itemized(attr: str) -> Callable[[EnergyData], Any]:
    return lambda data: getattr(data.bill.itemized, attr) if data.bill.itemized else None


SENSOR_DESCRIPTIONS: tuple[AlliantSensorDescription, ...] = (
    *_service_sensors(_ELECTRIC),
    *_service_sensors(_GAS),
    AlliantSensorDescription(
        key="gas_heat_factor",
        name="Gas heat factor",
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda data: _section_value(data, SERVICE_GAS, "heat_factor"),
    ),
    # Account-level bill summary.
    AlliantSensorDescription(
        key="amount_due",
        name="Amount due",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.bill.summary.amount_due,
    ),
    AlliantSensorDescription(
        key="current_charges",
        name="Current charges",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.bill.summary.current_charges,
    ),
    AlliantSensorDescription(
        key="past_due_amount",
        name="Past due amount",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.bill.summary.past_due_amount,
    ),
    AlliantSensorDescription(
        key="previous_balance",
        name="Previous balance",
        device_class=SensorDeviceClass.MONETARY,
        value_fn=lambda data: data.bill.summary.previous_balance,
    ),
    AlliantSensorDescription(
        key="due_date",
        name="Payment due date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.bill.summary.due_date,
    ),
    AlliantSensorDescription(
        key="invoice_date",
        name="Invoice date",
        device_class=SensorDeviceClass.DATE,
        value_fn=lambda data: data.bill.summary.invoice_date,
    ),
    AlliantSensorDescription(
        key="projected_bill",
        name="Projected bill",
        device_class=SensorDeviceClass.MONETARY,
        suggested_display_precision=2,
        value_fn=lambda data: data.projected_bill,
        attrs_fn=lambda data: {
            "electric": data.service(SERVICE_ELECTRIC).projected_cost,
            "gas": data.service(SERVICE_GAS).projected_cost,
        },
    ),
    # Billing cycle and weather, from the itemized bill.
    AlliantSensorDescription(
        key="billing_period_start",
        name="Billing period start",
        device_class=SensorDeviceClass.DATE,
        value_fn=_period("period_start"),
    ),
    AlliantSensorDescription(
        key="billing_period_end",
        name="Billing period end",
        device_class=SensorDeviceClass.DATE,
        value_fn=_period("period_end"),
    ),
    AlliantSensorDescription(
        key="billed_days",
        name="Days billed",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.DAYS,
        value_fn=_period("billed_days"),
    ),
    AlliantSensorDescription(
        key="next_meter_read",
        name="Next meter reading",
        device_class=SensorDeviceClass.DATE,
        value_fn=_itemized("next_read_start"),
        attrs_fn=lambda data: {"window_end": _itemized("next_read_end")(data)},
    ),
    AlliantSensorDescription(
        key="average_temperature",
        name="Billing period average temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.FAHRENHEIT,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_itemized("average_temperature"),
    ),
    AlliantSensorDescription(
        key="degree_days",
        name="Billing period degree days",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_itemized("degree_days"),
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
        if self._description.unit_fn is not None:
            self._attr_native_unit_of_measurement = self._description.unit_fn(data)
        elif self._description.device_class == SensorDeviceClass.MONETARY:
            self._attr_native_unit_of_measurement = data.currency
        else:
            self._attr_native_unit_of_measurement = self._description.native_unit_of_measurement
        if self._description.attrs_fn is not None:
            self._attr_extra_state_attributes = self._description.attrs_fn(data)
