"""Tests for long-term statistics import."""

from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

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


def _hh_from_dates(api) -> list[str]:
    """The From dates of every hourly usage request made through the mock."""
    return [
        c.args[4]
        for c in api.async_get_usage.call_args_list
        if c.kwargs.get("periodicity") == PERIODICITY_HOURLY
    ]


async def _run_meter(hass, api, meter, rate=None, backfill_days=None) -> None:
    """Import one meter through the real recorder and wait for the writes."""
    with ExitStack() as stack:
        stack.enter_context(patch.object(stats, "_CHUNK_DELAY", 0))
        if backfill_days is not None:
            stack.enter_context(patch.object(stats, "STATISTICS_BACKFILL_DAYS", backfill_days))
        await stats._async_update_meter(hass, api, "acct", "P1", meter, CHICAGO, rate)
    await async_wait_recording_done(hass)


async def _rows(hass, stat_id) -> list[tuple[float, float]]:
    """(state, sum) for every stored hour of a statistic, oldest first."""

    def _get():
        return statistics_during_period(
            hass, datetime(2000, 1, 1, tzinfo=UTC), None, {stat_id}, "hour", None, {"state", "sum"}
        ).get(stat_id, [])

    rows = await get_instance(hass).async_add_executor_job(_get)
    return [(row["state"], row["sum"]) for row in rows]


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


async def test_gas_consumption_in_kwh_and_sum_continues(hass) -> None:
    """Gas is stored in kWh; a second run adds only new hours to the sum."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    cons_id = stats.statistic_id("acct", meter)

    first = ["2026-10-07T00:00:00+01:00", "2026-10-07T01:00:00+01:00"]
    await _run_meter(hass, _api(hourly=[_hourly(h, 1.0) for h in first]), meter)
    # The repeated 01:00 hour is skipped.
    second = ["2026-10-07T01:00:00+01:00", "2026-10-07T02:00:00+01:00"]
    await _run_meter(hass, _api(hourly=[_hourly(h, 2.0) for h in second]), meter)

    assert await _rows(hass, cons_id) == pytest.approx(
        [
            (THERM_TO_KWH, THERM_TO_KWH),
            (THERM_TO_KWH, 2 * THERM_TO_KWH),
            (2 * THERM_TO_KWH, 4 * THERM_TO_KWH),
        ]
    )


async def test_effective_electric_cost(hass) -> None:
    """Electric cost uses amount/consumption from the covering billing period."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    api = _api(
        hourly=[
            _hourly("2026-10-07T00:00:00+01:00", 4.0),
            _hourly("2026-10-07T01:00:00+01:00", 6.0),
        ],
        monthly=[
            _monthly(100.0, 25.0, "2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00")
        ],  # rate 0.25 $/kWh
    )
    await _run_meter(hass, api, meter)

    assert await _rows(hass, stats.statistic_id("acct", meter)) == pytest.approx(
        [(4.0, 4.0), (6.0, 10.0)]
    )
    assert await _rows(hass, stats.statistic_id("acct", meter, "_cost")) == pytest.approx(
        [(1.0, 1.0), (1.5, 2.5)]
    )


async def test_effective_gas_cost(hass) -> None:
    """Gas cost is native therms x the billing period's effective $/therm."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    api = _api(
        hourly=[_hourly("2026-10-07T00:00:00+01:00", 2.0)],  # 2 therms
        monthly=[_monthly(13.0, 21.0, "2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00")],
    )
    await _run_meter(hass, api, meter)

    cost = await _rows(hass, stats.statistic_id("acct", meter, "_cost"))
    assert cost[0][0] == pytest.approx(2.0 * 21.0 / 13.0)


async def test_static_gas_cost(hass) -> None:
    """A static $/therm rate prices gas on the native therm value."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    api = _api(hourly=[_hourly("2026-10-07T00:00:00+01:00", 3.0)])
    await _run_meter(hass, api, meter, rate=0.9)

    cost = await _rows(hass, stats.statistic_id("acct", meter, "_cost"))
    assert cost == pytest.approx([(2.7, 2.7)])


async def test_later_runs_fetch_only_from_last_imported_day(hass) -> None:
    """The first run backfills in chunks; the next run requests one short range."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    hours = [_hourly(f"2026-10-07T0{h}:00:00+01:00", 1.0) for h in range(3)]

    first = _api(hourly=hours)
    await _run_meter(hass, first, meter, rate=0.2, backfill_days=40)
    assert len(_hh_from_dates(first)) == 2  # 40 days in 30-day chunks

    second = _api(hourly=hours)
    await _run_meter(hass, second, meter, rate=0.2, backfill_days=40)
    assert _hh_from_dates(second) == ["10/07/2026"]  # from the last imported day


async def test_new_cost_statistic_is_backfilled(hass) -> None:
    """When a rate first appears, cost is imported for the whole window."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    hours = [_hourly(f"2026-10-07T0{h}:00:00+01:00", 1.0) for h in range(3)]
    cost_id = stats.statistic_id("acct", meter, "_cost")

    await _run_meter(hass, _api(hourly=hours), meter, backfill_days=40)  # no rate
    assert await _rows(hass, cost_id) == []

    later = _api(hourly=hours)
    await _run_meter(hass, later, meter, rate=1.5, backfill_days=40)
    assert len(_hh_from_dates(later)) == 2  # full window again, for the cost
    assert [state for state, _ in await _rows(hass, cost_id)] == pytest.approx([1.5] * 3)
