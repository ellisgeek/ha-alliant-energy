"""Read an account's latest bill: its summary and, when readable, its itemized PDF.

The PDF is downloaded and parsed only when a new invoice appears, about once a
month. The parsed result is saved per config entry, so restarts and reloads
reuse it. Reading the PDF is best-effort (ADR 0005): when it fails, the bill
still has its summary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .api import AlliantEnergyApi, AlliantEnergyAuthError, BillSummary
from .billparser import (
    PARSER_VERSION,
    ItemizedBill,
    ServiceBill,
    bill_from_dict,
    bill_to_dict,
    parse_bill,
)
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Bill:
    """An account's bill: the summary, and the itemized bill when its PDF was read."""

    summary: BillSummary
    itemized: ItemizedBill | None = None

    def section(self, service: str) -> ServiceBill | None:
        """One service's section of the itemized bill, if there is one."""
        return self.itemized.section(service) if self.itemized else None


class LatestBill:
    """The latest bill of one account, for one config entry."""

    def __init__(
        self, hass: HomeAssistant, api: AlliantEnergyApi, account_number: str, entry_id: str
    ) -> None:
        self._hass = hass
        self._api = api
        self._account = account_number
        self._store = _store(hass, entry_id)
        self._loaded = False
        self._itemized: ItemizedBill | None = None
        self._invoice: str | None = None

    async def latest(self) -> Bill:
        """Fetch the bill summary, and read the PDF if the invoice is new.

        Raises the API's errors for the summary. An auth error while reading
        the PDF is raised too; any other PDF failure keeps the last itemized
        bill read.
        """
        if not self._loaded:
            await self._async_load_saved()
        summary = await self._api.async_get_current_bill(self._account)
        invoice = summary.invoice_id
        if invoice and invoice != self._invoice:
            await self._async_read_pdf(invoice)
        return Bill(summary, self._itemized)

    async def _async_read_pdf(self, invoice: str) -> None:
        try:
            pdf = await self._api.async_get_bill_pdf(self._account, invoice)
            if not pdf:
                return
            self._itemized = await self._hass.async_add_executor_job(parse_bill, pdf)
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


async def async_forget_bill(hass: HomeAssistant, entry_id: str) -> None:
    """Delete the bill saved for a config entry (when the entry is removed)."""
    await _store(hass, entry_id).async_remove()


def _store(hass: HomeAssistant, entry_id: str) -> Store[dict[str, Any]]:
    return Store(hass, 1, f"{DOMAIN}.bill.{entry_id}")
