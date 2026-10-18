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

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

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
from .billparser import FuelRate, RateCard, parse_rate_card
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ELECTRICITY_RATE,
    CONF_GAS_RATE,
    CONF_PASSWORD,
    CONF_PREMISE_ID,
    CONF_USERNAME,
    CURRENCY_USD,
)
from .models import (
    RATE_SOURCE_BILL,
    RATE_SOURCE_BILLING_CYCLE,
    RATE_SOURCE_OPTIONS,
    EnergyData,
    RateInfo,
)

_LOGGER = logging.getLogger(__name__)

_LOOKBACK_DAYS = 30
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
        # Rate card parsed from the bill PDF, cached per invoice.
        self._rate_card: RateCard | None = None
        self._rate_invoice: str | None = None

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
            rates = await self._async_resolve_rates()
            await stats.async_update_statistics(
                self.hass,
                self._api,
                self._account_id,
                premise,
                meters,
                electricity_rate=rates[SERVICE_ELECTRIC].rate,
                gas_rate=rates[SERVICE_GAS].rate,
            )
        except AlliantEnergyAuthError:
            self._logged_in = False
            raise

    async def _async_resolve_rates(
        self, current_bill: dict[str, Any] | None = None
    ) -> dict[str, RateInfo]:
        """Resolve the cost rate and rate plan per service.

        Rate precedence: a static rate from options, else the marginal rate
        parsed from the latest bill PDF, else None (the statistics importer then
        falls back to an effective rate per billing cycle). The rate plan comes
        from the bill PDF.
        """
        card = await self._async_ensure_rate_card(current_bill)
        rates: dict[str, RateInfo] = {}
        for service, option_key in (
            (SERVICE_ELECTRIC, CONF_ELECTRICITY_RATE),
            (SERVICE_GAS, CONF_GAS_RATE),
        ):
            fuel: FuelRate | None = None
            if card is not None:
                fuel = card.electric if service == SERVICE_ELECTRIC else card.gas
            rate, source = None, None
            if (option := _option_float(self.entry.options.get(option_key))) is not None:
                rate, source = option, RATE_SOURCE_OPTIONS
            elif fuel is not None:
                rate, source = fuel.marginal_rate, RATE_SOURCE_BILL
            rates[service] = RateInfo(
                rate=rate,
                source=source,
                rate_plan=fuel.rate_plan if fuel else None,
                rate_plan_name=fuel.rate_plan_name if fuel else None,
                customer_charge_per_day=fuel.customer_charge_per_day if fuel else None,
            )
        return rates

    async def _async_ensure_rate_card(
        self, current_bill: dict[str, Any] | None = None
    ) -> RateCard | None:
        """Fetch and parse the latest bill PDF, cached per invoice (best-effort)."""
        try:
            bill = current_bill or await self._api.async_get_current_bill(self._account_id)
            invoice = bill.get("invoiceId")
            if not invoice:
                return self._rate_card
            if invoice == self._rate_invoice:
                return self._rate_card
            pdf = await self._api.async_get_bill_pdf(self._account_id, invoice)
            if not pdf:
                return self._rate_card
            card = await self.hass.async_add_executor_job(parse_rate_card, pdf)
            self._rate_card = card
            self._rate_invoice = invoice
        except AlliantEnergyAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - best-effort rate parsing
            _LOGGER.warning("Could not read rates from the bill PDF: %s", err)
        return self._rate_card

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
            if (bill := _latest(monthly, _has_cost)) is not None:
                cost[meter.service_type] = bill

        current_bill = await self._api.async_get_current_bill(self._account_id)
        rates = await self._async_resolve_rates(current_bill)
        # Without a fixed or bill rate, show the latest billing cycle's
        # effective rate, as the cost statistics use for recent hours.
        for service, reading in cost.items():
            info = rates[service]
            if info.rate is None and reading.cost is not None and reading.usage:
                rates[service] = replace(
                    info, rate=reading.cost / reading.usage, source=RATE_SOURCE_BILLING_CYCLE
                )

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
            rates=rates,
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
