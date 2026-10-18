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

Cost statistics are imported alongside consumption when a cost rate is known
(see :mod:`.rates`). Cost is append-only: imported hours keep the rate they were
priced with. Only a change to the static rate reprices a cost statistic, from
the hourly usage already stored, with no new history requests. See
docs/adr/0012-price-old-hours-by-billing-cycle.md.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.components.recorder.models import (
    StatisticData,
    StatisticMeanType,
    StatisticMetaData,
)
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .api import (
    PERIODICITY_HOURLY,
    PERIODICITY_MONTHLY,
    SERVICE_GAS,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    Meter,
    UsageReading,
)
from .const import (
    DEFAULT_MAX_HISTORY_DAYS,
    DOMAIN,
    STATISTICS_CHUNK_DAYS,
    STATISTICS_EMPTY_CHUNKS,
    STATISTICS_LAG_DAYS,
)
from .rates import BillingCycle, CostRate, KnownRates, cost_identity, known_cost_rate

_LOGGER = logging.getLogger(__name__)

_DATE_FMT = "%m/%d/%Y"
# Gentle pause between history chunk requests on first-run backfill.
_CHUNK_DELAY = 1.0
# How far back to look for billing cycles when deriving effective rates on a
# normal update; a full-history import looks back to its oldest hour.
_RATE_LOOKBACK_DAYS = 400
# Billing-cycle requests span at most this many days each.
_RATE_CHUNK_DAYS = 365

# Records which meters have their full history imported, with which history
# limit, and which cost rate each cost statistic was priced under:
# {"full_history": {<usage id>: <max history days>}, "cost_rates": {<cost id>:
# <cost rate identity>}}. The first version stored a list of usage ids; those
# were built with the default limit. A cost statistic without an identity was
# priced under the rule before ADR 0012 and is repriced once.
_STORE_KEY = f"{DOMAIN}.statistics"
_STORE_VERSION = 1

# A statistic's last imported hour (UTC timestamp) and its running sum.
_Last = tuple[float, float]
# Hourly (UTC hour, native value) points.
_Hours = list[tuple[datetime, float]]


@dataclass
class _Imported:
    """What the store records about earlier imports."""

    full_history: dict[str, int] = field(default_factory=dict)
    cost_rates: dict[str, str] = field(default_factory=dict)


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
    rates: Mapping[str, KnownRates],
    max_history_days: int = DEFAULT_MAX_HISTORY_DAYS,
) -> None:
    """Import hourly usage (and cost, when a cost rate is known) for every meter.

    ``rates`` holds the static and marginal rate known for each service.
    """
    tz = await dt_util.async_get_time_zone(hass.config.time_zone) or dt_util.UTC
    for meter in meters:
        known = rates.get(meter.service_type) or KnownRates()
        await _async_update_meter(
            hass, api, account_number, premise_number, meter, tz, known, max_history_days
        )


async def _async_update_meter(
    hass: HomeAssistant,
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    known: KnownRates,
    max_days: int,
) -> None:
    cons_id = statistic_id(account_number, meter)
    cost_id = statistic_id(account_number, meter, "_cost")
    imported = await _async_load_imported(hass)
    cons_last = await _async_last(hass, cons_id)

    # Rebuild from the full history when the meter was never imported in full
    # (or was imported with a different history limit), or its usage
    # statistic is missing.
    if imported.full_history.get(cons_id) != max_days or cons_last is None:
        await _async_import_full_history(
            hass, api, account_number, premise_number, meter, tz, known, max_days
        )
        return

    cost_last = await _async_last(hass, cost_id)
    reprice = cost_last is None or imported.cost_rates.get(cost_id) != cost_identity(known.static)

    # Fetch only from the older of the two last imported hours.
    since = cons_last[0] if cost_last is None or reprice else min(cons_last[0], cost_last[0])
    native = await _async_fetch_hourly(api, account_number, premise_number, meter, tz, since)

    if not reprice:
        rate = await _async_cost_rate(
            api, account_number, premise_number, meter, tz, known, _local_date(since, tz)
        )
        _import_usage(hass, meter, cons_id, native, cons_last)
        if rate is not None:
            _import_cost(hass, meter, cost_id, native, rate, cost_last)
        return

    # Reprice the whole cost statistic: the usage already stored plus the new
    # hours, without fetching the history again.
    stored = await _async_hours(hass, cons_id)
    _import_usage(hass, meter, cons_id, native, cons_last)
    hours = stored + [(hour, value) for hour, value in native if hour.timestamp() > cons_last[0]]
    if not hours:
        return
    rate = await _async_cost_rate(
        api, account_number, premise_number, meter, tz, known, hours[0][0].astimezone(tz).date()
    )
    if rate is None:
        if cost_last is not None:
            _LOGGER.warning(
                "No cost rate for %s; keeping its cost history and adding no new hours", cost_id
            )
        return
    _LOGGER.info("Repricing the cost history of %s", cost_id)
    get_instance(hass).async_clear_statistics([cost_id])
    _import_cost(hass, meter, cost_id, hours, rate, None)
    imported.cost_rates[cost_id] = rate.identity
    await _async_save_imported(hass, imported)


