"""An in-memory meter usage source for tests (the second MeterUsage adapter)."""

from __future__ import annotations

from datetime import date, datetime, tzinfo

from custom_components.alliant_energy.api import AlliantEnergyError, Meter
from custom_components.alliant_energy.rates import BillingCycle


class FakeUsage:
    """Answers reads from canned usage and records each read.

    Readings are shared by every meter. ``fail_before`` makes hourly reads that
    start before that date raise, like a backend that errors for dates before
    the history begins.
    """

    def __init__(
        self,
        tz: tzinfo,
        hourly: list[tuple[datetime, float]] | None = None,
        daily: list[tuple[date, float]] | None = None,
        cycles: list[BillingCycle] | None = None,
        fail_before: date | None = None,
    ) -> None:
        self._tz = tz
        self._hourly = hourly or []
        self._daily = daily or []
        self._cycles = cycles or []
        self._fail_before = fail_before
        # (read, meter number, start, end) of every read, in order.
        self.reads: list[tuple[str, str, date, date]] = []

    async def hourly(self, meter: Meter, start: date, end: date) -> list[tuple[datetime, float]]:
        self.reads.append(("hourly", meter.meter_number, start, end))
        if self._fail_before is not None and start < self._fail_before:
            raise AlliantEnergyError("HTTP 500")
        return [
            (hour, value)
            for hour, value in self._hourly
            if start <= hour.astimezone(self._tz).date() <= end
        ]

    async def daily(self, meter: Meter, start: date, end: date) -> list[tuple[date, float]]:
        self.reads.append(("daily", meter.meter_number, start, end))
        return [(day, value) for day, value in self._daily if start <= day <= end]

    async def billing_cycles(self, meter: Meter, start: date, end: date) -> list[BillingCycle]:
        self.reads.append(("billing_cycles", meter.meter_number, start, end))
        return [
            cycle
            for cycle in self._cycles
            if cycle.start.astimezone(self._tz).date() <= end
            and cycle.end.astimezone(self._tz).date() >= start
        ]

    def ranges(self, read: str) -> list[tuple[str, str]]:
        """The (start, end) of every read of one kind, as MM/DD/YYYY strings."""
        return [
            (start.strftime("%m/%d/%Y"), end.strftime("%m/%d/%Y"))
            for kind, _meter, start, end in self.reads
            if kind == read
        ]
