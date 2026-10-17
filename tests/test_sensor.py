"""Tests for utility sensors."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.coordinator import EnergyCoordinator
from custom_components.alliant_energy.models import EnergyData
from custom_components.alliant_energy.sensor import SENSOR_DESCRIPTIONS, UtilitySensor


async def test_usage_sensor_exposes_reading(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Usage sensors expose the corresponding provider value and unit."""
    coordinator = EnergyCoordinator(hass, config_entry, AsyncMock())
    coordinator.data = EnergyData(electricity_kwh=18.4)
    sensor = UtilitySensor(coordinator, SENSOR_DESCRIPTIONS[0], "entry-123")

    assert sensor.native_value == 18.4
    assert sensor.native_unit_of_measurement == "kWh"
    assert sensor.unique_id == "entry-123_electricity_usage"


async def test_gas_sensor_reports_therms(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Gas usage is reported in therms."""
    coordinator = EnergyCoordinator(hass, config_entry, AsyncMock())
    coordinator.data = EnergyData(gas_therms=5.0)
    sensor = UtilitySensor(coordinator, SENSOR_DESCRIPTIONS[1], "entry-123")

    assert sensor.native_value == 5.0
    assert sensor.native_unit_of_measurement == "therm"


async def test_bill_sensors_expose_amount_and_due_date(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Bill summary sensors expose amount due (currency) and due date (date)."""
    from datetime import date

    coordinator = EnergyCoordinator(hass, config_entry, AsyncMock())
    coordinator.data = EnergyData(amount_due=117.74, currency="USD", due_date=date(2026, 11, 2))
    by_key = {d.key: d for d in SENSOR_DESCRIPTIONS}

    amount = UtilitySensor(coordinator, by_key["amount_due"], "e")
    assert amount.native_value == 117.74
    assert amount.native_unit_of_measurement == "USD"

    due = UtilitySensor(coordinator, by_key["due_date"], "e")
    assert due.native_value == date(2026, 11, 2)
    assert due.native_unit_of_measurement is None


async def test_cost_sensor_uses_provider_currency(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Cost sensor unit follows the currency reported by the provider."""
    coordinator = EnergyCoordinator(hass, config_entry, AsyncMock())
    coordinator.data = EnergyData(electricity_cost=7.25, currency="USD")
    sensor = UtilitySensor(coordinator, SENSOR_DESCRIPTIONS[2], "entry-123")

    assert sensor.native_value == 7.25
    assert sensor.native_unit_of_measurement == "USD"
