"""Long-term statistics import for the Home Assistant Energy dashboard.

Imports hourly usage per meter as external statistics with a cumulative sum,
following the pattern used by the built-in ``opower`` integration: read the last
imported statistic, continue its running sum, and append new hours. On first run
it backfills a window of history in gentle chunks.

Gas is reported by Alliant in therms, which the Energy dashboard does not
accept, so consumption is stored as energy in kWh (1 therm = 29.3001 kWh, exact).

Cost statistics are imported alongside consumption when a rate is available.
The caller resolves a static or bill-PDF marginal rate per service; without one,
an effective rate per billing cycle is derived from the monthly bill
(amount / consumption). See docs/adr/0005-cost-rate-precedence.md.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, tzinfo

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    PERIODICITY_HOURLY,
    PERIODICITY_MONTHLY,
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyApi,
    Meter,
)
from .const import (
    DOMAIN,
    STATISTICS_BACKFILL_DAYS,
    STATISTICS_CHUNK_DAYS,
    STATISTICS_LAG_DAYS,
    THERM_TO_KWH,
)

_LOGGER = logging.getLogger(__name__)

_DATE_FMT = "%m/%d/%Y"
# Gentle pause between history chunk requests on first-run backfill.
_CHUNK_DELAY = 1.0
# How far back to look for billing periods when deriving an effective rate.
_RATE_LOOKBACK_DAYS = 400

# A billing period with an effective per-unit rate.
_RatePeriod = tuple[datetime, datetime, float]


def statistic_id(account_number: str, meter: Meter, suffix: str = "") -> str:
    """Return an external statistic id for a meter (optionally a '_cost' id)."""
    base = f"{DOMAIN}:{account_number}_{meter.service_type}_{meter.meter_number}"
    return f"{base}{suffix}".lower()


async def async_update_statistics(
    hass: HomeAssistant,
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meters: list[Meter],
    electricity_rate: float | None = None,
    gas_rate: float | None = None,
) -> None:
    """Import hourly usage (and cost, when a rate is known) for every meter."""
    tz = await dt_util.async_get_time_zone(hass.config.time_zone) or dt_util.UTC
    for meter in meters:
        rate = electricity_rate if meter.service_type == SERVICE_ELECTRIC else gas_rate
        await _async_update_meter(hass, api, account_number, premise_number, meter, tz, rate)


async def _async_update_meter(
    hass: HomeAssistant,
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    static_rate: float | None,
) -> None:
    is_gas = meter.service_type == SERVICE_GAS

    # Native hourly readings (electric kWh, gas therms).
    native = await _async_fetch_hourly(api, account_number, premise_number, meter, tz)
    if not native:
        return

    # Consumption is always stored in kWh.
    consumption = [(hour, value * THERM_TO_KWH if is_gas else value) for hour, value in native]
    await _async_import_series(
        hass,
        statistic_id(account_number, meter),
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Alliant Energy {meter.service_type} {meter.meter_number}",
            source=DOMAIN,
            statistic_id=statistic_id(account_number, meter),
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        ),
        consumption,
    )

    cost = await _async_cost_series(
        api, account_number, premise_number, meter, tz, native, static_rate
    )
    if cost is None:
        return
    cost_id = statistic_id(account_number, meter, "_cost")
    await _async_import_series(
        hass,
        cost_id,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Alliant Energy {meter.service_type} {meter.meter_number} cost",
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=None,
        ),
        cost,
    )


async def _async_cost_series(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    native: list[tuple[datetime, float]],
    static_rate: float | None,
) -> list[tuple[datetime, float]] | None:
    """Return hourly (hour, cost) points, or None when no rate is available.

    Cost is charged on the native unit: electric kWh x $/kWh, gas therms x
    $/therm. The effective rate per billing period comes from the monthly bill
    (amount / consumption) for both fuels; a static rate from options overrides
    it.
    """
    if static_rate is not None:
        return [(hour, value * static_rate) for hour, value in native]
    periods = await _async_rate_periods(api, account_number, premise_number, meter, tz)
    if not periods:
        return None
    return [(hour, value * _rate_for(periods, hour)) for hour, value in native]


async def _async_rate_periods(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
) -> list[_RatePeriod]:
    """Derive an effective $/unit for each billing period from monthly bills."""
    today = dt_util.now(tz).date()
    readings = await api.async_get_usage(
        meter.service_type,
        account_number,
        premise_number,
        meter.meter_number,
        (today - timedelta(days=_RATE_LOOKBACK_DAYS)).strftime(_DATE_FMT),
        today.strftime(_DATE_FMT),
        periodicity=PERIODICITY_MONTHLY,
    )
    periods: list[_RatePeriod] = []
    for reading in readings:
        if not reading.usage or reading.cost is None:
            continue
        start = _parse_hour(reading.reading_from, tz)
        end = _parse_hour(reading.reading_to, tz)
        if start is None or end is None or start >= end:
            continue
        periods.append((start, end, reading.cost / reading.usage))
    periods.sort()
    return periods


def _rate_for(periods: list[_RatePeriod], hour: datetime) -> float:
    """Pick the rate for the billing period containing ``hour``.

    Hours before the first period use the earliest rate; hours after the last
    (e.g. the current, unbilled period) use the most recent rate.
    """
    for start, end, rate in periods:
        if start <= hour < end:
            return rate
    if hour < periods[0][0]:
        return periods[0][2]
    return periods[-1][2]


async def _async_import_series(
    hass: HomeAssistant,
    stat_id: str,
    metadata: StatisticMetaData,
    points: list[tuple[datetime, float]],
) -> None:
    """Append (hour, value) points to a statistic, continuing its running sum."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    if last.get(stat_id):
        row = last[stat_id][0]
        last_ts: float | None = row["start"]
        running_sum: float = row.get("sum") or 0.0
    else:
        last_ts = None
        running_sum = 0.0

    statistics: list[StatisticData] = []
    for hour, value in points:
        if last_ts is not None and hour.timestamp() <= last_ts:
            continue
        running_sum += value
        statistics.append(StatisticData(start=hour, state=value, sum=running_sum))

    if statistics:
        _LOGGER.debug("Adding %d statistics for %s", len(statistics), stat_id)
        async_add_external_statistics(hass, metadata, statistics)


