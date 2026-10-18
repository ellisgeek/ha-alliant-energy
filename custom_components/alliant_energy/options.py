"""Read the integration options, falling back to defaults."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry

from .const import (
    CONF_MAX_HISTORY_DAYS,
    CONF_UPDATE_INTERVAL_HOURS,
    DEFAULT_MAX_HISTORY_DAYS,
    DEFAULT_UPDATE_INTERVAL_HOURS,
    MAX_UPDATE_INTERVAL_HOURS,
    MIN_HISTORY_DAYS,
    MIN_UPDATE_INTERVAL_HOURS,
)


def max_history_days(entry: ConfigEntry) -> int:
    """How many days of hourly history to import at most."""
    return _int_option(
        entry,
        CONF_MAX_HISTORY_DAYS,
        DEFAULT_MAX_HISTORY_DAYS,
        MIN_HISTORY_DAYS,
        DEFAULT_MAX_HISTORY_DAYS,
    )


def update_interval(entry: ConfigEntry) -> timedelta:
    """How often to refresh the sensors and import new statistics."""
    hours = _int_option(
        entry,
        CONF_UPDATE_INTERVAL_HOURS,
        DEFAULT_UPDATE_INTERVAL_HOURS,
        MIN_UPDATE_INTERVAL_HOURS,
        MAX_UPDATE_INTERVAL_HOURS,
    )
    return timedelta(hours=hours)


def _int_option(entry: ConfigEntry, key: str, default: int, low: int, high: int) -> int:
    """An integer option within bounds; the default if missing or invalid."""
    value = entry.options.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    value = int(value)
    return value if low <= value <= high else default
