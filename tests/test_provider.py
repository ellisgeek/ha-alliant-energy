"""Tests for the Home Assistant-facing provider client."""

import asyncio
from dataclasses import replace
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.api import (
    PERIODICITY_DAILY,
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    Account,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    BillSummary,
    Meter,
    UsageReading,
)
from custom_components.alliant_energy.bill import Bill, PastBills
from custom_components.alliant_energy.billparser import (
    PARSER_VERSION,
    ItemizedBill,
    ServiceBill,
    bill_to_dict,
)
from custom_components.alliant_energy.const import DOMAIN
from custom_components.alliant_energy.coordinator import EnergyCoordinator
from custom_components.alliant_energy.models import ServiceData
from custom_components.alliant_energy.provider import LocalUtilityClient
from custom_components.alliant_energy.rates import KnownRates, MarginalRate
from custom_components.alliant_energy.sensor import SENSOR_DESCRIPTIONS, UtilitySensor
from custom_components.alliant_energy.usage import AlliantUsage


@pytest.fixture
def api():
    """Replace the real curl_cffi-backed client and the statistics import."""
    instance = MagicMock()
    instance.async_login = AsyncMock()
    instance.async_get_accounts = AsyncMock(return_value=[])
    instance.async_get_meters = AsyncMock(return_value=[])
    instance.async_get_usage = AsyncMock(return_value=[])
    instance.async_get_current_bill = AsyncMock(return_value=BillSummary())
    instance.async_get_bill_pdf = AsyncMock(return_value=None)
    instance.close = AsyncMock()
    with (
        patch(
            "custom_components.alliant_energy.provider.AlliantEnergyApi",
            return_value=instance,
        ),
        patch(
            "custom_components.alliant_energy.provider.stats.async_update_statistics",
            new=AsyncMock(),
        ) as update_stats,
    ):
        instance.update_statistics = update_stats
        yield instance


def _entry(options=None, **extra):
    data = {"username": "u", "password": "p", "account_id": "acct-1", **extra}
    return MockConfigEntry(domain=DOMAIN, data=data, options=options or {})


# A parsed bill for both services.
BILL = ItemizedBill(
    electric=ServiceBill(
        marginal_rate=0.16223,
        customer_charge_per_day=0.526,
        components={"energy_charge": 0.17011, "fuel_adjustment": -0.00788},
        rate_plan="RG1",
        rate_plan_name="Residential Electric Service",
        period_start=date(2026, 9, 11),
        period_end=date(2026, 10, 12),
        billed_days=31,
        meter_number="300000003",
        meter_reading=70190,
        previous_meter_reading=69851,
        meter_unit="kWh",
    ),
    gas=ServiceBill(
        marginal_rate=0.508507,
        customer_charge_per_day=0.4603,
        components={"supply_market_adj": -0.471793},
        rate_plan="GG1",
        rate_plan_name="Residential Gas Service",
        period_end=date(2026, 10, 12),
        meter_reading=6207,
        meter_unit="CCF",
        heat_factor=1.06,
    ),
    bill_date=date(2026, 10, 13),
    next_read_start=date(2026, 11, 9),
    next_read_end=date(2026, 11, 12),
    average_temperature=60,
    degree_days=157,
)


def _with_bill(api, hass_storage, client: LocalUtilityClient, bill: ItemizedBill = BILL) -> None:
    """Serve invoice INV1, already read and saved as ``bill`` (no PDF download)."""
    api.async_get_current_bill.return_value = BillSummary(invoice_id="INV1")
    key = f"alliant_energy.bill.{client.entry.entry_id}"
    hass_storage[key] = {
        "version": 1,
        "key": key,
        "data": {
            "parser_version": PARSER_VERSION,
            "invoice_id": "INV1",
            "bill": bill_to_dict(bill),
        },
    }


def _daily(usage, date):
    return UsageReading(
        usage=usage, cost=0.0, reading_date=date, reading_from=None, reading_to=None, unit=None
    )


def _monthly(cost, period_to, period_from=None):
    """A billing cycle of 100 units ending at ``period_to`` (a month long by default)."""
    if period_from is None:
        end = date.fromisoformat(period_to[:10])
        period_from = f"{end.replace(month=end.month - 1).isoformat()}T00:00:00"
    return UsageReading(
        usage=100.0,
        cost=cost,
        reading_date="0001-01-01T01:00:00",
        reading_from=period_from,
        reading_to=period_to,
        unit=None,
    )


