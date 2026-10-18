"""Tests for reading usage through the Alliant backend adapter."""

from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo

import pytest

from custom_components.alliant_energy.api import (
    PERIODICITY_DAILY,
    PERIODICITY_HOURLY,
    PERIODICITY_MONTHLY,
    SERVICE_ELECTRIC,
    AlliantEnergyApi,
    AlliantEnergyError,
    Meter,
    UsageReading,
)
from custom_components.alliant_energy.rates import BillingCycle
from custom_components.alliant_energy.usage import AlliantUsage

CHICAGO = ZoneInfo("America/Chicago")
METER = Meter("E1", "P1", SERVICE_ELECTRIC, {})


class FakeApi:
    """Answers usage requests from canned readings and records each request."""

    def __init__(self, readings: list[UsageReading] | None = None) -> None:
        self.readings = readings or []
        self.requests: list[dict] = []

    async def async_get_usage(self, **request) -> list[UsageReading]:
        self.requests.append(request)
        return self.readings


def _reading(reading_date, usage):
    return UsageReading(
        usage=usage,
        cost=0.0,
        reading_date=reading_date,
        reading_from=None,
        reading_to=None,
        unit=None,
    )


def _usage(api: FakeApi) -> AlliantUsage:
    return AlliantUsage(cast(AlliantEnergyApi, api), "acct", "P1", CHICAGO)


async def test_hourly_returns_local_wall_clock_hours_in_utc() -> None:
    """The bogus offset is dropped, duplicates merge, and missing usage is skipped."""
    api = FakeApi(
        [
            _reading("2026-10-07T01:00:00+01:00", 3.0),
            _reading("2026-10-07T00:00:00+01:00", 2.0),
            _reading("2026-10-07T01:00:00+01:00", 3.0),  # duplicate hour
            _reading("2026-10-07T02:00:00+01:00", None),  # skipped
        ]
    )

    hours = await _usage(api).hourly(METER, date(2026, 10, 7), date(2026, 10, 8))

    # 00:00 and 01:00 in Chicago on 2026-10-07 (CDT, UTC-5).
    assert hours == [
        (datetime(2026, 10, 7, 5, tzinfo=UTC), 2.0),
        (datetime(2026, 10, 7, 6, tzinfo=UTC), 3.0),
    ]
    assert api.requests == [
        {
            "service_type": SERVICE_ELECTRIC,
            "account_number": "acct",
            "premise_number": "P1",
            "meter_number": "E1",
            "from_date": "10/07/2026",
            "to_date": "10/08/2026",
            "periodicity": PERIODICITY_HOURLY,
        }
    ]


def _ranges(api: FakeApi) -> list[tuple[str, str]]:
    return [(r["from_date"], r["to_date"]) for r in api.requests]


async def test_hourly_splits_long_ranges_into_30_day_requests() -> None:
    """Each request covers at most 30 days, both ends included."""
    api = FakeApi()

    await _usage(api).hourly(METER, date(2026, 8, 1), date(2026, 10, 7))

    assert _ranges(api) == [
        ("08/01/2026", "08/30/2026"),
        ("08/31/2026", "09/29/2026"),
        ("09/30/2026", "10/07/2026"),
    ]


async def test_daily_returns_local_dates() -> None:
    api = FakeApi(
        [
            _reading("2026-10-08T00:00:00", 12.0),
            _reading("2026-10-07T00:00:00", 10.0),
            _reading("2026-10-09T00:00:00", None),  # skipped
        ]
    )

    days = await _usage(api).daily(METER, date(2026, 9, 1), date(2026, 10, 18))

    assert days == [(date(2026, 10, 7), 10.0), (date(2026, 10, 8), 12.0)]
    assert [r["periodicity"] for r in api.requests] == [PERIODICITY_DAILY]


def _cycle_reading(start, end, usage, cost):
    return UsageReading(
        usage=usage,
        cost=cost,
        reading_date="0001-01-01T01:00:00",
        reading_from=start,
        reading_to=end,
        unit=None,
    )


async def test_billing_cycles_from_yearly_requests_without_duplicates() -> None:
    """Every yearly request returns the same cycle here; it is counted once."""
    api = FakeApi(
        [
            _cycle_reading("2026-09-01T00:00:00+01:00", "2026-10-01T00:00:00+01:00", 400.0, 100.0),
            _cycle_reading("0001-01-01T00:00:00", "0001-01-01T00:00:00", 1.0, 1.0),  # unusable
            _cycle_reading("2026-10-01T00:00:00+01:00", "2026-11-01T00:00:00+01:00", None, 5.0),
        ]
    )

    cycles = await _usage(api).billing_cycles(METER, date(2024, 10, 1), date(2026, 10, 18))

    assert cycles == [
        BillingCycle(
            start=datetime(2026, 9, 1, 5, tzinfo=UTC),
            end=datetime(2026, 10, 1, 5, tzinfo=UTC),
            usage=400.0,
            cost=100.0,
        )
    ]
    assert {r["periodicity"] for r in api.requests} == {PERIODICITY_MONTHLY}
    assert _ranges(api) == [
        ("10/01/2024", "09/30/2025"),
        ("10/01/2025", "09/30/2026"),
        ("10/01/2026", "10/18/2026"),
    ]


async def test_hour_returned_by_two_requests_is_counted_once() -> None:
    api = FakeApi([_reading("2026-08-31T00:00:00+01:00", 2.0)])  # every request returns it

    hours = await _usage(api).hourly(METER, date(2026, 8, 1), date(2026, 9, 15))

    assert len(api.requests) == 2
    assert hours == [(datetime(2026, 8, 31, 5, tzinfo=UTC), 2.0)]


async def test_error_in_a_later_request_fails_the_whole_read() -> None:
    """Backend errors pass through unchanged; a partial read is not returned."""

    class FailsSecond(FakeApi):
        async def async_get_usage(self, **request) -> list[UsageReading]:
            if self.requests:
                raise AlliantEnergyError("HTTP 500")
            return await super().async_get_usage(**request)

    with pytest.raises(AlliantEnergyError):
        await _usage(FailsSecond()).hourly(METER, date(2026, 8, 1), date(2026, 9, 15))
