"""Home Assistant-facing client that orchestrates login, usage, and statistics.

Adapts the low-level :mod:`.api` client to what the coordinator needs: it owns
the account/premise identity, imports hourly long-term statistics for the Energy
dashboard, and produces the latest usage and most-recent bill cost for sensors.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import date, timedelta

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import statistics as stats
from .api import (
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    Meter,
)
from .bill import Bill, LatestBill, PastBills
from .billparser import ItemizedBill, ServiceBill
from .const import (
    BILL_PARSER,
    CONF_ACCOUNT_ID,
    CONF_ELECTRICITY_RATE,
    CONF_GAS_RATE,
    CONF_PASSWORD,
    CONF_PREMISE_ID,
    CONF_USERNAME,
    CURRENCY_USD,
    UTILITY,
)
from .models import EnergyData, ServiceData
from .options import max_history_days
from .rates import (
    RATE_SOURCE_BILLING_CYCLE,
    BillingCycle,
    KnownRates,
    MarginalRate,
    known_cost_rate,
)
from .usage import AlliantUsage

_LOGGER = logging.getLogger(__name__)

# Long enough to cover the whole billing cycle in progress (about a month).
_LOOKBACK_DAYS = 45
_COST_LOOKBACK_DAYS = 120

__all__ = [
    "AlliantEnergyAuthError",
    "AlliantEnergyError",
    "LocalUtilityClient",
]


class LocalUtilityClient:
    """Fetches readings and imports statistics for one account/premise."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        # The client owns a curl_cffi session; aiohttp is blocked by Cloudflare.
        self._api = AlliantEnergyApi(UTILITY)
        self._meters: list[Meter] | None = None
        self._premise: str | None = entry.data.get(CONF_PREMISE_ID)
        self._logged_in = False
        self._usage: AlliantUsage | None = None
        self._context_lock = asyncio.Lock()
        self._login_rejected: AlliantEnergyAuthError | None = None
        self._bills = LatestBill(hass, self._api, self._account_id, entry.entry_id, BILL_PARSER)
        self._past_bills = PastBills(
            hass,
            self._api,
            self._account_id,
            entry.entry_id,
            BILL_PARSER,
            latest=self._bills,
        )

    @property
    def _account_id(self) -> str:
        return str(self.entry.data[CONF_ACCOUNT_ID])

    async def async_close(self) -> None:
        """Release the underlying HTTP session."""
        await self._api.close()

    async def async_fetch_data(self) -> EnergyData:
        """Return the latest usage and most-recent bill cost for sensors."""
        try:
            usage, meters = await self._async_ensure_context()
        except AlliantEnergyAuthError:
            self._logged_in = False
            raise
        return await self._async_build_energy_data(usage, meters)

    async def async_update_statistics(self, bill: Bill) -> None:
        """Import hourly long-term statistics for the Energy dashboard.

        Runs as a background task after each sensor refresh (see __init__),
        priced with the bill that refresh fetched, so the history backfill does
        not block setup.
        """
        try:
            usage, meters = await self._async_ensure_context()
            await stats.async_update_statistics(
                self.hass,
                usage,
                self._account_id,
                meters,
                rates=self._known_rates(bill),
                max_history_days=max_history_days(self.entry),
                past_bills=self._past_bills,
            )
        except AlliantEnergyAuthError:
            self._logged_in = False
            raise

    def _known_rates(self, bill: Bill) -> dict[str, KnownRates]:
        """Each service's static rate (options) and marginal rate (itemized bill)."""
        known: dict[str, KnownRates] = {}
        for service, option_key in (
            (SERVICE_ELECTRIC, CONF_ELECTRICITY_RATE),
            (SERVICE_GAS, CONF_GAS_RATE),
        ):
            section = bill.section(service)
            marginal = section.marginal_rate if section else None
            known[service] = KnownRates(
                static=_option_float(self.entry.options.get(option_key)),
                marginals=(
                    (MarginalRate(marginal, section.period_start),)
                    if section and marginal is not None
                    else ()
                ),
            )
        return known

    async def _async_ensure_context(self) -> tuple[AlliantUsage, list[Meter]]:
        # Overlapping updates (a refresh during a running import) must log in,
        # and discover the premise and meters, only once (ADR 0007).
        async with self._context_lock:
            return await self._async_ensure_context_locked()

    async def _async_ensure_context_locked(self) -> tuple[AlliantUsage, list[Meter]]:
        if self._login_rejected is not None:
            # Never retry a rejected password; reauthentication reloads the
            # entry with a new client (ADR 0007).
            raise self._login_rejected
        if not self._logged_in:
            try:
                await self._api.async_login(
                    self.entry.data[CONF_USERNAME], self.entry.data[CONF_PASSWORD]
                )
            except AlliantEnergyAuthError as err:
                self._login_rejected = err
                raise
            self._logged_in = True
        if not self._premise:
            for account in await self._api.async_get_accounts():
                if account.account_number == self._account_id:
                    self._premise = account.premise_number
                    break
            if not self._premise:
                raise AlliantEnergyError(f"No premise found for account {self._account_id}")
        if self._meters is None:
            self._meters = await self._api.async_get_meters(self._account_id, self._premise)
        if self._usage is None:
            self._usage = AlliantUsage(self._api, self._account_id, self._premise)
        return self._usage, self._meters

    async def _async_build_energy_data(
        self, usage: AlliantUsage, meters: list[Meter]
    ) -> EnergyData:
        today = dt_util.now().date()
        latest: dict[str, tuple[date, float]] = {}
        daily_by_service: dict[str, list[tuple[date, float]]] = {}
        cycles_by_service: dict[str, list[BillingCycle]] = {}
        cost: dict[str, float] = {}
        for meter in meters:
            daily = await usage.daily(meter, today - timedelta(days=_LOOKBACK_DAYS), today)
            daily_by_service[meter.service_type] = daily
            if daily:
                latest[meter.service_type] = daily[-1]

            cycles = await usage.billing_cycles(
                meter, today - timedelta(days=_COST_LOOKBACK_DAYS), today
            )
            cycles_by_service[meter.service_type] = cycles
            # The newest billed cycle; a cycle at 0 is not billed yet.
            if billed := [cycle.cost for cycle in cycles if cycle.cost]:
                cost[meter.service_type] = billed[-1]

        bill = await self._bills.latest()
        known_rates = self._known_rates(bill)
        tz = dt_util.get_default_time_zone()
        services: dict[str, ServiceData] = {}
        for service in (SERVICE_ELECTRIC, SERVICE_GAS):
            latest_day = latest.get(service)
            # Show the cost rate the cost statistic uses for the newest hour.
            rate = known_cost_rate(known_rates[service], cycles_by_service.get(service, []), tz)
            value, source = rate.current if rate else (None, None)
            data = ServiceData(
                latest_usage=latest_day[1] if latest_day else None,
                last_bill_cost=cost.get(service),
                rate=value,
                rate_source=source,
                section=bill.section(service),
                adjustment=bill.adjustment(service),
            )
            # Project the cost of the billing cycle in progress.
            section = data.section
            if section is not None and section.period_end is not None:
                data = _project(
                    data,
                    daily_by_service.get(service, []),
                    section.period_end,
                    _expected_next_read(bill.itemized, section),
                )
            services[service] = data
        projected = [d.projected_cost for d in services.values() if d.projected_cost is not None]
        return EnergyData(
            bill=bill,
            services=services,
            currency=CURRENCY_USD,
            projected_bill=round(sum(projected), 2) if projected else None,
        )