def _usage_router(daily_by_service, monthly_by_service):
    async def _get(*, service_type, periodicity=PERIODICITY_DAILY, **_request):
        if periodicity == PERIODICITY_DAILY:
            return daily_by_service.get(service_type, [])
        return monthly_by_service.get(service_type, [])

    return _get


async def test_fetch_data_picks_latest_usage_and_bill_cost(hass: HomeAssistant, api) -> None:
    """Latest daily usage per fuel and most-recent electric bill cost are returned."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [
        Meter("E1", "P1", SERVICE_ELECTRIC, {}),
        Meter("G1", "P1", SERVICE_GAS, {}),
    ]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={
            SERVICE_ELECTRIC: [
                _daily(10.0, "2026-10-13T00:00:00"),
                _daily(11.0, "2026-10-15T00:00:00"),  # newest
            ],
            SERVICE_GAS: [_daily(4.0, "2026-10-14T00:00:00")],
        },
        monthly_by_service={
            SERVICE_ELECTRIC: [
                _monthly(22.0, "2026-09-11T00:00:00"),
                _monthly(75.0, "2026-10-12T00:00:00"),  # newest period
            ],
            SERVICE_GAS: [],  # gas monthly is empty upstream
        },
    )

    data = await client.async_fetch_data()

    electric, gas = data.services[SERVICE_ELECTRIC], data.services[SERVICE_GAS]
    assert electric.latest_usage == 11.0
    assert gas.latest_usage == 4.0
    assert electric.last_bill_cost == 75.0
    assert gas.last_bill_cost is None
    assert data.currency == "USD"


async def test_update_statistics_delegates_to_importer(hass: HomeAssistant, api) -> None:
    """The statistics method logs in, resolves context, and imports stats."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    meters = [Meter("E1", "P1", SERVICE_ELECTRIC, {})]
    api.async_get_meters.return_value = meters

    await client.async_update_statistics(Bill(BillSummary()))

    api.update_statistics.assert_awaited_once()
    # Called with (hass, usage, account, meters).
    args = api.update_statistics.await_args[0]
    assert isinstance(args[1], AlliantUsage)
    assert args[2] == "acct-1"
    assert args[3] == meters


async def test_premise_is_discovered_when_not_configured(hass: HomeAssistant, api) -> None:
    """Without a stored premise, it is resolved from the account list."""
    client = LocalUtilityClient(hass, _entry())  # no premise_id
    api.async_get_accounts.return_value = [
        Account("other", "P0", {}),
        Account("acct-1", "P9", {}),
    ]

    await client.async_fetch_data()

    api.async_get_accounts.assert_awaited_once()
    api.async_get_meters.assert_awaited_once_with("acct-1", "P9")


async def test_configured_premise_skips_discovery(hass: HomeAssistant, api) -> None:
    """A stored premise is used directly without an account lookup."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))

    await client.async_fetch_data()

    api.async_get_accounts.assert_not_awaited()
    api.async_get_meters.assert_awaited_once_with("acct-1", "P1")


async def test_close_releases_session(hass: HomeAssistant, api) -> None:
    """Closing the client closes the underlying session."""
    client = LocalUtilityClient(hass, _entry())

    await client.async_close()

    api.close.assert_awaited_once()


async def test_rates_prefer_options_then_bill(hass: HomeAssistant, api, hass_storage) -> None:
    """A fixed rate from options wins; otherwise the bill's marginal rate is used."""
    client = LocalUtilityClient(hass, _entry(options={"electricity_rate": 0.2}, premise_id="P1"))
    _with_bill(api, hass_storage, client)
    data = await client.async_fetch_data()

    electric = data.services[SERVICE_ELECTRIC]
    assert (electric.rate, electric.rate_source) == (0.2, "options")
    assert electric.section is not None
    assert electric.section.rate_plan == "RG1"
    assert electric.section.rate_plan_name == "Residential Electric Service"
    assert electric.section.customer_charge_per_day == 0.526
    assert data.services[SERVICE_GAS].rate == pytest.approx(0.508507)
    assert data.services[SERVICE_GAS].rate_source == "bill"
    assert data.services[SERVICE_GAS].section == BILL.gas
    # The bill summary is reused; bill/Current is requested once per refresh.
    api.async_get_current_bill.assert_awaited_once()


