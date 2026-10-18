"""Tests for long-term statistics import."""

from datetime import UTC, date, datetime
from functools import partial
from unittest.mock import AsyncMock, MagicMock, patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers.recorder import get_instance
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.alliant_energy import statistics as stats
from custom_components.alliant_energy.api import (
    PERIODICITY_HOURLY,
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyError,
    Meter,
    UsageReading,
)
from custom_components.alliant_energy.const import DOMAIN
from custom_components.alliant_energy.rates import KnownRates

CHICAGO = ZoneInfo("America/Chicago")

# With "now" frozen here, the newest hourly data requested is 2026-10-17 and
# the backward walk's 30-day chunks are:
#   C1 09/17-10/17, C2 08/17-09/16, C3 07/17-08/16, C4 06/16-07/16, C5 05/16-06/15
NOW = "2026-10-18T12:00:00-05:00"


@pytest.fixture(autouse=True)
def _frozen_now(freezer):
    freezer.move_to(NOW)


@pytest.fixture(autouse=True)
def _no_chunk_delay():
    with patch.object(stats, "_CHUNK_DELAY", 0):
        yield


def _hourly(reading_date, usage):
    return UsageReading(
        usage=usage,
        cost=0.0,
        reading_date=reading_date,
        reading_from=None,
        reading_to=None,
        unit=None,
    )


def _hours(day, values):
    """Hourly readings from 00:00 on ``day`` (YYYY-MM-DD), one per value."""
    return [_hourly(f"{day}T{h:02d}:00:00+01:00", v) for h, v in enumerate(values)]


def _monthly(usage, cost, start, end):
    return UsageReading(
        usage=usage,
        cost=cost,
        reading_date="0001-01-01T01:00:00",
        reading_from=start,
        reading_to=end,
        unit=None,
    )


def _mdy(value: str) -> date:
    return datetime.strptime(value, "%m/%d/%Y").date()


def _api(hourly=None, monthly=None, fail_before=None):
    """API mock: hourly requests return the readings inside From..To.

    ``fail_before`` makes hourly requests that start before that date raise,
    like an API that errors for dates before the history begins.
    """
    api = MagicMock()

    async def _get(service, account, premise, meter, frm, to, periodicity=PERIODICITY_HOURLY):
        if periodicity != PERIODICITY_HOURLY:
            return monthly or []
        if fail_before is not None and _mdy(frm) < fail_before:
            raise AlliantEnergyError("HTTP 500")
        return [
            r
            for r in hourly or []
            if _mdy(frm) <= date.fromisoformat(r.reading_date[:10]) <= _mdy(to)
        ]

    api.async_get_usage = AsyncMock(side_effect=_get)
    return api


def _hh_requests(api) -> list[tuple[str, str]]:
    """(From, To) of every hourly usage request made through the mock."""
    return [
        (c.args[4], c.args[5])
        for c in api.async_get_usage.call_args_list
        if c.kwargs.get("periodicity") == PERIODICITY_HOURLY
    ]


async def _run_meter(
    hass, api, meter, rate=None, max_days=3650, marginal=None, marginal_from=None
) -> None:
    """Import one meter through the real recorder and wait for the writes.

    ``rate`` is the static rate; ``marginal`` the bill's, from ``marginal_from``.
    """
    await hass.config.async_set_time_zone("America/Chicago")
    known = KnownRates(static=rate, marginal=marginal, marginal_from=marginal_from)
    await stats.async_update_statistics(
        hass, api, "acct", "P1", [meter], {meter.service_type: known}, max_history_days=max_days
    )
    await async_wait_recording_done(hass)


async def _unit(hass, stat_id) -> str | None:
    """The unit a statistic is stored in."""
    metadata = await get_instance(hass).async_add_executor_job(
        partial(get_metadata, hass, statistic_ids={stat_id})
    )
    return metadata[stat_id][1]["unit_of_measurement"]


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


async def test_history_dedups_and_returns_native() -> None:
    """Native values (unconverted), duplicate hours merged, missing usage skipped."""
    api = _api(
        hourly=[
            _hourly("2026-10-07T00:00:00+01:00", 2.0),
            _hourly("2026-10-07T01:00:00+01:00", 3.0),
            _hourly("2026-10-07T01:00:00+01:00", 3.0),  # duplicate hour
            _hourly("2026-10-07T02:00:00+01:00", None),  # skipped
        ]
    )
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    rows = await stats._async_fetch_history(api, "acct", "P1", meter, CHICAGO)

    assert [v for _, v in rows] == [2.0, 3.0]
    assert rows[0][0] < rows[1][0]


