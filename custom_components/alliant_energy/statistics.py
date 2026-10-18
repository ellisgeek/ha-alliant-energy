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
from collections.abc import Callable, Mapping
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
from homeassistant.core import HomeAssistant
from homeassistant.helpers.recorder import get_instance
from homeassistant.helpers.storage import Store
from homeassistant.util.unit_conversion import EnergyConverter

from .api import AlliantEnergyAuthError, AlliantEnergyError, Meter
from .const import (
    DEFAULT_MAX_HISTORY_DAYS,
    DOMAIN,
    STATISTICS_CHUNK_DAYS,
    STATISTICS_EMPTY_CHUNKS,
    STATISTICS_LAG_DAYS,
    USAGE_UNIT,
)
from .rates import BillingCycle, CostRate, KnownRates, cost_identity, known_cost_rate
from .usage import MeterUsage

_LOGGER = logging.getLogger(__name__)

# How far back to look for billing cycles when deriving effective rates on a
# normal update; a full-history import looks back to its oldest hour.
_RATE_LOOKBACK_DAYS = 400

# Records which meters have their full history imported, with which history
# limit, and which cost rate each cost statistic was priced under:
# {"full_history": {<usage id>: <max history days>}, "cost_rates": {<cost id>:
# <cost rate identity>}}. The first version stored a list of usage ids; those
# were built with the default limit. A cost statistic without an identity was
# priced under the rule before ADR 0012 and is repriced once.
_STORE_KEY = f"{DOMAIN}.statistics"
_STORE_LOCK = f"{DOMAIN}_statistics_store_lock"  # hass.data key
_STORE_VERSION = 1
# Recorded in place of an identity while no cost rate is known.
_NO_RATE = "none"

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
    usage: MeterUsage,
    account_number: str,
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
        await _async_update_meter(hass, usage, account_number, meter, tz, known, max_history_days)