async def test_rate_falls_back_to_latest_billing_cycle(hass: HomeAssistant, api) -> None:
    """Without options or a readable bill, the latest cycle's amount/usage is shown."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [Meter("E1", "P1", SERVICE_ELECTRIC, {})]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={},
        monthly_by_service={SERVICE_ELECTRIC: [_monthly(22.0, "2026-10-12T00:00:00")]},
    )

    data = await client.async_fetch_data()

    assert data.services[SERVICE_ELECTRIC].rate == pytest.approx(0.22)  # $22 / 100 kWh
    assert data.services[SERVICE_ELECTRIC].rate_source == "billing_cycle"
    assert data.services[SERVICE_ELECTRIC].section is None
    assert data.services[SERVICE_GAS] == ServiceData()


async def test_rate_skips_a_billing_cycle_with_cost_zero(hass: HomeAssistant, api) -> None:
    """Cost 0 means not billed yet: the rate comes from the cycle before, as in statistics."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [Meter("E1", "P1", SERVICE_ELECTRIC, {})]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={},
        monthly_by_service={
            SERVICE_ELECTRIC: [
                _monthly(22.0, "2026-09-11T00:00:00"),
                _monthly(0.0, "2026-10-12T00:00:00"),  # newest, not billed yet
            ]
        },
    )

    data = await client.async_fetch_data()

    assert data.services[SERVICE_ELECTRIC].rate == pytest.approx(0.22)
    assert data.services[SERVICE_ELECTRIC].rate_source == "billing_cycle"


async def test_no_rate_without_options_bill_or_billing_cycles(hass: HomeAssistant, api) -> None:
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [Meter("E1", "P1", SERVICE_ELECTRIC, {})]

    data = await client.async_fetch_data()

    assert data.services[SERVICE_ELECTRIC].rate is None
    assert data.services[SERVICE_ELECTRIC].rate_source is None


async def test_bill_without_a_period_still_shows_the_marginal_rate(
    hass: HomeAssistant, api, hass_storage
) -> None:
    """Gas in the test bill has no period start; its marginal rate is current."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    _with_bill(api, hass_storage, client)
    data = await client.async_fetch_data()

    gas = data.services[SERVICE_GAS]
    assert (gas.rate, gas.rate_source) == (pytest.approx(0.508507), "bill")


async def test_statistics_use_the_rates_of_the_bill_they_are_given(
    hass: HomeAssistant, api
) -> None:
    """The refresh's bill prices the import; the bill is not fetched again."""
    client = LocalUtilityClient(hass, _entry(options={"gas_rate": 1.1}, premise_id="P1"))
    await client.async_update_statistics(Bill(BillSummary(invoice_id="INV1"), BILL))

    api.async_get_current_bill.assert_not_awaited()

    kwargs = api.update_statistics.await_args.kwargs
    assert kwargs["rates"][SERVICE_ELECTRIC] == KnownRates(
        static=None, marginals=(MarginalRate(0.16223, date(2026, 9, 11)),)
    )
    assert kwargs["rates"][SERVICE_GAS] == KnownRates(
        static=1.1, marginals=(MarginalRate(0.508507, None),)
    )
    assert isinstance(kwargs["past_bills"], PastBills)  # for older billing cycles


