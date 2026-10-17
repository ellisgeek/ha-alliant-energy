"""Tests for long-term statistics import."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from custom_components.alliant_energy import statistics as stats
from custom_components.alliant_energy.api import (
    PERIODICITY_HOURLY,
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    Meter,
    UsageReading,
)
from custom_components.alliant_energy.const import DOMAIN, THERM_TO_KWH

CHICAGO = ZoneInfo("America/Chicago")


def _hourly(reading_date, usage):
    return UsageReading(
        usage=usage,
        cost=0.0,
        reading_date=reading_date,
        reading_from=None,
        reading_to=None,
        unit=None,
    )


def _monthly(usage, cost, start, end):
    return UsageReading(
        usage=usage,
        cost=cost,
        reading_date="0001-01-01T01:00:00",
        reading_from=start,
        reading_to=end,
        unit=None,
    )


def _api(hourly=None, monthly=None):
    """API mock routing async_get_usage by periodicity."""
    api = MagicMock()

    async def _get(service, account, premise, meter, frm, to, periodicity=PERIODICITY_HOURLY):
        return (hourly if periodicity == PERIODICITY_HOURLY else monthly) or []

    api.async_get_usage = AsyncMock(side_effect=_get)
    return api


def _recorder(last_by_id=None):
    """Patch the recorder bits; return (executor, captured async_add_external_statistics)."""
    last_by_id = last_by_id or {}
    executor = MagicMock()

    async def _job(func, hass, number, stat_id, convert, types):
        return {stat_id: [last_by_id[stat_id]]} if stat_id in last_by_id else {}

    executor.async_add_executor_job = AsyncMock(side_effect=_job)
    added = MagicMock()
    return executor, added


def test_statistic_ids() -> None:
    meter = Meter("300000003", "P1", SERVICE_ELECTRIC, {})
    expected = f"{DOMAIN}:1000000001_electric_300000003"
    assert stats.statistic_id("1000000001", meter) == expected
    assert stats.statistic_id("1000000001", meter, "_cost") == f"{expected}_cost"


def test_parse_hour_localizes_wall_clock_time() -> None:
    """The bogus offset is dropped; the wall time is the account's local time."""
    # 01:00 local in Chicago on 2026-10-07 (CDT, UTC-5) -> 06:00 UTC.
    hour = stats._parse_hour("2026-10-07T01:00:00+01:00", CHICAGO)
    assert hour == datetime(2026, 10, 7, 6, 0, tzinfo=UTC)


async def test_fetch_hourly_dedups_and_returns_native() -> None:
    """Returns native values (unconverted), deduped, None-usage skipped."""
    api = _api(
        hourly=[
            _hourly("2026-10-07T00:00:00+01:00", 2.0),
            _hourly("2026-10-07T01:00:00+01:00", 3.0),
            _hourly("2026-10-07T01:00:00+01:00", 3.0),  # duplicate hour
            _hourly("2026-10-07T02:00:00+01:00", None),  # skipped
        ]
    )
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    with patch.object(stats, "_CHUNK_DELAY", 0), patch.object(stats, "STATISTICS_BACKFILL_DAYS", 1):
        rows = await stats._async_fetch_hourly(api, "acct", "P1", meter, CHICAGO)

    assert [v for _, v in rows] == [2.0, 3.0]  # native therms, not converted
    assert rows[0][0] < rows[1][0]


async def test_update_meter_imports_consumption_in_kwh(hass) -> None:
    """Gas consumption is converted therms->kWh; cumulative sum continues."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    cons_id = stats.statistic_id("acct", meter)
    h0 = stats._parse_hour("2026-10-07T00:00:00+01:00", CHICAGO)
    assert h0 is not None
    api = _api(
        hourly=[
            _hourly("2026-10-07T00:00:00+01:00", 1.0),  # == last stored hour, skipped
            _hourly("2026-10-07T01:00:00+01:00", 2.0),
        ]
    )
    executor, added = _recorder({cons_id: {"start": h0.timestamp(), "sum": 100.0}})

    with (
        patch.object(stats, "get_instance", return_value=executor),
        patch.object(stats, "async_add_external_statistics", added),
        patch.object(stats, "_CHUNK_DELAY", 0),
    ):
        # static_rate None + gas -> no cost series, only consumption.
        await stats._async_update_meter(hass, api, "acct", "P1", meter, CHICAGO, None)

    added.assert_called_once()
    _hass, metadata, series = added.call_args[0]
    assert metadata["statistic_id"] == cons_id
    assert metadata["unit_of_measurement"] == "kWh"
    assert len(series) == 1  # first hour skipped
    assert series[0]["state"] == pytest.approx(2.0 * THERM_TO_KWH)
    assert series[0]["sum"] == pytest.approx(100.0 + 2.0 * THERM_TO_KWH)


async def test_update_meter_effective_electric_cost(hass) -> None:
    """Electric cost uses amount/consumption from the covering billing period."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cons_id = stats.statistic_id("acct", meter)
    cost_id = stats.statistic_id("acct", meter, "_cost")
    api = _api(
        hourly=[
            _hourly("2026-10-07T00:00:00+01:00", 4.0),
            _hourly("2026-10-07T01:00:00+01:00", 6.0),
        ],
        monthly=[
            _monthly(100.0, 25.0, "2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00")
        ],  # rate 0.25 $/kWh
    )
    executor, added = _recorder()  # first run, sums from 0

    with (
        patch.object(stats, "get_instance", return_value=executor),
        patch.object(stats, "async_add_external_statistics", added),
        patch.object(stats, "_CHUNK_DELAY", 0),
    ):
        await stats._async_update_meter(hass, api, "acct", "P1", meter, CHICAGO, None)

    by_id = {c.args[1]["statistic_id"]: c.args[2] for c in added.call_args_list}
    assert set(by_id) == {cons_id, cost_id}
    assert [s["state"] for s in by_id[cost_id]] == pytest.approx([1.0, 1.5])  # 4*.25, 6*.25
    assert [s["sum"] for s in by_id[cost_id]] == pytest.approx([1.0, 2.5])


