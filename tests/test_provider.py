"""Tests for the Home Assistant-facing provider client."""

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
from custom_components.alliant_energy.const import DOMAIN
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


def _entry(**extra):
    data = {"username": "u", "password": "p", "account_id": "acct-1", **extra}
    return MockConfigEntry(domain=DOMAIN, data=data)


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