async def test_bill_details_and_projection(hass: HomeAssistant, api, hass_storage) -> None:
    """Bill details are exposed and the cycle in progress is projected."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [Meter("E1", "P1", SERVICE_ELECTRIC, {})]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={
            SERVICE_ELECTRIC: [
                _daily(50.0, "2026-10-11T00:00:00"),  # before the cycle: ignored
                _daily(10.0, "2026-10-12T00:00:00"),  # read day starts the cycle
                _daily(12.0, "2026-10-13T00:00:00"),
                _daily(8.0, "2026-10-14T00:00:00"),
            ]
        },
        monthly_by_service={},
    )
    _with_bill(api, hass_storage, client)
    data = await client.async_fetch_data()

    electric = data.services[SERVICE_ELECTRIC]
    assert electric.adjustment == pytest.approx(-0.00788)
    assert electric.section == BILL.electric
    # Cycle Oct 12 -> Nov 10 (middle of the Nov 9-12 read window): 29 days.
    # 30 kWh in 3 days -> 290 kWh x 0.16223 + 29 x 0.526 customer charge.
    assert (electric.usage_to_date, electric.days_to_date, electric.cycle_days) == (30, 3, 29)
    assert electric.cost_to_date == pytest.approx(6.44)
    assert electric.projected_cost == pytest.approx(62.30)
    assert data.projected_bill == pytest.approx(62.30)  # no gas usage data

    gas = data.services[SERVICE_GAS]
    assert gas.section == BILL.gas
    assert gas.adjustment == pytest.approx(-0.471793)
    assert gas.projected_cost is None

    assert data.billing_period == BILL.electric
    assert data.bill.itemized == BILL


async def test_sensor_refresh_fails_when_the_bill_summary_fails(hass: HomeAssistant, api) -> None:
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_current_bill.side_effect = AlliantEnergyError("HTTP 500")

    with pytest.raises(AlliantEnergyError):
        await client.async_fetch_data()


async def _all_sensors(hass: HomeAssistant, api, hass_storage) -> dict[str, tuple]:
    """(value, unit, attributes) of every sensor after a full refresh."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [
        Meter("E1", "P1", SERVICE_ELECTRIC, {}),
        Meter("G1", "P1", SERVICE_GAS, {}),
    ]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={
            SERVICE_ELECTRIC: [
                _daily(10.0, "2026-10-12T00:00:00"),
                _daily(12.0, "2026-10-13T00:00:00"),
            ],
            SERVICE_GAS: [_daily(2.0, "2026-10-13T00:00:00")],
        },
        monthly_by_service={
            SERVICE_ELECTRIC: [_monthly(22.0, "2026-09-11T00:00:00")],
            SERVICE_GAS: [_monthly(90.0, "2026-09-11T00:00:00")],
        },
    )
    _with_bill(api, hass_storage, client)
    api.async_get_current_bill.return_value = BillSummary(
        amount_due=117.74,
        current_charges=110.0,
        past_due_amount=0.0,
        previous_balance=7.74,
        due_date=date(2026, 11, 2),
        invoice_date=date(2026, 10, 13),
        invoice_id="INV1",
    )
    coordinator = EnergyCoordinator(hass, client.entry, AsyncMock())
    coordinator.data = await client.async_fetch_data()
    sensors = {}
    for description in SENSOR_DESCRIPTIONS:
        sensor = UtilitySensor(coordinator, description, "e")
        sensors[description.key] = (
            sensor.native_value,
            sensor.native_unit_of_measurement,
            sensor.extra_state_attributes,
        )
    return sensors


async def test_every_sensor_after_a_full_refresh(hass: HomeAssistant, api, hass_storage) -> None:
    """Characterizes every sensor; reshaping the data must not change what they show."""
    sensors = await _all_sensors(hass, api, hass_storage)

    assert sensors == {
        "electricity_usage": (12.0, "kWh", None),
        "gas_usage": (2.0, "thm", None),
        "electricity_cost": (22.0, "USD", None),
        "gas_cost": (90.0, "USD", None),
        "amount_due": (117.74, "USD", None),
        "current_charges": (110.0, "USD", None),
        "past_due_amount": (0.0, "USD", None),
        "previous_balance": (7.74, "USD", None),
        "projected_bill": (109.85, "USD", {"electric": 67.01, "gas": 42.84}),
        "electricity_projected_cost": (
            67.01,
            "USD",
            {"usage_to_date": 22.0, "cost_to_date": 4.62, "days_to_date": 2, "cycle_days": 29},
        ),
        "gas_projected_cost": (
            42.84,
            "USD",
            {"usage_to_date": 2.0, "cost_to_date": 1.48, "days_to_date": 1, "cycle_days": 29},
        ),
        "electricity_rate": (
            0.16223,
            "USD/kWh",
            {"source": "bill", "rate_plan": "RG1", "customer_charge_per_day": 0.526},
        ),
        "gas_rate": (
            0.508507,
            "USD/thm",
            {"source": "bill", "rate_plan": "GG1", "customer_charge_per_day": 0.4603},
        ),
        "electricity_rate_plan": ("RG1", None, {"name": "Residential Electric Service"}),
        "gas_rate_plan": ("GG1", None, {"name": "Residential Gas Service"}),
        "electricity_fuel_adjustment": (-0.00788, "USD/kWh", None),
        "gas_supply_market_adjustment": (-0.471793, "USD/thm", None),
        "electricity_meter_reading": (
            70190,
            "kWh",
            {
                "meter_number": "300000003",
                "previous_reading": 69851,
                "read_date": date(2026, 10, 12),
            },
        ),
        "gas_meter_reading": (
            6207,
            "CCF",
            {"meter_number": None, "previous_reading": None, "read_date": date(2026, 10, 12)},
        ),
        "gas_heat_factor": (1.06, None, None),
        "billing_period_start": (date(2026, 9, 11), None, None),
        "billing_period_end": (date(2026, 10, 12), None, None),
        "billed_days": (31, "d", None),
        "next_meter_read": (date(2026, 11, 9), None, {"window_end": date(2026, 11, 12)}),
        "average_temperature": (60, "°F", None),
        "degree_days": (157, None, None),
        "due_date": (date(2026, 11, 2), None, None),
        "invoice_date": (date(2026, 10, 13), None, None),
    }


