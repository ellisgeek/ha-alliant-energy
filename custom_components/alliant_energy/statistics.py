"""Long-term statistics import for the Home Assistant Energy dashboard.

Imports hourly usage per meter as external statistics with a cumulative sum,
following the pattern used by the built-in ``opower`` integration: read the last
imported statistic, continue its running sum, and append new hours.

The first import of a meter walks back through the API in gentle chunks until
the history runs out, so it imports all the hourly data Alliant has. A sum can
only grow forward, so older hours cannot be added to a statistic later. A meter
whose statistics were not built this way (by an earlier version, or before a
cost rate was known) is therefore cleared and imported again once. A small
store records which meters have their full history. Later runs fetch only from
the last imported day. See docs/adr/0011-import-full-history.md.

Usage is stored in the meter's native unit: kWh for electric and therms
(``thm``) for gas, both energy units the Energy dashboard accepts (see
docs/adr/0010-store-gas-in-therms.md).

Cost statistics are imported alongside consumption when a rate is available.
The caller resolves a static or bill-PDF marginal rate per service; without one,
an effective rate per billing cycle is derived from the monthly bill
(amount / consumption). See docs/adr/0005-cost-rate-precedence.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
from functools import partial

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
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    PERIODICITY_HOURLY,
    PERIODICITY_MONTHLY,
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    Meter,
    UsageReading,
)
from .const import (
    DOMAIN,
    STATISTICS_CHUNK_DAYS,
    STATISTICS_EMPTY_CHUNKS,
    STATISTICS_LAG_DAYS,
    STATISTICS_MAX_HISTORY_DAYS,
)

_LOGGER = logging.getLogger(__name__)

_DATE_FMT = "%m/%d/%Y"
# Gentle pause between history chunk requests on first-run backfill.
_CHUNK_DELAY = 1.0
# How far back to look for billing periods when deriving an effective rate on
# a normal update; a full-history import looks back to its oldest hour.
_RATE_LOOKBACK_DAYS = 400
# Billing-period requests span at most this many days each.
_RATE_CHUNK_DAYS = 365

# Records which meters have their full history imported:
# {"full_history": [<usage statistic id>, ...]}.
_STORE_KEY = f"{DOMAIN}.statistics"
_STORE_VERSION = 1

# A billing period with an effective per-unit rate.
_RatePeriod = tuple[datetime, datetime, float]
# A statistic's last imported hour (UTC timestamp) and its running sum.
_Last = tuple[float, float]


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
    cons_id = statistic_id(account_number, meter)
    cost_id = statistic_id(account_number, meter, "_cost")
    today = dt_util.now(tz).date()

    rate_for = await _async_rate_fn(
        api,
        account_number,
        premise_number,
        meter,
        tz,
        static_rate,
        today - timedelta(days=_RATE_LOOKBACK_DAYS),
    )
    cons_last = await _async_last(hass, cons_id)
    cost_last = await _async_last(hass, cost_id) if rate_for is not None else None

    # Rebuild from the full history when the meter was never imported in full,
    # its usage statistic is missing, or a cost rate is newly available.
    if (
        cons_id not in await _async_full_history_ids(hass)
        or cons_last is None
        or (rate_for is not None and cost_last is None)
    ):
        await _async_import_full_history(
            hass, api, account_number, premise_number, meter, tz, static_rate
        )
        return

    # Otherwise fetch only from the older of the two last imported hours.
    since = cons_last[0] if cost_last is None else min(cons_last[0], cost_last[0])
    native = await _async_fetch_hourly(api, account_number, premise_number, meter, tz, since)
    _import_usage(hass, meter, cons_id, native, cons_last)
    if rate_for is not None:
        _import_cost(hass, meter, cost_id, native, rate_for, cost_last)


async def _async_import_full_history(
    hass: HomeAssistant,
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    static_rate: float | None,
) -> None:
    """Replace a meter's statistics with every hour the API has."""
    cons_id = statistic_id(account_number, meter)
    cost_id = statistic_id(account_number, meter, "_cost")
    _LOGGER.info("Importing the full hourly history for %s", cons_id)

    # Fetch everything before clearing, so a failed fetch keeps the old data.
    native = await _async_fetch_history(api, account_number, premise_number, meter, tz)
    if not native:
        return
    oldest = native[0][0].astimezone(tz).date()
    rate_for = await _async_rate_fn(
        api, account_number, premise_number, meter, tz, static_rate, oldest
    )

    # The recorder runs these in order: clear first, then the new imports.
    get_instance(hass).async_clear_statistics([cons_id, cost_id])
    _import_usage(hass, meter, cons_id, native, None)
    if rate_for is not None:
        _import_cost(hass, meter, cost_id, native, rate_for, None)
    await _async_mark_full_history(hass, cons_id)
    _LOGGER.info("Imported %d hours for %s, back to %s", len(native), cons_id, oldest.isoformat())


