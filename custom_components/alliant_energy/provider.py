"""Home Assistant-facing client that orchestrates login, usage, and statistics.

Adapts the low-level :mod:`.api` client to what the coordinator needs: it owns
the account/premise identity, imports hourly long-term statistics for the Energy
dashboard, and produces the latest usage and most-recent bill cost for sensors.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import statistics as stats
from .api import (
    PERIODICITY_DAILY,
    PERIODICITY_MONTHLY,
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    Meter,
    UsageReading,
)
from .billparser import (
    PARSER_VERSION,
    Bill,
    ServiceBill,
    bill_from_dict,
    bill_to_dict,
    parse_bill,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ELECTRICITY_RATE,
    CONF_GAS_RATE,
    CONF_PASSWORD,
    CONF_PREMISE_ID,
    CONF_USERNAME,
    CURRENCY_USD,
    DOMAIN,
)
from .models import RATE_SOURCE_BILLING_CYCLE, EnergyData, ServiceData
from .options import max_history_days
from .rates import KnownRates, known_cost_rate

_LOGGER = logging.getLogger(__name__)

# Long enough to cover the whole billing cycle in progress (about a month).
_LOOKBACK_DAYS = 45
_COST_LOOKBACK_DAYS = 120
_DATE_FMT = "%m/%d/%Y"

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
        self._api = AlliantEnergyApi()
        self._meters: list[Meter] | None = None
        self._premise: str | None = entry.data.get(CONF_PREMISE_ID)
        self._logged_in = False
        # The latest bill PDF, parsed and cached per invoice. Also saved to a
        # store so restarts and reloads reuse it: the PDF is downloaded only
        # when a new invoice appears, about once a month.
        self._bill: Bill | None = None
        self._bill_invoice: str | None = None
        self._bill_loaded = False
        self._bill_store: Store[dict[str, Any]] = bill_store(hass, entry.entry_id)

    @property
    def _account_id(self) -> str:
        return str(self.entry.data[CONF_ACCOUNT_ID])

    async def async_close(self) -> None:
        """Release the underlying HTTP session."""
        await self._api.close()

    async def async_fetch_data(self) -> EnergyData:
        """Return the latest usage and most-recent bill cost for sensors."""
        try:
            premise, meters = await self._async_ensure_context()
        except AlliantEnergyAuthError:
            self._logged_in = False
            raise
        return await self._async_build_energy_data(premise, meters)

    async def async_update_statistics(self) -> None:
        """Import hourly long-term statistics for the Energy dashboard.

        Run as a background task (see __init__) so the initial history backfill
        does not block config-entry setup.
        """
        try:
            premise, meters = await self._async_ensure_context()
            bill = await self._async_ensure_bill()
            await stats.async_update_statistics(
                self.hass,
                self._api,
                self._account_id,
                premise,
                meters,
                rates=self._known_rates(bill),
                max_history_days=max_history_days(self.entry),
            )
        except AlliantEnergyAuthError:
            self._logged_in = False
            raise

    def _known_rates(self, bill: Bill | None) -> dict[str, KnownRates]:
        """Each service's static rate (options) and marginal rate (bill PDF)."""
        known: dict[str, KnownRates] = {}
        for service, option_key in (
            (SERVICE_ELECTRIC, CONF_ELECTRICITY_RATE),
            (SERVICE_GAS, CONF_GAS_RATE),
        ):
            section = _section(bill, service)
            known[service] = KnownRates(
                static=_option_float(self.entry.options.get(option_key)),
                marginal=section.marginal_rate if section else None,
                marginal_from=section.period_start if section else None,
            )
        return known

    async def _async_resolve_services(
        self, current_bill: dict[str, Any] | None = None
    ) -> tuple[dict[str, ServiceData], Bill | None]:
        """Resolve each service's bill details from the latest bill PDF."""
        bill = await self._async_ensure_bill(current_bill)
        services: dict[str, ServiceData] = {}
        for service, adjustment_key in (
            (SERVICE_ELECTRIC, "fuel_adjustment"),
            (SERVICE_GAS, "supply_market_adj"),
        ):
            section = _section(bill, service)
            if section is None:
                services[service] = ServiceData()
                continue
            services[service] = ServiceData(
                rate_plan=section.rate_plan,
                rate_plan_name=section.rate_plan_name,
                customer_charge_per_day=section.customer_charge_per_day,
                adjustment=section.components.get(adjustment_key),
                meter_number=section.meter_number,
                meter_reading=section.meter_reading,
                previous_meter_reading=section.previous_meter_reading,
                meter_unit=section.meter_unit,
                heat_factor=section.heat_factor,
            )
        return services, bill

    async def _async_ensure_bill(self, current_bill: dict[str, Any] | None = None) -> Bill | None:
        """Fetch and parse the latest bill PDF, cached per invoice (best-effort)."""
        if not self._bill_loaded:
            await self._async_load_saved_bill()
        try:
            bill = current_bill or await self._api.async_get_current_bill(self._account_id)
            invoice = bill.get("invoiceId")
            if not invoice:
                return self._bill
            if invoice == self._bill_invoice:
                return self._bill
            pdf = await self._api.async_get_bill_pdf(self._account_id, invoice)
            if not pdf:
                return self._bill
            self._bill = await self.hass.async_add_executor_job(parse_bill, pdf)
            self._bill_invoice = invoice
            await self._bill_store.async_save(
                {
                    "parser_version": PARSER_VERSION,
                    "invoice_id": invoice,
                    "bill": bill_to_dict(self._bill),
                }
            )
        except AlliantEnergyAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - best-effort bill parsing
            _LOGGER.warning("Could not read the bill PDF: %s", err)
        return self._bill

    async def _async_load_saved_bill(self) -> None:
        """Reuse the bill saved by an earlier run, if it was parsed the same way."""
        self._bill_loaded = True
        try:
            saved = await self._bill_store.async_load()
            if not saved or saved.get("parser_version") != PARSER_VERSION:
                return
            self._bill = bill_from_dict(saved["bill"])
            self._bill_invoice = saved["invoice_id"]
        except Exception as err:  # noqa: BLE001 - a bad cache just means a new download
            _LOGGER.debug("Ignoring the saved bill: %s", err)

    async def _async_ensure_context(self) -> tuple[str, list[Meter]]:
        if not self._logged_in:
            await self._api.async_login(
                self.entry.data[CONF_USERNAME], self.entry.data[CONF_PASSWORD]
            )
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
        return self._premise, self._meters

    async def _async_build_energy_data(self, premise: str, meters: list[Meter]) -> EnergyData:
        today = datetime.now()
        day_from = (today - timedelta(days=_LOOKBACK_DAYS)).strftime(_DATE_FMT)
        cost_from = (today - timedelta(days=_COST_LOOKBACK_DAYS)).strftime(_DATE_FMT)
        to_date = today.strftime(_DATE_FMT)

        latest: dict[str, UsageReading] = {}
        daily_by_service: dict[str, list[UsageReading]] = {}
        monthly_by_service: dict[str, list[UsageReading]] = {}
        cost: dict[str, UsageReading] = {}
        for meter in meters:
            daily = await self._api.async_get_usage(
                meter.service_type,
                self._account_id,
                premise,
                meter.meter_number,
                day_from,
                to_date,
                periodicity=PERIODICITY_DAILY,
            )
            daily_by_service[meter.service_type] = daily
            if (reading := _latest(daily, _has_usage)) is not None:
                latest[meter.service_type] = reading

            monthly = await self._api.async_get_usage(
                meter.service_type,
                self._account_id,
                premise,
                meter.meter_number,
                cost_from,
                to_date,
                periodicity=PERIODICITY_MONTHLY,
            )
            monthly_by_service[meter.service_type] = monthly
            if (bill := _latest(monthly, _has_cost)) is not None:
                cost[meter.service_type] = bill

        current_bill = await self._api.async_get_current_bill(self._account_id)
        services, bill = await self._async_resolve_services(current_bill)
        # Show the cost rate the cost statistic uses for the newest hour.
        tz = dt_util.get_default_time_zone()
        for service, known in self._known_rates(bill).items():
            cycles = stats.billing_cycles(monthly_by_service.get(service, []), tz)
            if (rate := known_cost_rate(known, cycles, tz)) is not None:
                value, source = rate.current
                services[service] = replace(services[service], rate=value, rate_source=source)
        # Project each service's cost for the billing cycle in progress.
        for service, data in services.items():
            section = _section(bill, service)
            if section is not None and section.period_end is not None:
                services[service] = _project(
                    data,
                    daily_by_service.get(service, []),
                    section.period_end,
                    _expected_next_read(bill, section),
                )
        projected = [d.projected_cost for d in services.values() if d.projected_cost is not None]
        period = _section(bill, SERVICE_ELECTRIC) or _section(bill, SERVICE_GAS)

        electric = latest.get(SERVICE_ELECTRIC)
        gas = latest.get(SERVICE_GAS)
        return EnergyData(
            electricity_kwh=electric.usage if electric else None,
            gas_therms=gas.usage if gas else None,
            electricity_cost=cost[SERVICE_ELECTRIC].cost if SERVICE_ELECTRIC in cost else None,
            gas_cost=cost[SERVICE_GAS].cost if SERVICE_GAS in cost else None,
            currency=CURRENCY_USD,
            reading_dates={
                SERVICE_ELECTRIC: electric.reading_date if electric else None,
                SERVICE_GAS: gas.reading_date if gas else None,
            },
            amount_due=_to_float(current_bill.get("totalAmountDue")),
            current_charges=_to_float(current_bill.get("currentCharges")),
            past_due_amount=_to_float(current_bill.get("pastDueAmount")),
            previous_balance=_to_float(current_bill.get("previousBalance")),
            due_date=_parse_mdy(current_bill.get("netDueDate")),
            invoice_date=_parse_mdy(current_bill.get("invoiceDate")),
            invoice_id=current_bill.get("invoiceId"),
            services=services,
            billing_period_start=period.period_start if period else None,
            billing_period_end=period.period_end if period else None,
            billed_days=period.billed_days if period else None,
            next_meter_read_start=bill.next_read_start if bill else None,
            next_meter_read_end=bill.next_read_end if bill else None,
            average_temperature=bill.average_temperature if bill else None,
            degree_days=bill.degree_days if bill else None,
            projected_bill=round(sum(projected), 2) if projected else None,
        )


