"""Tests for config entry setup and unload."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from custom_components.alliant_energy.api import AlliantEnergyAuthError, BillSummary
from custom_components.alliant_energy.bill import Bill
from custom_components.alliant_energy.models import EnergyData, ServiceData


def _client() -> AsyncMock:
    client = AsyncMock()
    client.async_fetch_data.return_value = EnergyData(
        bill=Bill(BillSummary(amount_due=101.25, previous_balance=88.0)),
        services={"electric": ServiceData(latest_usage=12.5)},
        currency="USD",
    )
    return client


async def test_setup_and_unload(hass: HomeAssistant, config_entry) -> None:
    """Setup creates the sensors and starts statistics; unload closes the client."""
    client = _client()
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=client):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.LOADED
    amount_due = hass.states.get("sensor.alliant_energy_amount_due")
    previous_balance = hass.states.get("sensor.alliant_energy_previous_balance")
    assert amount_due is not None and amount_due.state == "101.25"
    assert previous_balance is not None and previous_balance.state == "88.0"
    # The statistics import starts after the refresh, with the bill it fetched.
    client.async_update_statistics.assert_awaited_once_with(
        client.async_fetch_data.return_value.bill
    )

    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED
    client.async_close.assert_awaited_once()


async def test_setup_retries_and_closes_client_on_failure(
    hass: HomeAssistant, config_entry
) -> None:
    """A failed first refresh retries later and does not leak the HTTP session."""
    client = _client()
    client.async_fetch_data.side_effect = RuntimeError("down")
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=client):
        await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.state is ConfigEntryState.SETUP_RETRY
    client.async_close.assert_awaited_once()
    client.async_update_statistics.assert_not_awaited()  # no bill yet to price with


async def test_update_interval_option_applies(hass: HomeAssistant, config_entry) -> None:
    """Statistics follow the sensor refresh, so one interval drives both."""
    hass.config_entries.async_update_entry(config_entry, options={"update_interval_hours": 3})
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=_client()):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.runtime_data.update_interval == timedelta(hours=3)


async def _setup(hass: HomeAssistant, config_entry, client) -> None:
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=client):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()


async def test_each_refresh_starts_a_statistics_import_with_its_bill(
    hass: HomeAssistant, config_entry
) -> None:
    client = _client()
    await _setup(hass, config_entry, client)
    newer = EnergyData(bill=Bill(BillSummary(invoice_id="INV2")))
    client.async_fetch_data.return_value = newer

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    assert client.async_update_statistics.await_count == 2
    client.async_update_statistics.assert_awaited_with(newer.bill)
    assert client.async_fetch_data.await_count == 2  # no separate bill fetch


async def test_failed_refresh_still_imports_with_the_last_bill(
    hass: HomeAssistant, config_entry
) -> None:
    """A broken sensor endpoint must not stop the Energy dashboard's usage."""
    client = _client()
    await _setup(hass, config_entry, client)
    last_bill = client.async_fetch_data.return_value.bill
    client.async_fetch_data.side_effect = RuntimeError("bill/Current: HTTP 500")

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)
    await config_entry.runtime_data.async_refresh()  # failing again
    await hass.async_block_till_done(wait_background_tasks=True)

    assert client.async_update_statistics.await_count == 3
    client.async_update_statistics.assert_awaited_with(last_bill)


async def test_refresh_during_a_running_import_starts_no_second_one(
    hass: HomeAssistant, config_entry
) -> None:
    """A long backfill is still running when the next refresh comes."""
    client = _client()
    release = asyncio.Event()

    async def _import(_bill) -> None:
        await release.wait()

    client.async_update_statistics.side_effect = _import
    await _setup(hass, config_entry, client)

    await config_entry.runtime_data.async_refresh()
    await asyncio.sleep(0)
    assert client.async_update_statistics.await_count == 1

    release.set()
    running = config_entry.runtime_data.statistics_import
    assert running is not None
    await running
    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert client.async_update_statistics.await_count == 2


async def test_auth_error_during_import_starts_reauthentication(
    hass: HomeAssistant, config_entry
) -> None:
    client = _client()
    client.async_update_statistics.side_effect = AlliantEnergyAuthError("expired")
    await _setup(hass, config_entry, client)

    flows = hass.config_entries.flow.async_progress_by_handler("alliant_energy")
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_update_interval_defaults_to_a_day(hass: HomeAssistant, config_entry) -> None:
    with patch("custom_components.alliant_energy.LocalUtilityClient", return_value=_client()):
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()

    assert config_entry.runtime_data.update_interval == timedelta(hours=24)


async def test_removing_the_entry_deletes_the_saved_bill(
    hass: HomeAssistant, config_entry, hass_storage
) -> None:
    key = f"alliant_energy.bill.{config_entry.entry_id}"
    hass_storage[key] = {"version": 1, "key": key, "data": {"parser_version": 1}}

    await hass.config_entries.async_remove(config_entry.entry_id)
    await hass.async_block_till_done()

    assert key not in hass_storage


async def test_unload_stops_a_running_import_before_closing_the_client(
    hass: HomeAssistant, config_entry
) -> None:
    """Otherwise the import fails on a closed session and the backfill restarts."""
    client = _client()
    order: list[str] = []
    started = asyncio.Event()

    async def _import(_bill) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            order.append("import cancelled")
            raise

    async def _close() -> None:
        order.append("client closed")

    client.async_update_statistics.side_effect = _import
    client.async_close.side_effect = _close
    await _setup(hass, config_entry, client)
    await started.wait()

    assert await hass.config_entries.async_unload(config_entry.entry_id)

    assert order == ["import cancelled", "client closed"]


async def test_auth_failure_in_a_refresh_starts_no_import(
    hass: HomeAssistant, config_entry
) -> None:
    client = _client()
    await _setup(hass, config_entry, client)
    client.async_fetch_data.side_effect = AlliantEnergyAuthError("expired")

    await config_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()

    client.async_update_statistics.assert_awaited_once()  # only the first refresh's
    flows = hass.config_entries.flow.async_progress_by_handler("alliant_energy")
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]