def _to_float(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _option_float(value: object) -> float | None:
    """Coerce a stored option to a positive float, or None if unset/invalid."""
    rate = _to_float(value)
    return rate if rate is not None and rate > 0 else None


def _expected_next_read(bill: ItemizedBill | None, section: ServiceBill) -> date | None:
    """When the cycle in progress should end: the middle of the next read window.

    Falls back to the last period's length when the bill has no window.
    """
    if bill is not None and bill.next_read_start and bill.next_read_end:
        return bill.next_read_start + (bill.next_read_end - bill.next_read_start) / 2
    if section.period_end is not None and section.billed_days:
        return section.period_end + timedelta(days=section.billed_days)
    return None


def _project(
    data: ServiceData,
    daily: list[tuple[date, float]],
    cycle_start: date,
    cycle_end: date | None,
) -> ServiceData:
    """Estimate the cost of the billing cycle in progress.

    The cycle starts on the day of the last meter reading. Average daily use so
    far is extended to the expected cycle length and priced at the service's
    rate, plus the daily customer charge when the rate excludes it (fixed or
    bill rate; a billing-cycle effective rate already includes it). Fees and
    taxes are not included.
    """
    if data.rate is None or cycle_end is None:
        return data
    cycle_days = (cycle_end - cycle_start).days
    by_day = {day: value for day, value in daily if day >= cycle_start}
    if not by_day or cycle_days <= 0:
        return data
    usage = sum(by_day.values())
    days = len(by_day)
    charge = data.section.customer_charge_per_day if data.section else None
    fixed = (charge or 0.0) if data.rate_source != RATE_SOURCE_BILLING_CYCLE else 0.0
    return replace(
        data,
        usage_to_date=round(usage, 3),
        days_to_date=days,
        cycle_days=cycle_days,
        cost_to_date=round(usage * data.rate + fixed * days, 2),
        projected_cost=round(usage / days * cycle_days * data.rate + fixed * cycle_days, 2),
    )