async def test_history_walks_back_until_two_empty_chunks() -> None:
    """One empty chunk is a gap; two in a row are the start of the history."""
    api = _api(
        hourly=[
            *_hours("2026-10-07", [1.0]),  # C1
            # C2 is empty: a gap, keep going
            *_hours("2026-08-01", [2.0]),  # C3
            # C4 and C5 are empty: stop
            *_hours("2026-05-01", [9.0]),  # never requested
        ]
    )
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    rows = await stats._async_fetch_history(api, "acct", "P1", meter, CHICAGO)

    assert [v for _, v in rows] == [2.0, 1.0]
    assert _hh_requests(api) == [
        ("09/17/2026", "10/17/2026"),
        ("08/17/2026", "09/16/2026"),
        ("07/17/2026", "08/16/2026"),
        ("06/16/2026", "07/16/2026"),
        ("05/16/2026", "06/15/2026"),
    ]


async def test_history_stops_at_an_api_error_once_data_was_found() -> None:
    """An error for older dates ends the walk; the newer data is kept."""
    api = _api(hourly=_hours("2026-10-07", [1.0]), fail_before=date(2026, 9, 1))
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    rows = await stats._async_fetch_history(api, "acct", "P1", meter, CHICAGO)

    assert [v for _, v in rows] == [1.0]


async def test_history_error_before_any_data_is_raised() -> None:
    api = _api(hourly=_hours("2026-10-07", [1.0]), fail_before=date(2026, 12, 1))
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    with pytest.raises(AlliantEnergyError):
        await stats._async_fetch_history(api, "acct", "P1", meter, CHICAGO)


async def test_gas_consumption_in_therms_and_sum_continues(hass) -> None:
    """Gas is stored in native therms; a second run adds only new hours."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    cons_id = stats.statistic_id("acct", meter)

    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0, 1.0])), meter)
    # The repeated 01:00 hour is skipped.
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [9.0, 1.0, 2.0])), meter)

    assert await _rows(hass, cons_id) == pytest.approx([(1.0, 1.0), (1.0, 2.0), (2.0, 4.0)])
    assert await _unit(hass, cons_id) == "thm"


async def test_effective_electric_cost(hass) -> None:
    """Electric cost uses amount/consumption from the covering billing period."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    api = _api(
        hourly=_hours("2026-10-07", [4.0, 6.0]),
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
        hourly=_hours("2026-10-07", [2.0]),  # 2 therms
        monthly=[_monthly(13.0, 21.0, "2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00")],
    )
    await _run_meter(hass, api, meter)

    cost = await _rows(hass, stats.statistic_id("acct", meter, "_cost"))
    assert cost[0][0] == pytest.approx(2.0 * 21.0 / 13.0)


async def test_static_gas_cost(hass) -> None:
    """A static $/therm rate prices gas on the native therm value."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [3.0])), meter, rate=0.9)

    cost = await _rows(hass, stats.statistic_id("acct", meter, "_cost"))
    assert cost == pytest.approx([(2.7, 2.7)])


async def test_later_runs_fetch_only_from_last_imported_day(hass) -> None:
    """The first run walks the history back; the next requests one short range."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    hours = _hours("2026-10-07", [1.0, 1.0, 1.0])

    first = _api(hourly=hours)
    await _run_meter(hass, first, meter, rate=0.2)
    assert len(_hh_requests(first)) == 3  # C1 with data, then two empty chunks

    second = _api(hourly=hours)
    await _run_meter(hass, second, meter, rate=0.2)
    assert _hh_requests(second) == [("10/07/2026", "10/17/2026")]