def _import_usage(
    hass: HomeAssistant,
    meter: Meter,
    cons_id: str,
    native: list[tuple[datetime, float]],
    last: _Last | None,
) -> None:
    """Import native hourly usage (electric kWh, gas therms) as stored."""
    unit = UnitOfEnergy.THERM if meter.service_type == SERVICE_GAS else UnitOfEnergy.KILO_WATT_HOUR
    _import_series(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Alliant Energy {meter.service_type} {meter.meter_number}",
            source=DOMAIN,
            statistic_id=cons_id,
            unit_class=EnergyConverter.UNIT_CLASS,
            unit_of_measurement=unit,
        ),
        native,
        last,
    )


def _import_cost(
    hass: HomeAssistant,
    meter: Meter,
    cost_id: str,
    native: list[tuple[datetime, float]],
    rate_for: Callable[[datetime], float],
    last: _Last | None,
) -> None:
    """Import hourly cost: native usage x the rate (kWh x $/kWh, therms x $/therm)."""
    _import_series(
        hass,
        StatisticMetaData(
            mean_type=StatisticMeanType.NONE,
            has_sum=True,
            name=f"Alliant Energy {meter.service_type} {meter.meter_number} cost",
            source=DOMAIN,
            statistic_id=cost_id,
            unit_class=None,
            unit_of_measurement=None,
        ),
        [(hour, value * rate_for(hour)) for hour, value in native],
        last,
    )


def _store(hass: HomeAssistant) -> Store[dict[str, list[str]]]:
    return Store(hass, _STORE_VERSION, _STORE_KEY)


async def _async_full_history_ids(hass: HomeAssistant) -> set[str]:
    """Usage statistic ids whose meters have their full history imported."""
    data = await _store(hass).async_load() or {}
    return set(data.get("full_history", []))


async def _async_mark_full_history(hass: HomeAssistant, cons_id: str) -> None:
    ids = await _async_full_history_ids(hass) | {cons_id}
    await _store(hass).async_save({"full_history": sorted(ids)})


async def _async_rate_fn(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    static_rate: float | None,
    since: date,
) -> Callable[[datetime], float] | None:
    """Return the $/unit rate for an hour, or None when no rate is available.

    A static rate (from options or the bill PDF) wins; otherwise each hour uses
    the effective rate of its billing period (amount / consumption), using
    billing periods back to ``since``.
    """
    if static_rate is not None:
        return lambda _hour: static_rate
    periods = await _async_rate_periods(api, account_number, premise_number, meter, tz, since)
    if not periods:
        return None
    return partial(_rate_for, periods)


async def _async_rate_periods(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    since: date,
) -> list[_RatePeriod]:
    """Derive an effective $/unit for each billing period since a date."""
    periods: dict[datetime, _RatePeriod] = {}
    chunk_end = dt_util.now(tz).date()
    first = True
    while chunk_end >= since:
        chunk_start = max(chunk_end - timedelta(days=_RATE_CHUNK_DAYS), since)
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
            periodicity=PERIODICITY_MONTHLY,
        )
        for reading in readings:
            if not reading.usage or reading.cost is None:
                continue
            start = _parse_hour(reading.reading_from, tz)
            end = _parse_hour(reading.reading_to, tz)
            if start is None or end is None or start >= end:
                continue
            periods[start] = (start, end, reading.cost / reading.usage)
        chunk_end = chunk_start - timedelta(days=1)
    return sorted(periods.values())


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


async def _async_last(hass: HomeAssistant, stat_id: str) -> _Last | None:
    """Return the last imported hour (UTC timestamp) and running sum, if any."""
    last = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, stat_id, True, {"sum"}
    )
    if not last.get(stat_id):
        return None
    row = last[stat_id][0]
    return row["start"], row.get("sum") or 0.0