async def test_every_sensor_for_a_gas_only_account_without_a_bill(hass: HomeAssistant, api) -> None:
    """Electric sensors stay empty and gas sensors read gas, with no bill at all."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [Meter("G1", "P1", SERVICE_GAS, {})]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={SERVICE_GAS: [_daily(2.0, "2026-10-13T00:00:00")]},
        monthly_by_service={SERVICE_GAS: [_monthly(90.0, "2026-09-11T00:00:00")]},
    )
    coordinator = EnergyCoordinator(hass, client.entry, AsyncMock())
    coordinator.data = await client.async_fetch_data()
    sensors = {}
    for description in SENSOR_DESCRIPTIONS:
        sensor = UtilitySensor(coordinator, description, "e")
        sensors[description.key] = (
            sensor.native_value,
            sensor.native_unit_of_measurement,
            sensor.extra_state_attributes,
        )
    shown = {key: value for key, (value, _unit, _attrs) in sensors.items() if value is not None}
    assert shown == {"gas_usage": 2.0, "gas_cost": 90.0, "gas_rate": 0.9}
    assert sensors["gas_usage"][1] == "thm"
    assert sensors["gas_rate"][1:] == (
        "USD/thm",
        {"source": "billing_cycle", "rate_plan": None, "customer_charge_per_day": None},
    )
    assert sensors["gas_meter_reading"][1] == "CCF"
    assert sensors["electricity_meter_reading"][1] == "kWh"


async def test_overlapping_updates_log_in_once(hass: HomeAssistant, api) -> None:
    """Each login is recorded as a device on the account (ADR 0007)."""

    async def _login(username, password) -> None:
        await asyncio.sleep(0)  # in flight: let the other update run

    api.async_login.side_effect = _login
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))

    await asyncio.gather(
        client.async_fetch_data(), client.async_update_statistics(Bill(BillSummary()))
    )

    api.async_login.assert_awaited_once()


async def test_rejected_login_is_not_retried_by_an_overlapping_update(
    hass: HomeAssistant, api
) -> None:
    """Repeated failed logins lock the IP address (ADR 0007)."""

    async def _login(username, password) -> None:
        await asyncio.sleep(0)
        raise AlliantEnergyAuthError("bad password")

    api.async_login.side_effect = _login
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))

    results = await asyncio.gather(
        client.async_fetch_data(),
        client.async_update_statistics(Bill(BillSummary())),
        return_exceptions=True,
    )

    assert all(isinstance(result, AlliantEnergyAuthError) for result in results)
    api.async_login.assert_awaited_once()


async def test_account_without_a_premise_is_an_error(hass: HomeAssistant, api) -> None:
    client = LocalUtilityClient(hass, _entry())  # no premise_id
    api.async_get_accounts.return_value = [Account("other", "P0", {})]

    with pytest.raises(AlliantEnergyError, match="acct-1"):
        await client.async_fetch_data()

    api.async_get_meters.assert_not_awaited()


@pytest.mark.parametrize("option", ["abc", "", 0, -0.1, None])
async def test_unusable_rate_option_is_ignored(hass: HomeAssistant, api, option) -> None:
    client = LocalUtilityClient(hass, _entry(options={"electricity_rate": option}, premise_id="P1"))

    await client.async_update_statistics(Bill(BillSummary()))

    assert api.update_statistics.await_args.kwargs["rates"][SERVICE_ELECTRIC].static is None


async def test_projection_without_a_read_window_uses_the_last_period_length(
    hass: HomeAssistant, api, hass_storage
) -> None:
    """No next-read window: the electric cycle lasts as long as the last one (31
    days); gas, with no billed days either, cannot be projected."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    api.async_get_meters.return_value = [
        Meter("E1", "P1", SERVICE_ELECTRIC, {}),
        Meter("G1", "P1", SERVICE_GAS, {}),
    ]
    api.async_get_usage.side_effect = _usage_router(
        daily_by_service={
            SERVICE_ELECTRIC: [_daily(10.0, "2026-10-12T00:00:00")],
            SERVICE_GAS: [_daily(1.0, "2026-10-12T00:00:00")],
        },
        monthly_by_service={},
    )
    _with_bill(api, hass_storage, client, replace(BILL, next_read_start=None, next_read_end=None))

    data = await client.async_fetch_data()

    assert data.services[SERVICE_ELECTRIC].cycle_days == 31
    gas = data.services[SERVICE_GAS]
    assert gas.rate is not None
    assert (gas.cycle_days, gas.projected_cost) == (None, None)
