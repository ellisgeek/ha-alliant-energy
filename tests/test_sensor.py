"""Tests for utility sensors."""

from unittest.mock import AsyncMock

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.coordinator import EnergyCoordinator
from custom_components.alliant_energy.models import EnergyData, ServiceData
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


async def test_rate_and_rate_plan_sensors(
    hass: HomeAssistant, config_entry: MockConfigEntry
) -> None:
    """Rate sensors show $/unit with their source; plan sensors show the schedule."""
    coordinator = EnergyCoordinator(hass, config_entry, AsyncMock())
    coordinator.data = EnergyData(
        currency="USD",
        services={
            "electric": ServiceData(
                rate=0.16223,
                rate_source="bill",
                rate_plan="RG1",
                rate_plan_name="Residential Electric Service",
                customer_charge_per_day=0.526,
            ),
            "gas": ServiceData(rate=1.1, rate_source="options"),
        },
    )
    by_key = {d.key: d for d in SENSOR_DESCRIPTIONS}

    electric = UtilitySensor(coordinator, by_key["electricity_rate"], "e")
    assert electric.native_value == 0.16223
    assert electric.native_unit_of_measurement == "USD/kWh"
    assert electric.extra_state_attributes == {
        "source": "bill",
        "rate_plan": "RG1",
        "customer_charge_per_day": 0.526,
    }

    gas = UtilitySensor(coordinator, by_key["gas_rate"], "e")
    assert gas.native_value == 1.1
    assert gas.native_unit_of_measurement == "USD/thm"

    plan = UtilitySensor(coordinator, by_key["electricity_rate_plan"], "e")
    assert plan.native_value == "RG1"
    assert plan.extra_state_attributes == {"name": "Residential Electric Service"}

    gas_plan = UtilitySensor(coordinator, by_key["gas_rate_plan"], "e")
    assert gas_plan.native_value is None  # not on the bill


async def test_bill_detail_sensors(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """Projection, meter, adjustment, cycle, and weather sensors."""
    from datetime import date

    coordinator = EnergyCoordinator(hass, config_entry, AsyncMock())
    coordinator.data = EnergyData(
        currency="USD",
        services={
            "electric": ServiceData(
                adjustment=-0.00788,
                meter_number="300000003",
                meter_reading=70190,
                previous_meter_reading=69851,
                projected_cost=62.3,
                usage_to_date=30,
                cost_to_date=6.44,
                days_to_date=3,
                cycle_days=29,
            ),
            "gas": ServiceData(adjustment=-0.471793, meter_reading=6207, heat_factor=1.06),
        },
        projected_bill=62.3,
        billing_period_end=date(2026, 10, 12),
        billed_days=31,
        next_meter_read_start=date(2026, 11, 9),
        next_meter_read_end=date(2026, 11, 12),
        average_temperature=60,
        degree_days=157,
    )
    by_key = {d.key: d for d in SENSOR_DESCRIPTIONS}

    def sensor(key):
        return UtilitySensor(coordinator, by_key[key], "e")

    bill = sensor("projected_bill")
    assert (bill.native_value, bill.native_unit_of_measurement) == (62.3, "USD")
    assert bill.extra_state_attributes == {"electric": 62.3, "gas": None}

    projected = sensor("electricity_projected_cost")
    assert projected.extra_state_attributes == {
        "usage_to_date": 30,
        "cost_to_date": 6.44,
        "days_to_date": 3,
        "cycle_days": 29,
    }

    meter = sensor("electricity_meter_reading")
    assert (meter.native_value, meter.native_unit_of_measurement) == (70190, "kWh")
    assert meter.extra_state_attributes == {
        "meter_number": "300000003",
        "previous_reading": 69851,
        "read_date": date(2026, 10, 12),
    }
    assert sensor("gas_meter_reading").native_unit_of_measurement == "CCF"
    assert sensor("gas_heat_factor").native_value == 1.06

    fuel = sensor("electricity_fuel_adjustment")
    assert (fuel.native_value, fuel.native_unit_of_measurement) == (-0.00788, "USD/kWh")
    assert sensor("gas_supply_market_adjustment").native_unit_of_measurement == "USD/thm"

    read = sensor("next_meter_read")
    assert read.native_value == date(2026, 11, 9)
    assert read.extra_state_attributes == {"window_end": date(2026, 11, 12)}
    assert sensor("billed_days").native_value == 31
    assert sensor("average_temperature").native_unit_of_measurement == "°F"
    assert sensor("degree_days").native_value == 157