async def _async_import_full_history(
    hass: HomeAssistant,
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    known: KnownRates,
    max_days: int,
) -> None:
    """Replace a meter's statistics with every hour the API has, up to the limit."""
    cons_id = statistic_id(account_number, meter)
    cost_id = statistic_id(account_number, meter, "_cost")
    _LOGGER.info("Importing the full hourly history for %s", cons_id)

    # Fetch everything before clearing, so a failed fetch keeps the old data.
    native = await _async_fetch_history(api, account_number, premise_number, meter, tz, max_days)
    if not native:
        return
    oldest = native[0][0].astimezone(tz).date()
    rate = await _async_cost_rate(api, account_number, premise_number, meter, tz, known, oldest)

    # The recorder runs these in order: clear first, then the new imports.
    get_instance(hass).async_clear_statistics([cons_id, cost_id])
    _import_usage(hass, meter, cons_id, native, None)
    imported = await _async_load_imported(hass)
    imported.full_history[cons_id] = max_days
    imported.cost_rates.pop(cost_id, None)
    if rate is not None:
        _import_cost(hass, meter, cost_id, native, rate, None)
        imported.cost_rates[cost_id] = rate.identity
    await _async_save_imported(hass, imported)
    _LOGGER.info("Imported %d hours for %s, back to %s", len(native), cons_id, oldest.isoformat())


def _import_usage(
    hass: HomeAssistant,
    meter: Meter,
    cons_id: str,
    native: _Hours,
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
    native: _Hours,
    rate: CostRate,
    last: _Last | None,
) -> None:
    """Import hourly cost: native usage x the cost rate (kWh x $/kWh, therms x $/therm)."""
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
        [(hour, value * rate(hour)) for hour, value in native],
        last,
    )


def _store(hass: HomeAssistant) -> Store[dict[str, Any]]:
    return Store(hass, _STORE_VERSION, _STORE_KEY)


async def _async_load_imported(hass: HomeAssistant) -> _Imported:
    data = await _store(hass).async_load() or {}
    stored = data.get("full_history") or {}
    if isinstance(stored, list):  # first version: ids only, built with the default
        full_history = dict.fromkeys(stored, DEFAULT_MAX_HISTORY_DAYS)
    else:
        full_history = {str(key): int(value) for key, value in stored.items()}
    cost_rates = {str(key): str(value) for key, value in (data.get("cost_rates") or {}).items()}
    return _Imported(full_history, cost_rates)


async def _async_save_imported(hass: HomeAssistant, imported: _Imported) -> None:
    await _store(hass).async_save(
        {
            "full_history": dict(sorted(imported.full_history.items())),
            "cost_rates": dict(sorted(imported.cost_rates.items())),
        }
    )


async def _async_cost_rate(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    known: KnownRates,
    since: date,
) -> CostRate | None:
    """Return the cost rate for hours from ``since`` on, or None when none is known.

    Billing cycles are read only when an hour from ``since`` on needs an
    effective rate: never with a static rate, and with a marginal rate only for
    hours before its billing cycle.
    """
    cycles: list[BillingCycle] = []
    needs_cycles = known.static is None and (
        known.marginal is None or known.marginal_from is None or since < known.marginal_from
    )
    if needs_cycles:
        lookback = dt_util.now(tz).date() - timedelta(days=_RATE_LOOKBACK_DAYS)
        cycles = await _async_billing_cycles(
            api, account_number, premise_number, meter, tz, min(since, lookback)
        )
    return known_cost_rate(known, cycles, tz)


async def _async_billing_cycles(
    api: AlliantEnergyApi,
    account_number: str,
    premise_number: str,
    meter: Meter,
    tz: tzinfo,
    since: date,
) -> list[BillingCycle]:
    """Read a meter's billing cycles since a date, a year per request."""
    cycles: list[BillingCycle] = []
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
        cycles.extend(billing_cycles(readings, tz))
        chunk_end = chunk_start - timedelta(days=1)
    return cycles


def billing_cycles(readings: list[UsageReading], tz: tzinfo) -> list[BillingCycle]:
    """Billing cycles from billing-cycle usage readings, skipping unusable ones."""
    cycles: dict[datetime, BillingCycle] = {}
    for reading in readings:
        start = _parse_hour(reading.reading_from, tz)
        end = _parse_hour(reading.reading_to, tz)
        if start is None or end is None or start >= end or reading.usage is None:
            continue
        cycles[start] = BillingCycle(start, end, reading.usage, reading.cost)
    return sorted(cycles.values(), key=lambda cycle: cycle.start)


def _local_date(timestamp: float, tz: tzinfo) -> date:
    return datetime.fromtimestamp(timestamp, tz).date()


async def _async_hours(hass: HomeAssistant, stat_id: str) -> _Hours:
    """Every stored (UTC hour, value) of a statistic, oldest first."""

    def _get() -> list[Any]:
        return statistics_during_period(
            hass, datetime(2000, 1, 1, tzinfo=UTC), None, {stat_id}, "hour", None, {"state"}
        ).get(stat_id, [])

    rows = await get_instance(hass).async_add_executor_job(_get)
    return [
        (datetime.fromtimestamp(row["start"], UTC), row["state"])
        for row in rows
        if row.get("state") is not None
    ]


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
    points: _Hours,
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
    max_days: int = DEFAULT_MAX_HISTORY_DAYS,
) -> list[tuple[datetime, float]]:
    """Fetch every hourly reading the API has, walking back in chunks.

    Stops after ``STATISTICS_EMPTY_CHUNKS`` chunks in a row with no data (so a
    short gap, such as a meter swap, does not end the history), or at
    ``max_days`` back (the history limit option). Returns a sorted list of (UTC
    hour, native value).
    """
    now = dt_util.now(tz)
    oldest = (now - timedelta(days=max_days)).date()
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