def _to_float(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _parse_mdy(value: object) -> date | None:
    """Parse the API's ``MM-DD-YYYY`` date strings into a date."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%m-%d-%Y").date()
    except ValueError:
        return None


def _option_float(value: object) -> float | None:
    """Coerce a stored option to a positive float, or None if unset/invalid."""
    rate = _to_float(value)
    return rate if rate is not None and rate > 0 else None


def _has_usage(reading: UsageReading) -> bool:
    return reading.usage is not None


def _has_cost(reading: UsageReading) -> bool:
    return reading.cost is not None and reading.cost != 0


def _period_key(reading: UsageReading) -> str:
    """Best timestamp for ordering, ignoring the API's bogus 0001-… values."""
    candidates = [reading.reading_to, reading.reading_date, reading.reading_from]
    valid = [c for c in candidates if c and not c.startswith("0001")]
    return max(valid) if valid else ""


def _latest(
    readings: list[UsageReading], predicate: Callable[[UsageReading], bool]
) -> UsageReading | None:
    candidates = [r for r in readings if predicate(r)]
    if not candidates:
        return None
    return max(candidates, key=_period_key)


def _section(bill: Bill | None, service: str) -> ServiceBill | None:
    if bill is None:
        return None
    return bill.electric if service == SERVICE_ELECTRIC else bill.gas


def _expected_next_read(bill: Bill | None, section: ServiceBill) -> date | None:
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
    daily: list[UsageReading],
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
    by_day: dict[date, float] = {}
    for reading in daily:
        if reading.usage is None or not reading.reading_date:
            continue
        try:
            day = date.fromisoformat(reading.reading_date[:10])
        except ValueError:
            continue
        if day >= cycle_start:
            by_day[day] = reading.usage
    if not by_day or cycle_days <= 0:
        return data
    usage = sum(by_day.values())
    days = len(by_day)
    fixed = (
        data.customer_charge_per_day or 0.0
        if data.rate_source != RATE_SOURCE_BILLING_CYCLE
        else 0.0
    )
    return replace(
        data,
        usage_to_date=round(usage, 3),
        days_to_date=days,
        cycle_days=cycle_days,
        cost_to_date=round(usage * data.rate + fixed * days, 2),
        projected_cost=round(usage / days * cycle_days * data.rate + fixed * cycle_days, 2),
    )


def bill_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    """Where an entry's latest parsed bill is saved."""
    return Store(hass, 1, f"{DOMAIN}.bill.{entry_id}")
