"""Read an account's latest bill: its summary and, when readable, its itemized PDF.

The PDF is downloaded and parsed only when a new invoice appears, about once a
month. The parsed result is saved per config entry, so restarts and reloads
reuse it. Reading the PDF is best-effort (ADR 0005): when it fails, or the
utility has no parser for its bill layout (ADR 0014), the bill still has its
summary.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import homeassistant.util.dt as dt_util
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import AlliantEnergyApi, AlliantEnergyAuthError, BillSummary, PastBill
from .billparser import (
    PARSER_VERSION,
    ItemizedBill,
    ServiceBill,
    bill_from_dict,
    bill_to_dict,
)
from .const import DOMAIN
from .rates import MarginalRate

_LOGGER = logging.getLogger(__name__)

# bill/History requests span at most this many days each.
_HISTORY_CHUNK_DAYS = 365


@dataclass(frozen=True)
class Bill:
    """An account's bill: the summary, and the itemized bill when its PDF was read."""

    summary: BillSummary
    itemized: ItemizedBill | None = None

    def section(self, service: str) -> ServiceBill | None:
        """One service's section of the itemized bill, if there is one."""
        return self.itemized.section(service) if self.itemized else None

    def adjustment(self, service: str) -> float | None:
        """One service's adjustment rate from the itemized bill, if there is one."""
        return self.itemized.adjustment(service) if self.itemized else None