async def _async_update_meter(
    hass: HomeAssistant,
    usage: MeterUsage,
    account_number: str,
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
        await _async_import_full_history(hass, usage, account_number, meter, tz, known, max_days)
        return

    cost_last = await _async_last(hass, cost_id)
    priced_with = imported.cost_rates.get(cost_id)
    reprice = cost_last is None or priced_with != cost_identity(known.static)
    # Cost is appended from the older of the two last imported hours.
    since = cons_last[0] if reprice or cost_last is None else min(cons_last[0], cost_last[0])
    rate = await _async_cost_rate(usage, meter, tz, known, _local_date(since, tz))

    if rate is None:
        # No cost to add or reprice: import usage only, from the last usage hour.
        native = await _async_fetch_hourly(usage, meter, tz, cons_last[0])
        _import_usage(hass, meter, cons_id, native, cons_last)
        if cost_last is not None and priced_with != _NO_RATE:
            _LOGGER.warning(
                "No cost rate for %s; keeping its cost history and adding no new hours", cost_id
            )
            # Reprices once a rate is back.
            await _async_record(hass, lambda saved: saved.cost_rates.update({cost_id: _NO_RATE}))
        return

    native = await _async_fetch_hourly(usage, meter, tz, since)
    if not reprice:
        _import_usage(hass, meter, cons_id, native, cons_last)
        _import_cost(hass, meter, cost_id, native, rate, cost_last)
        return

    # Reprice the whole cost statistic: the usage already stored plus the new
    # hours, without fetching the history again. Read the stored usage before
    # adding the new hours to it.
    by_hour = dict(await _async_hours(hass, cons_id))
    _import_usage(hass, meter, cons_id, native, cons_last)
    by_hour.update((hour, value) for hour, value in native if hour.timestamp() > cons_last[0])
    hours = sorted(by_hour.items())
    if not hours:
        return
    oldest = hours[0][0].astimezone(tz).date()
    if oldest < _local_date(since, tz):
        rate = await _async_cost_rate(usage, meter, tz, known, oldest) or rate
    _LOGGER.info("Repricing the cost history of %s", cost_id)
    get_instance(hass).async_clear_statistics([cost_id])
    _import_cost(hass, meter, cost_id, hours, rate, None)
    identity = rate.identity
    await _async_record(hass, lambda saved: saved.cost_rates.update({cost_id: identity}))


async def _async_import_full_history(
    hass: HomeAssistant,
    usage: MeterUsage,
    account_number: str,
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
    native = await _async_fetch_history(usage, meter, tz, max_days)
    if not native:
        return
    oldest = native[0][0].astimezone(tz).date()
    rate = await _async_cost_rate(usage, meter, tz, known, oldest)

    # The recorder runs these in order: clear first, then the new imports.
    get_instance(hass).async_clear_statistics([cons_id, cost_id])
    _import_usage(hass, meter, cons_id, native, None)
    if rate is not None:
        _import_cost(hass, meter, cost_id, native, rate, None)

    def _full_history(saved: _Imported) -> None:
        saved.full_history[cons_id] = max_days
        saved.cost_rates.pop(cost_id, None)
        if rate is not None:
            saved.cost_rates[cost_id] = rate.identity

    await _async_record(hass, _full_history)
    _LOGGER.info("Imported %d hours for %s, back to %s", len(native), cons_id, oldest.isoformat())


def _import_usage(
    hass: HomeAssistant,
    meter: Meter,
    cons_id: str,
    native: _Hours,
    last: _Last | None,
) -> None:
    """Import native hourly usage (electric kWh, gas therms) as stored."""
    unit = USAGE_UNIT[meter.service_type]
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


async def _async_record(hass: HomeAssistant, change: Callable[[_Imported], None]) -> None:
    """Change the store: read it fresh, apply ``change``, and save, under one lock.

    Imports of several config entries share the store, so a copy read earlier
    must never be saved over another entry's newer records (issue #12).
    """
    if (lock := hass.data.get(_STORE_LOCK)) is None:
        lock = hass.data[_STORE_LOCK] = asyncio.Lock()
    async with lock:
        imported = await _async_load_imported(hass)
        change(imported)
        await _async_save_imported(hass, imported)


async def _async_save_imported(hass: HomeAssistant, imported: _Imported) -> None:
    await _store(hass).async_save(
        {
            "full_history": dict(sorted(imported.full_history.items())),
            "cost_rates": dict(sorted(imported.cost_rates.items())),
        }
    )


async def _async_cost_rate(
    usage: MeterUsage,
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
        today = dt_util.now(tz).date()
        lookback = today - timedelta(days=_RATE_LOOKBACK_DAYS)
        cycles = await usage.billing_cycles(meter, min(since, lookback), today)
    return known_cost_rate(known, cycles, tz)


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
    usage: MeterUsage,
    meter: Meter,
    tz: tzinfo,
    since: float,
) -> _Hours:
    """Fetch hourly usage forward from the last imported hour.

    ``since`` is the last imported hour (UTC timestamp); fetching restarts at
    its local date so a partly reported day is completed.
    """
    start_date = _local_date(since, tz)
    end_date = (dt_util.now(tz) - timedelta(days=STATISTICS_LAG_DAYS)).date()
    if start_date > end_date:
        return []
    return await usage.hourly(meter, start_date, end_date)


async def _async_fetch_history(
    usage: MeterUsage,
    meter: Meter,
    tz: tzinfo,
    max_days: int = DEFAULT_MAX_HISTORY_DAYS,
) -> _Hours:
    """Fetch every hourly reading the API has, walking back in chunks.

    Stops after ``STATISTICS_EMPTY_CHUNKS`` chunks in a row with no data (so a
    short gap, such as a meter swap, does not end the history), or at
    ``max_days`` back (the history limit option).
    """
    now = dt_util.now(tz)
    oldest = (now - timedelta(days=max_days)).date()
    chunk_end = (now - timedelta(days=STATISTICS_LAG_DAYS)).date()

    by_hour: dict[datetime, float] = {}
    empty = 0
    while chunk_end >= oldest and empty < STATISTICS_EMPTY_CHUNKS:
        chunk_start = max(chunk_end - timedelta(days=STATISTICS_CHUNK_DAYS - 1), oldest)
        try:
            hours = await usage.hourly(meter, chunk_start, chunk_end)
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
        by_hour.update(hours)
        empty = 0 if hours else empty + 1
        chunk_end = chunk_start - timedelta(days=1)
    return sorted(by_hour.items())