async def _async_fetch_hourly(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
) -> list[tuple[datetime, float]]:
    """Fetch hourly readings across the backfill window in chunks.

    Returns a sorted list of (UTC hour, native value) with the meter's native
    unit (electric kWh, gas therms). Duplicate hours across chunk boundaries are
    de-duplicated.
    """
    start_date = (dt_util.now(tz) - timedelta(days=STATISTICS_BACKFILL_DAYS)).date()
    end_date = (dt_util.now(tz) - timedelta(days=STATISTICS_LAG_DAYS)).date()

    by_hour: dict[datetime, float] = {}
    chunk_start = start_date
    first = True
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=STATISTICS_CHUNK_DAYS), end_date)
        if not first:
            await asyncio.sleep(_CHUNK_DELAY)
        first = False
        readings = await api.async_get_usage(
            meter.service_type,
            account_number,
            premise_number,
            meter.meter_number,
            chunk_start.strftime(_DATE_FMT),
            chunk_end.strftime(_DATE_FMT),
            periodicity=PERIODICITY_HOURLY,
        )
        for reading in readings:
            if reading.usage is None:
                continue
            hour = _parse_hour(reading.reading_date, tz)
            if hour is not None:
                by_hour[hour] = reading.usage
        chunk_start = chunk_end + timedelta(days=1)

    return sorted(by_hour.items())


def _parse_hour(reading_date: str | None, tz: tzinfo) -> datetime | None:
    """Parse a usage record's timestamp into a UTC hour boundary.

    The API stamps timestamps with a fixed, bogus UTC offset; the wall-clock
    time is the account's local time, so the offset is dropped and the naive
    time is localized to the configured time zone.
    """
    if not reading_date:
        return None
    try:
        parsed = datetime.fromisoformat(reading_date)
    except ValueError:
        return None
    local = parsed.replace(tzinfo=tz)
    return dt_util.as_utc(local).replace(minute=0, second=0, microsecond=0)