def _import_series(
    hass: HomeAssistant,
    metadata: StatisticMetaData,
    points: list[tuple[datetime, float]],
    last: _Last | None,
) -> None:
    """Append (hour, value) points after ``last``, continuing its running sum."""
    last_ts, running_sum = last if last is not None else (None, 0.0)

    statistics: list[StatisticData] = []
    for hour, value in points:
        if last_ts is not None and hour.timestamp() <= last_ts:
            continue
        running_sum += value
        statistics.append(StatisticData(start=hour, state=value, sum=running_sum))

    if statistics:
        _LOGGER.debug("Adding %d statistics for %s", len(statistics), metadata["statistic_id"])
        async_add_external_statistics(hass, metadata, statistics)


async def _async_fetch_hourly(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    since: float,
) -> list[tuple[datetime, float]]:
    """Fetch hourly readings forward from the last imported hour, in chunks.

    ``since`` is the last imported hour (UTC timestamp); fetching restarts at
    its local date so a partly reported day is completed. Returns a sorted list
    of (UTC hour, native value) in the meter's native unit.
    """
    start_date = datetime.fromtimestamp(since, tz).date()
    end_date = (dt_util.now(tz) - timedelta(days=STATISTICS_LAG_DAYS)).date()

    by_hour: dict[datetime, float] = {}
    chunk_start = start_date
    first = True
    while chunk_start <= end_date:
        chunk_end = min(chunk_start + timedelta(days=STATISTICS_CHUNK_DAYS), end_date)
        if not first:
            await asyncio.sleep(_CHUNK_DELAY)
        first = False
        _collect(
            await _async_get_hourly(
                api, account_number, premise_number, meter, chunk_start, chunk_end
            ),
            tz,
            by_hour,
        )
        chunk_start = chunk_end + timedelta(days=1)
    return sorted(by_hour.items())


async def _async_fetch_history(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
) -> list[tuple[datetime, float]]:
    """Fetch every hourly reading the API has, walking back in chunks.

    Stops after ``STATISTICS_EMPTY_CHUNKS`` chunks in a row with no data (so a
    short gap, such as a meter swap, does not end the history), or at the
    ``STATISTICS_MAX_HISTORY_DAYS`` safety limit. Returns a sorted list of (UTC
    hour, native value).
    """
    now = dt_util.now(tz)
    oldest = (now - timedelta(days=STATISTICS_MAX_HISTORY_DAYS)).date()
    chunk_end = (now - timedelta(days=STATISTICS_LAG_DAYS)).date()

    by_hour: dict[datetime, float] = {}
    empty = 0
    first = True
    while chunk_end >= oldest and empty < STATISTICS_EMPTY_CHUNKS:
        chunk_start = max(chunk_end - timedelta(days=STATISTICS_CHUNK_DAYS), oldest)
        if not first:
            await asyncio.sleep(_CHUNK_DELAY)
        first = False
        try:
            readings = await _async_get_hourly(
                api, account_number, premise_number, meter, chunk_start, chunk_end
            )
        except AlliantEnergyAuthError:
            raise
        except AlliantEnergyError as err:
            # Requests for dates before the history starts may fail instead of
            # returning nothing; once some data is in hand, treat it as the end.
            if not by_hour:
                raise
            _LOGGER.warning(
                "Stopping the history of meter %s at %s: %s",
                meter.meter_number,
                chunk_end.isoformat(),
                err,
            )
            break
        empty = 0 if _collect(readings, tz, by_hour) else empty + 1
        chunk_end = chunk_start - timedelta(days=1)
    return sorted(by_hour.items())


async def _async_get_hourly(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    start: date,
    end: date,
) -> list[UsageReading]:
    return await api.async_get_usage(
        meter.service_type,
        account_number,
        premise_number,
        meter.meter_number,
        start.strftime(_DATE_FMT),
        end.strftime(_DATE_FMT),
        periodicity=PERIODICITY_HOURLY,
    )


def _collect(readings: list[UsageReading], tz: tzinfo, by_hour: dict[datetime, float]) -> int:
    """Add readings to ``by_hour`` (de-duplicating hours); return how many had usage."""
    count = 0
    for reading in readings:
        if reading.usage is None:
            continue
        hour = _parse_hour(reading.reading_date, tz)
        if hour is not None:
            by_hour[hour] = reading.usage
            count += 1
    return count


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