async def test_update_meter_effective_gas_cost(hass) -> None:
    """Gas cost also derives from the monthly bill (amount/consumption therms)."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    cons_id = stats.statistic_id("acct", meter)
    cost_id = stats.statistic_id("acct", meter, "_cost")
    api = _api(
        hourly=[_hourly("2026-10-07T00:00:00+01:00", 2.0)],  # 2 therms
        monthly=[
            _monthly(13.0, 21.0, "2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00")
        ],  # ~1.615 $/therm
    )
    executor, added = _recorder()
    with (
        patch.object(stats, "get_instance", return_value=executor),
        patch.object(stats, "async_add_external_statistics", added),
        patch.object(stats, "_CHUNK_DELAY", 0),
    ):
        await stats._async_update_meter(hass, api, "acct", "P1", meter, CHICAGO, None)

    by_id = {c.args[1]["statistic_id"]: c.args[2] for c in added.call_args_list}
    assert set(by_id) == {cons_id, cost_id}
    # consumption converted to kWh; cost on native therms x effective rate.
    assert by_id[cons_id][0]["state"] == pytest.approx(2.0 * THERM_TO_KWH)
    assert by_id[cost_id][0]["state"] == pytest.approx(2.0 * (21.0 / 13.0))


async def test_update_meter_static_gas_cost(hass) -> None:
    """A static $/therm rate yields gas cost on the native therm value."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    cost_id = stats.statistic_id("acct", meter, "_cost")
    api = _api(hourly=[_hourly("2026-10-07T00:00:00+01:00", 3.0)])
    executor, added = _recorder()

    with (
        patch.object(stats, "get_instance", return_value=executor),
        patch.object(stats, "async_add_external_statistics", added),
        patch.object(stats, "_CHUNK_DELAY", 0),
    ):
        await stats._async_update_meter(hass, api, "acct", "P1", meter, CHICAGO, 0.9)

    by_id = {c.args[1]["statistic_id"]: c.args[2] for c in added.call_args_list}
    assert by_id[cost_id][0]["state"] == pytest.approx(3.0 * 0.9)


async def test_import_through_real_recorder(recorder_mock, hass) -> None:
    """The recorder accepts the metadata and a second run continues the sum."""
    from homeassistant.components.recorder.statistics import get_last_statistics
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )

    await hass.config.async_set_time_zone("America/Chicago")
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    gas_id = stats.statistic_id("A1", meter)
    cost_id = stats.statistic_id("A1", meter, "_cost")

    async def _run(hours):
        api = _api(hourly=[_hourly(h, 1.0) for h in hours])
        with (
            patch.object(stats, "_CHUNK_DELAY", 0),
            patch.object(stats, "STATISTICS_BACKFILL_DAYS", 1),
        ):
            await stats.async_update_statistics(hass, api, "A1", "P1", [meter], gas_rate=2.0)
        await async_wait_recording_done(hass)

    def _last(stat_id):
        return get_last_statistics(hass, 1, stat_id, True, {"sum"})[stat_id][0]["sum"]

    await _run(["2026-10-07T00:00:00+01:00", "2026-10-07T01:00:00+01:00"])
    # The second run repeats the last hour; only the new hour is added.
    await _run(["2026-10-07T01:00:00+01:00", "2026-10-07T02:00:00+01:00"])

    executor = recorder_mock.async_add_executor_job
    assert await executor(_last, gas_id) == pytest.approx(3 * THERM_TO_KWH)
    assert await executor(_last, cost_id) == pytest.approx(6.0)