async def test_new_cost_rate_prices_the_stored_usage(hass) -> None:
    """When a rate first appears, cost covers every stored hour, with no new walk."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    hours = _hours("2026-10-07", [1.0, 1.0, 1.0])
    cost_id = stats.statistic_id("acct", meter, "_cost")

    await _run_meter(hass, _api(hourly=hours), meter)  # no rate yet
    assert await _rows(hass, cost_id) == []

    later = _api(hourly=hours)
    await _run_meter(hass, later, meter, rate=1.5)
    assert _hh_requests(later) == [("10/07/2026", "10/17/2026")]
    assert [state for state, _ in await _rows(hass, cost_id)] == pytest.approx([1.5] * 3)
    assert await _rows(hass, stats.statistic_id("acct", meter)) == pytest.approx(
        [(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)]
    )


async def test_statistics_from_an_earlier_version_are_rebuilt_once(hass) -> None:
    """A gas statistic stored in kWh, without full history, is rebuilt in therms."""
    meter = Meter("G1", "P1", SERVICE_GAS, {})
    cons_id = stats.statistic_id("acct", meter)
    async_add_external_statistics(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="old",
            source=DOMAIN,
            statistic_id=cons_id,
            unit_class="energy",
            unit_of_measurement="kWh",
        ),
        [StatisticData(start=datetime(2026, 10, 7, 11, tzinfo=UTC), state=29.3001, sum=29.3001)],
    )
    await async_wait_recording_done(hass)

    hours = _hours("2026-10-07", [1.0, 1.0, 1.0])
    rebuild = _api(hourly=hours)
    await _run_meter(hass, rebuild, meter)

    assert await _unit(hass, cons_id) == "thm"
    assert await _rows(hass, cons_id) == pytest.approx([(1.0, 1.0), (1.0, 2.0), (1.0, 3.0)])
    assert len(_hh_requests(rebuild)) == 3  # full walk

    # Only once: the next run is a normal update.
    after = _api(hourly=hours)
    await _run_meter(hass, after, meter)
    assert _hh_requests(after) == [("10/07/2026", "10/17/2026")]


async def test_changing_the_history_limit_rebuilds_with_the_new_limit(hass) -> None:
    """Lowering the limit rebuilds once and drops hours older than the limit."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cons_id = stats.statistic_id("acct", meter)
    hours = [*_hours("2026-08-01", [5.0]), *_hours("2026-10-07", [1.0])]  # 78 and 11 days ago

    await _run_meter(hass, _api(hourly=hours), meter)  # default limit: both kept
    assert len(await _rows(hass, cons_id)) == 2

    lowered = _api(hourly=hours)
    await _run_meter(hass, lowered, meter, max_days=30)
    assert _hh_requests(lowered) == [("09/18/2026", "10/17/2026")]  # walk stops at the limit
    assert await _rows(hass, cons_id) == pytest.approx([(1.0, 1.0)])

    again = _api(hourly=hours)
    await _run_meter(hass, again, meter, max_days=30)  # same limit: normal update
    assert _hh_requests(again) == [("10/07/2026", "10/17/2026")]


async def test_store_from_the_first_version_counts_as_the_default_limit(hass, hass_storage) -> None:
    """Meters recorded as a plain list need no rebuild at the default limit."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cons_id = stats.statistic_id("acct", meter)
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0])), meter)
    hass_storage["alliant_energy.statistics"] = {
        "version": 1,
        "key": "alliant_energy.statistics",
        "data": {"full_history": [cons_id]},
    }

    later = _api(hourly=_hours("2026-10-07", [1.0, 1.0]))
    await _run_meter(hass, later, meter)

    assert _hh_requests(later) == [("10/07/2026", "10/17/2026")]


def _monthly_requests(api) -> int:
    return sum(
        1
        for c in api.async_get_usage.call_args_list
        if c.kwargs.get("periodicity") != PERIODICITY_HOURLY
    )


async def test_changing_the_static_rate_reprices_cost_only(hass) -> None:
    """Cost is repriced from the stored usage; usage and history stay untouched."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    hours = _hours("2026-10-07", [2.0, 2.0])
    await _run_meter(hass, _api(hourly=hours), meter, rate=0.10)

    changed = _api(hourly=hours)
    await _run_meter(hass, changed, meter, rate=0.20)

    assert _hh_requests(changed) == [("10/07/2026", "10/17/2026")]
    assert await _rows(hass, stats.statistic_id("acct", meter, "_cost")) == pytest.approx(
        [(0.4, 0.4), (0.4, 0.8)]
    )
    assert await _rows(hass, stats.statistic_id("acct", meter)) == pytest.approx(
        [(2.0, 2.0), (2.0, 4.0)]
    )


async def test_imported_cost_keeps_its_rate_when_a_new_bill_arrives(hass) -> None:
    """Cost is append-only: a new marginal rate prices only the new hours."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cost_id = stats.statistic_id("acct", meter, "_cost")
    cycle_start = date(2026, 10, 1)
    await _run_meter(
        hass,
        _api(hourly=_hours("2026-10-07", [1.0])),
        meter,
        marginal=0.17,
        marginal_from=cycle_start,
    )

    await _run_meter(
        hass,
        _api(hourly=_hours("2026-10-07", [1.0, 1.0])),
        meter,
        marginal=0.20,
        marginal_from=cycle_start,
    )

    assert [state for state, _ in await _rows(hass, cost_id)] == pytest.approx([0.17, 0.20])


async def test_marginal_rate_prices_only_its_billing_cycle_and_later(hass) -> None:
    """Older hours use their own billing cycle's effective rate."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    api = _api(
        hourly=[*_hours("2026-09-20", [1.0]), *_hours("2026-10-07", [1.0])],
        monthly=[
            _monthly(100.0, 25.0, "2026-09-01T00:00:00+01:00", "2026-10-01T00:00:00+01:00")
        ],  # September: 0.25 $/kWh effective
    )
    await _run_meter(hass, api, meter, marginal=0.17, marginal_from=date(2026, 10, 1))

    cost = await _rows(hass, stats.statistic_id("acct", meter, "_cost"))
    assert [state for state, _ in cost] == pytest.approx([0.25, 0.17])


