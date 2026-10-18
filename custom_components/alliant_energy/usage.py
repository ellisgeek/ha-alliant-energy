"""Read a meter's usage readings, hiding the backend's request format.

Callers ask for usage by date range and get clean values back: UTC hours for
hourly readings, local dates for daily readings, and billing cycles. The
backend's date strings, periodicity codes, and timestamp quirks stay here.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta, tzinfo
from typing import Protocol

import homeassistant.util.dt as dt_util

from .api import (
    PERIODICITY_DAILY,
    PERIODICITY_HOURLY,
    PERIODICITY_MONTHLY,
    AlliantEnergyApi,
    Meter,
    UsageReading,
)
from .const import REQUEST_PAUSE_SECONDS, STATISTICS_CHUNK_DAYS
from .rates import BillingCycle

_DATE_FMT = "%m/%d/%Y"
# Daily and billing-cycle requests span at most this many days each.
_LONG_CHUNK_DAYS = 365

# (UTC hour, value) points, oldest first.
Hours = list[tuple[datetime, float]]


class MeterUsage(Protocol):
    """Usage readings for the meters of one account and premise."""

    async def hourly(self, meter: Meter, start: date, end: date) -> Hours:
        """Hourly usage from ``start`` through ``end`` (local dates)."""
        ...

    async def daily(self, meter: Meter, start: date, end: date) -> list[tuple[date, float]]:
        """Daily usage from ``start`` through ``end``, oldest first."""
        ...

    async def billing_cycles(self, meter: Meter, start: date, end: date) -> list[BillingCycle]:
        """Billing cycles reported from ``start`` through ``end``, oldest first."""
        ...


class AlliantUsage:
    """Usage readings from the Alliant backend for one account and premise.

    Requests are paced: each waits until ``pause`` seconds have passed since
    the previous one, across every read (ADR 0007).
    """

    def __init__(
        self,
        api: AlliantEnergyApi,
        account_number: str,
        premise_number: str,
        tz: tzinfo,
        pause: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._api = api
        self._account = account_number
        self._premise = premise_number
        self._tz = tz
        self._pause = REQUEST_PAUSE_SECONDS if pause is None else pause
        self._sleep = sleep
        self._clock = clock
        self._last_request: float | None = None

    async def hourly(self, meter: Meter, start: date, end: date) -> Hours:
        by_hour: dict[datetime, float] = {}
        for reading in await self._get(
            meter, start, end, PERIODICITY_HOURLY, STATISTICS_CHUNK_DAYS
        ):
            if reading.usage is None:
                continue
            if (hour := _parse_hour(reading.reading_date, self._tz)) is not None:
                by_hour[hour] = reading.usage
        return sorted(by_hour.items())

    async def daily(self, meter: Meter, start: date, end: date) -> list[tuple[date, float]]:
        by_day: dict[date, float] = {}
        for reading in await self._get(meter, start, end, PERIODICITY_DAILY, _LONG_CHUNK_DAYS):
            if reading.usage is None or not reading.reading_date:
                continue
            try:
                by_day[date.fromisoformat(reading.reading_date[:10])] = reading.usage
            except ValueError:
                continue
        return sorted(by_day.items())

    async def billing_cycles(self, meter: Meter, start: date, end: date) -> list[BillingCycle]:
        cycles: dict[datetime, BillingCycle] = {}  # by start: a cycle can span two requests
        for reading in await self._get(meter, start, end, PERIODICITY_MONTHLY, _LONG_CHUNK_DAYS):
            cycle_start = _parse_hour(reading.reading_from, self._tz)
            cycle_end = _parse_hour(reading.reading_to, self._tz)
            if cycle_start is None or cycle_end is None or cycle_start >= cycle_end:
                continue
            if reading.usage is None:
                continue
            cycles[cycle_start] = BillingCycle(cycle_start, cycle_end, reading.usage, reading.cost)
        return sorted(cycles.values(), key=lambda cycle: cycle.start)

    async def _get(
        self, meter: Meter, start: date, end: date, periodicity: str, chunk_days: int
    ) -> list[UsageReading]:
        """Readings from ``start`` through ``end``, at most ``chunk_days`` per request."""
        readings: list[UsageReading] = []
        chunk_start = start
        while chunk_start <= end:
            chunk_end = min(chunk_start + timedelta(days=chunk_days), end)
            await self._wait_for_pause()
            readings.extend(
                await self._api.async_get_usage(
                    service_type=meter.service_type,
                    account_number=self._account,
                    premise_number=self._premise,
                    meter_number=meter.meter_number,
                    from_date=chunk_start.strftime(_DATE_FMT),
                    to_date=chunk_end.strftime(_DATE_FMT),
                    periodicity=periodicity,
                )
            )
            chunk_start = chunk_end + timedelta(days=1)
        return readings

    async def _wait_for_pause(self) -> None:
        if self._last_request is not None:
            wait = self._pause - (self._clock() - self._last_request)
            if wait > 0:
                await self._sleep(wait)
        self._last_request = self._clock()


def _parse_hour(reading_date: str | None, tz: tzinfo) -> datetime | None:
    """Parse a usage record's timestamp into a UTC hour boundary.

    The API stamps timestamps with a fixed, bogus UTC offset; the wall-clock
    time is the account's local time, so the offset is dropped and the naive
    time is localized to the configured time zone.
    """
    if not reading_date or reading_date.startswith("0001"):  # the API's "no date"
        return None
    try:
        parsed = datetime.fromisoformat(reading_date)
    except ValueError:
        return None
    local = parsed.replace(tzinfo=tz)
    return dt_util.as_utc(local).replace(minute=0, second=0, microsecond=0)