class LatestBill:
    """The latest bill of one account, for one config entry."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: AlliantEnergyApi,
        account_number: str,
        entry_id: str,
        parser: Callable[[bytes], ItemizedBill] | None,
    ) -> None:
        self._hass = hass
        self._api = api
        self._parser = parser
        self._account = account_number
        self._store = _store(hass, entry_id)
        self._loaded = False
        self._itemized: ItemizedBill | None = None
        self._invoice: str | None = None
        # One read at a time, so overlapping callers download a new PDF once.
        self._lock = asyncio.Lock()

    async def latest(self) -> Bill:
        """Fetch the bill summary, and read the PDF if the invoice is new.

        Raises the API's errors for the summary. An auth error while reading
        the PDF is raised too; any other PDF failure keeps the last itemized
        bill read, and the next call tries the new PDF again. Without a parser,
        the PDF is never downloaded.
        """
        if self._parser is None:
            return Bill(await self._api.async_get_current_bill(self._account))
        async with self._lock:
            if not self._loaded:
                await self._async_load_saved()
            summary = await self._api.async_get_current_bill(self._account)
            invoice = summary.invoice_id
            if invoice and invoice != self._invoice:
                await self._async_read_pdf(invoice)
            return Bill(summary, self._itemized)

    def itemized_for(self, invoice: str) -> ItemizedBill | None:
        """The itemized bill already read for an invoice, if it is the latest."""
        return self._itemized if invoice == self._invoice else None

    async def _async_read_pdf(self, invoice: str) -> None:
        if self._parser is None:
            return
        try:
            pdf = await self._api.async_get_bill_pdf(self._account, invoice)
            if not pdf:
                return
            self._itemized = await self._hass.async_add_executor_job(self._parser, pdf)
            self._invoice = invoice
            await self._store.async_save(
                {
                    "parser_version": PARSER_VERSION,
                    "invoice_id": invoice,
                    "bill": bill_to_dict(self._itemized),
                }
            )
        except AlliantEnergyAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - best-effort bill parsing
            _LOGGER.warning("Could not read the bill PDF: %s", err)

    async def _async_load_saved(self) -> None:
        """Reuse the bill saved by an earlier run, if it was parsed the same way."""
        self._loaded = True
        try:
            saved = await self._store.async_load()
            if not saved or saved.get("parser_version") != PARSER_VERSION:
                return
            self._itemized = bill_from_dict(saved["bill"])
            self._invoice = saved["invoice_id"]
        except Exception as err:  # noqa: BLE001 - a bad cache just means a new download
            _LOGGER.debug("Ignoring the saved bill: %s", err)


class PastBills:
    """An account's past bills, read for the marginal rate of each billing cycle.

    Past bills never change, so each PDF is downloaded and parsed once and its
    itemized bill saved for good (ADR 0014). Reading a PDF is best-effort: a
    bill that fails is left out, and tried again on the next read.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        api: AlliantEnergyApi,
        account_number: str,
        entry_id: str,
        parser: Callable[[bytes], ItemizedBill] | None,
        latest: LatestBill | None = None,
    ) -> None:
        self._hass = hass
        self._api = api
        self._parser = parser
        self._account = account_number
        self._store = _past_store(hass, entry_id)
        self._lock = asyncio.Lock()
        # The latest bill is usually among the past bills; reuse what it read.
        self._latest = latest
        # Today's listing (day, since, bills), and invoices whose PDF failed today.
        self._listing: tuple[date, date, list[PastBill]] | None = None
        self._failed: dict[str, date] = {}

    async def marginal_rates(self, service: str, since: date) -> list[MarginalRate]:
        """The service's marginal rate from each bill issued since a date.

        None without a parser for the utility's bill layout: past bills are
        then not even listed.
        """
        if self._parser is None:
            return []
        rates = []
        for itemized in await self._async_itemized_since(since):
            section = itemized.section(service)
            if section is not None and section.marginal_rate is not None:
                rates.append(MarginalRate(section.marginal_rate, section.period_start))
        return rates

    async def _async_itemized_since(self, since: date) -> list[ItemizedBill]:
        async with self._lock:
            saved = await self._async_load()
            today = dt_util.now().date()
            itemized: list[ItemizedBill] = []
            changed = False
            try:
                for bill in await self._async_list(since, today):
                    if bill.invoice_id not in saved:
                        if self._failed.get(bill.invoice_id) == today:
                            continue  # failed earlier today; try again another day
                        read = (
                            self._latest.itemized_for(bill.invoice_id) if self._latest else None
                        ) or await self._async_read(bill.invoice_id)
                        if read is None:
                            self._failed[bill.invoice_id] = today
                            continue
                        saved[bill.invoice_id] = read
                        changed = True
                    itemized.append(saved[bill.invoice_id])
            finally:
                # Keep what was read even when an auth error stops the loop.
                if changed:
                    await self._store.async_save(
                        {
                            "parser_version": PARSER_VERSION,
                            "bills": {k: bill_to_dict(v) for k, v in saved.items()},
                        }
                    )
            return itemized

    async def _async_list(self, since: date, today: date) -> list[PastBill]:
        """Bills issued since a date, a year per request (best-effort).

        A listing made today that reaches back far enough is reused, so the
        services of one import share it.
        """
        if self._listing is not None:
            day, start, bills = self._listing
            if day == today and start <= since:
                return [b for b in bills if b.bill_date is None or b.bill_date >= since]
        bills: list[PastBill] = []
        chunk_start = since
        try:
            while chunk_start <= today:
                chunk_end = min(chunk_start + timedelta(days=_HISTORY_CHUNK_DAYS - 1), today)
                bills.extend(
                    await self._api.async_get_bill_history(self._account, chunk_start, chunk_end)
                )
                chunk_start = chunk_end + timedelta(days=1)
        except AlliantEnergyAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - best-effort: cost uses effective rates
            _LOGGER.warning("Could not list the past bills: %s", err)
            return []
        self._listing = (today, since, bills)
        return bills

    async def _async_read(self, invoice: str) -> ItemizedBill | None:
        if (parser := self._parser) is None:
            return None
        try:
            pdf = await self._api.async_get_bill_pdf(self._account, invoice)
            if not pdf:
                return None
            return await self._hass.async_add_executor_job(parser, pdf)
        except AlliantEnergyAuthError:
            raise
        except Exception as err:  # noqa: BLE001 - best-effort bill parsing
            _LOGGER.warning("Could not read the bill PDF of a past bill: %s", err)
            return None

    async def _async_load(self) -> dict[str, ItemizedBill]:
        """Saved past bills, if they were parsed the same way."""
        try:
            saved = await self._store.async_load()
            if not saved or saved.get("parser_version") != PARSER_VERSION:
                return {}
            return {invoice: bill_from_dict(bill) for invoice, bill in saved["bills"].items()}
        except Exception as err:  # noqa: BLE001 - a bad cache just means new downloads
            _LOGGER.debug("Ignoring the saved past bills: %s", err)
            return {}


async def async_forget_bill(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the bills saved for a config entry (when the entry is removed)."""
    await _store(hass, entry_id).async_remove()
    await _past_store(hass, entry_id).async_remove()


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.bill.{entry_id}")


def _past_store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.bills.{entry_id}")