async def test_updates_inside_the_marginal_billing_cycle_read_no_billing_cycles(hass) -> None:
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    hours = _hours("2026-10-07", [1.0])
    await _run_meter(
        hass, _api(hourly=hours), meter, marginal=0.17, marginal_from=date(2026, 10, 1)
    )

    later = _api(hourly=hours)
    await _run_meter(hass, later, meter, marginal=0.17, marginal_from=date(2026, 10, 1))

    assert _monthly_requests(later) == 0


async def test_cost_priced_before_cost_rate_identities_is_repriced_once(hass, hass_storage) -> None:
    """A store without cost rate identities means cost was priced the old way."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cost_id = stats.statistic_id("acct", meter, "_cost")
    hours = _hours("2026-10-07", [1.0])
    await _run_meter(hass, _api(hourly=hours), meter, rate=0.30)
    hass_storage["alliant_energy.statistics"]["data"].pop("cost_rates")
    async_add_external_statistics(  # an hour priced under the old rule
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name="old",
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=None,
        ),
        [StatisticData(start=datetime(2026, 10, 7, 5, tzinfo=UTC), state=9.0, sum=9.0)],
    )
    await async_wait_recording_done(hass)

    await _run_meter(hass, _api(hourly=hours), meter, rate=0.30)
    assert await _rows(hass, cost_id) == pytest.approx([(0.3, 0.3)])
    assert hass_storage["alliant_energy.statistics"]["data"]["cost_rates"] == {
        cost_id: "static:0.3"
    }


async def test_clearing_the_static_rate_without_a_fallback_keeps_the_cost_history(
    hass, caplog
) -> None:
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cost_id = stats.statistic_id("acct", meter, "_cost")
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0])), meter, rate=0.30)

    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0, 1.0])), meter)

    assert await _rows(hass, cost_id) == pytest.approx([(0.3, 0.3)])
    assert "keeping its cost history" in caplog.text


async def test_no_cost_rate_warns_once_and_does_not_retry_the_reprice(hass, caplog) -> None:
    """After a static rate is cleared with no fallback, later updates stay quiet."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0])), meter, rate=0.30)

    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0, 1.0])), meter)
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0, 1.0, 1.0])), meter)

    assert caplog.text.count("keeping its cost history") == 1
    assert "Repricing" not in caplog.text


async def test_without_a_cost_rate_hourly_requests_start_at_the_last_usage_hour(hass) -> None:
    """Cost that lags behind usage does not drag the hourly requests back."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cycle = [_monthly(100.0, 25.0, "2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00")]
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0]), monthly=cycle), meter)
    hours = [*_hours("2026-10-07", [1.0]), *_hours("2026-10-09", [1.0])]
    await _run_meter(hass, _api(hourly=hours), meter)  # billing cycles gone: no rate

    later = _api(hourly=hours)
    await _run_meter(hass, later, meter)

    assert _hh_requests(later) == [("10/09/2026", "10/17/2026")]


async def test_reprice_covers_new_hours_and_happens_once(hass, caplog) -> None:
    """Stored and newly fetched hours are repriced together; the next run appends."""
    meter = Meter("E1", "P1", SERVICE_ELECTRIC, {})
    cost_id = stats.statistic_id("acct", meter, "_cost")
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0])), meter, rate=0.10)

    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0, 2.0])), meter, rate=0.20)
    assert [v for row in await _rows(hass, cost_id) for v in row] == pytest.approx(
        [0.2, 0.2, 0.4, 0.6]
    )

    caplog.clear()
    await _run_meter(hass, _api(hourly=_hours("2026-10-07", [1.0, 2.0, 3.0])), meter, rate=0.20)
    assert "Repricing" not in caplog.text
    assert [v for row in await _rows(hass, cost_id) for v in row] == pytest.approx(
        [0.2, 0.2, 0.4, 0.6, 0.6, 1.2]
    )
