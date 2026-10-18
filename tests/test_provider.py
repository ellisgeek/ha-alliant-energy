"""Tests for the Home Assistant-facing provider client."""

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
    Meter,
    UsageReading,
)
from custom_components.alliant_energy.billparser import Bill, ServiceBill
from custom_components.alliant_energy.const import DOMAIN
from custom_components.alliant_energy.models import ServiceData
from custom_components.alliant_energy.provider import LocalUtilityClient


@pytest.fixture
def api():
    """Replace the real curl_cffi-backed client and the statistics import."""
    instance = MagicMock()
    instance.async_login = AsyncMock()
    instance.async_get_accounts = AsyncMock(return_value=[])
    instance.async_get_meters = AsyncMock(return_value=[])
    instance.async_get_usage = AsyncMock(return_value=[])
    instance.async_get_current_bill = AsyncMock(return_value={})
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
BILL = Bill(
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


def _with_bill(api) -> None:
    api.async_get_current_bill.return_value = {"invoiceId": "INV1"}
    api.async_get_bill_pdf.return_value = b"%PDF"


def _daily(usage, date):
    return UsageReading(
        usage=usage, cost=0.0, reading_date=date, reading_from=None, reading_to=None, unit=None
    )


def _monthly(cost, period_to):
    return UsageReading(
        usage=100.0,
        cost=cost,
        reading_date="0001-01-01T01:00:00",
        reading_from=None,
        reading_to=period_to,
        unit=None,
    )


def _usage_router(daily_by_service, monthly_by_service):
    async def _get(service_type, account, premise, meter, frm, to, periodicity=PERIODICITY_DAILY):
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

    assert data.electricity_kwh == 11.0
    assert data.gas_therms == 4.0
    assert data.electricity_cost == 75.0
    assert data.gas_cost is None
    assert data.currency == "USD"


async def test_update_statistics_delegates_to_importer(hass: HomeAssistant, api) -> None:
    """The statistics method logs in, resolves context, and imports stats."""
    client = LocalUtilityClient(hass, _entry(premise_id="P1"))
    meters = [Meter("E1", "P1", SERVICE_ELECTRIC, {})]
    api.async_get_meters.return_value = meters

    await client.async_update_statistics()

    api.update_statistics.assert_awaited_once()
    # Called with (hass, api, account, premise, meters).
    args = api.update_statistics.await_args[0]
    assert args[2] == "acct-1"
    assert args[3] == "P1"
    assert args[4] == meters


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


async def test_rates_prefer_options_then_bill(hass: HomeAssistant, api) -> None:
    """A fixed rate from options wins; otherwise the bill's marginal rate is used."""
    client = LocalUtilityClient(hass, _entry(options={"electricity_rate": 0.2}, premise_id="P1"))
    _with_bill(api)
    with patch("custom_components.alliant_energy.provider.parse_bill", return_value=BILL):
        data = await client.async_fetch_data()

    electric = data.services[SERVICE_ELECTRIC]
    assert (electric.rate, electric.rate_source) == (0.2, "options")
    assert electric.rate_plan == "RG1"
    assert electric.rate_plan_name == "Residential Electric Service"
    assert electric.customer_charge_per_day == 0.526
    assert data.services[SERVICE_GAS].rate == pytest.approx(0.508507)
    assert data.services[SERVICE_GAS].rate_source == "bill"
    assert data.services[SERVICE_GAS].rate_plan == "GG1"
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
    assert data.services[SERVICE_ELECTRIC].rate_plan is None
    assert data.services[SERVICE_GAS] == ServiceData()


async def test_statistics_use_the_resolved_rates(hass: HomeAssistant, api) -> None:
    client = LocalUtilityClient(hass, _entry(options={"gas_rate": 1.1}, premise_id="P1"))
    _with_bill(api)
    with patch("custom_components.alliant_energy.provider.parse_bill", return_value=BILL):
        await client.async_update_statistics()

    kwargs = api.update_statistics.await_args.kwargs
    assert kwargs["electricity_rate"] == pytest.approx(0.16223)
    assert kwargs["gas_rate"] == pytest.approx(1.1)


async def test_bill_details_and_projection(hass: HomeAssistant, api) -> None:
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
    _with_bill(api)
    with patch("custom_components.alliant_energy.provider.parse_bill", return_value=BILL):
        data = await client.async_fetch_data()

    electric = data.services[SERVICE_ELECTRIC]
    assert electric.adjustment == pytest.approx(-0.00788)
    assert (electric.meter_reading, electric.previous_meter_reading) == (70190, 69851)
    # Cycle Oct 12 -> Nov 10 (middle of the Nov 9-12 read window): 29 days.
    # 30 kWh in 3 days -> 290 kWh x 0.16223 + 29 x 0.526 customer charge.
    assert (electric.usage_to_date, electric.days_to_date, electric.cycle_days) == (30, 3, 29)
    assert electric.cost_to_date == pytest.approx(6.44)
    assert electric.projected_cost == pytest.approx(62.30)
    assert data.projected_bill == pytest.approx(62.30)  # no gas usage data

    gas = data.services[SERVICE_GAS]
    assert gas.heat_factor == pytest.approx(1.06)
    assert gas.adjustment == pytest.approx(-0.471793)
    assert gas.projected_cost is None

    assert data.billing_period_start == date(2026, 9, 11)
    assert data.billing_period_end == date(2026, 10, 12)
    assert data.billed_days == 31
    assert data.next_meter_read_start == date(2026, 11, 9)
    assert data.average_temperature == 60
    assert data.degree_days == 157


async def test_saved_bill_is_reused_after_a_restart(hass: HomeAssistant, api) -> None:
    """The PDF is downloaded once per invoice, not once per Home Assistant run."""
    entry = _entry(premise_id="P1")
    _with_bill(api)
    with patch("custom_components.alliant_energy.provider.parse_bill", return_value=BILL):
        await LocalUtilityClient(hass, entry).async_fetch_data()
        api.async_get_bill_pdf.assert_awaited_once()

        restarted = await LocalUtilityClient(hass, entry).async_fetch_data()

    api.async_get_bill_pdf.assert_awaited_once()  # not downloaded again
    assert restarted.services[SERVICE_ELECTRIC].rate_plan == "RG1"
    assert restarted.billing_period_end == date(2026, 10, 12)


async def test_new_invoice_downloads_the_new_bill(hass: HomeAssistant, api) -> None:
    entry = _entry(premise_id="P1")
    _with_bill(api)
    with patch("custom_components.alliant_energy.provider.parse_bill", return_value=BILL):
        await LocalUtilityClient(hass, entry).async_fetch_data()
        api.async_get_current_bill.return_value = {"invoiceId": "INV2"}
        await LocalUtilityClient(hass, entry).async_fetch_data()

    assert api.async_get_bill_pdf.await_count == 2


async def test_saved_bill_from_another_parser_version_is_parsed_again(
    hass: HomeAssistant, api, hass_storage
) -> None:
    entry = _entry(premise_id="P1")
    hass_storage[f"alliant_energy.bill.{entry.entry_id}"] = {
        "version": 1,
        "key": f"alliant_energy.bill.{entry.entry_id}",
        "data": {"parser_version": 0, "invoice_id": "INV1", "bill": {}},
    }
    _with_bill(api)
    with patch("custom_components.alliant_energy.provider.parse_bill", return_value=BILL):
        await LocalUtilityClient(hass, entry).async_fetch_data()

    api.async_get_bill_pdf.assert_awaited_once()
