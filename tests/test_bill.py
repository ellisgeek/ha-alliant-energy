"""Tests for reading the latest bill: its summary and, when readable, its itemized PDF."""

import asyncio
from datetime import date
from pathlib import Path
from typing import cast

import pytest
from homeassistant.core import HomeAssistant

from custom_components.alliant_energy.api import (
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    BillSummary,
    PastBill,
)
from custom_components.alliant_energy.bill import LatestBill, PastBills, async_forget_bill
from custom_components.alliant_energy.billparser import PARSER_VERSION, bill_to_dict, parse_bill
from custom_components.alliant_energy.rates import MarginalRate

PDF = (Path(__file__).parent / "fixtures" / "bill_2026_10.pdf").read_bytes()
SUMMARY = BillSummary(amount_due=97.12, due_date=date(2026, 11, 3), invoice_id="INV1")


class FakeApi:
    """Serves a bill summary and its PDF, counting PDF downloads."""

    def __init__(self, summary: BillSummary = SUMMARY, pdf: bytes | Exception | None = PDF):
        self.summary = summary
        self.pdf = pdf
        self.downloads: list[str] = []
        self.history: list[PastBill] = []
        self.history_requests: list[tuple[date, date]] = []

    async def async_get_bill_history(
        self, account_number: str, start: date, end: date
    ) -> list[PastBill]:
        self.history_requests.append((start, end))
        return self.history

    async def async_get_current_bill(self, account_number: str) -> BillSummary:
        return self.summary

    async def async_get_bill_pdf(self, account_number: str, invoice_id: str) -> bytes | None:
        self.downloads.append(invoice_id)
        if isinstance(self.pdf, Exception):
            raise self.pdf
        return self.pdf


def _latest(hass: HomeAssistant, api: FakeApi) -> LatestBill:
    return LatestBill(hass, cast(AlliantEnergyApi, api), "acct", "entry1")


async def test_latest_bill_has_the_summary_and_the_itemized_pdf(hass: HomeAssistant) -> None:
    bill = await _latest(hass, FakeApi()).latest()

    assert bill.summary == SUMMARY
    assert bill.itemized == parse_bill(PDF)
    assert bill.section(SERVICE_ELECTRIC) is not None


async def test_pdf_is_downloaded_once_per_invoice_even_after_a_restart(
    hass: HomeAssistant,
) -> None:
    api = FakeApi()
    await _latest(hass, api).latest()
    await _latest(hass, api).latest()  # a restart: a new reader, the same saved bill
    assert api.downloads == ["INV1"]

    api.summary = BillSummary(invoice_id="INV2")
    bill = await _latest(hass, api).latest()
    assert api.downloads == ["INV1", "INV2"]
    assert bill.summary.invoice_id == "INV2"


async def test_bill_saved_by_another_parser_version_is_parsed_again(
    hass: HomeAssistant, hass_storage
) -> None:
    hass_storage["alliant_energy.bill.entry1"] = {
        "version": 1,
        "key": "alliant_energy.bill.entry1",
        "data": {"parser_version": 0, "invoice_id": "INV1", "bill": {}},
    }
    api = FakeApi()

    bill = await _latest(hass, api).latest()

    assert api.downloads == ["INV1"]
    assert bill.itemized == parse_bill(PDF)


async def test_unreadable_pdf_still_gives_the_summary(hass: HomeAssistant) -> None:
    """PDF reading is best-effort (ADR 0005)."""
    bill = await _latest(hass, FakeApi(pdf=AlliantEnergyError("HTTP 500"))).latest()

    assert bill.summary == SUMMARY
    assert bill.itemized is None
    assert bill.section(SERVICE_ELECTRIC) is None


async def test_bill_without_an_invoice_downloads_nothing(hass: HomeAssistant) -> None:
    api = FakeApi(summary=BillSummary(amount_due=1.0))

    bill = await _latest(hass, api).latest()

    assert api.downloads == []
    assert bill.itemized is None


async def test_auth_error_while_downloading_is_raised(hass: HomeAssistant) -> None:
    with pytest.raises(AlliantEnergyAuthError):
        await _latest(hass, FakeApi(pdf=AlliantEnergyAuthError("expired"))).latest()


async def test_forgetting_the_bill_removes_the_saved_copy(
    hass: HomeAssistant, hass_storage
) -> None:
    await _latest(hass, FakeApi()).latest()
    assert "alliant_energy.bill.entry1" in hass_storage

    await async_forget_bill(hass, "entry1")

    assert "alliant_energy.bill.entry1" not in hass_storage


async def test_unreadable_pdf_of_a_new_invoice_keeps_the_last_itemized_bill(
    hass: HomeAssistant,
) -> None:
    """The last itemized bill stays until the new PDF is read; the next call retries it."""
    api = FakeApi()
    latest = _latest(hass, api)
    await latest.latest()

    api.summary = BillSummary(invoice_id="INV2")
    api.pdf = AlliantEnergyError("HTTP 500")
    bill = await latest.latest()
    assert bill.summary.invoice_id == "INV2"
    assert bill.itemized == parse_bill(PDF)

    api.pdf = PDF
    await latest.latest()
    assert api.downloads == ["INV1", "INV2", "INV2"]


async def test_empty_pdf_reads_nothing(hass: HomeAssistant, hass_storage) -> None:
    bill = await _latest(hass, FakeApi(pdf=None)).latest()

    assert bill.itemized is None
    assert "alliant_energy.bill.entry1" not in hass_storage


async def test_saved_bill_format(hass: HomeAssistant, hass_storage) -> None:
    """Earlier versions read this format; keep it stable."""
    await _latest(hass, FakeApi()).latest()

    assert hass_storage["alliant_energy.bill.entry1"]["data"] == {
        "parser_version": PARSER_VERSION,
        "invoice_id": "INV1",
        "bill": bill_to_dict(parse_bill(PDF)),
    }


async def test_overlapping_reads_download_a_new_pdf_once(hass: HomeAssistant) -> None:
    """The sensor refresh and the statistics import can ask at the same time."""
    api = FakeApi()
    latest = _latest(hass, api)

    await asyncio.gather(latest.latest(), latest.latest())

    assert api.downloads == ["INV1"]


def _past(hass: HomeAssistant, api: FakeApi) -> PastBills:
    return PastBills(hass, cast(AlliantEnergyApi, api), "acct", "entry1")


async def test_past_bills_give_each_billing_cycle_its_marginal_rate(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2027-03-01T12:00:00-06:00")
    api = FakeApi()
    api.history = [PastBill("INV1", date(2027, 2, 7))]

    electric = await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1))

    # The fixture bill: electric 0.15235 $/kWh for the cycle from 2027-01-06.
    assert electric == [MarginalRate(0.15235, date(2027, 1, 6))]
    assert api.history_requests == [(date(2026, 12, 1), date(2027, 3, 1))]


async def test_each_past_pdf_is_read_once_even_after_a_restart(hass: HomeAssistant) -> None:
    """Past bills never change, so their PDFs are saved for good."""
    api = FakeApi()
    api.history = [PastBill("INV1", date(2027, 2, 7))]
    await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1))
    await _past(hass, api).marginal_rates(SERVICE_GAS, date(2026, 12, 1))

    assert api.downloads == ["INV1"]


async def test_unreadable_past_pdf_is_skipped_and_tried_again_later(
    hass: HomeAssistant,
) -> None:
    api = FakeApi(pdf=AlliantEnergyError("HTTP 500"))
    api.history = [PastBill("INV1", date(2027, 2, 7))]
    past = _past(hass, api)

    assert await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1)) == []
    api.pdf = PDF
    assert await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1)) != []
    assert api.downloads == ["INV1", "INV1"]


async def test_auth_error_while_reading_past_bills_is_raised(hass: HomeAssistant) -> None:
    api = FakeApi(pdf=AlliantEnergyAuthError("expired"))
    api.history = [PastBill("INV1", date(2027, 2, 7))]

    with pytest.raises(AlliantEnergyAuthError):
        await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1))


async def test_forgetting_the_bill_removes_saved_past_bills_too(
    hass: HomeAssistant, hass_storage
) -> None:
    api = FakeApi()
    api.history = [PastBill("INV1", date(2027, 2, 7))]
    await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1))
    assert "alliant_energy.bills.entry1" in hass_storage

    await async_forget_bill(hass, "entry1")

    assert "alliant_energy.bills.entry1" not in hass_storage


async def test_past_bills_reuse_the_latest_bill_already_read(hass: HomeAssistant) -> None:
    """The bill history includes the latest invoice; its PDF is not read twice."""
    api = FakeApi()
    latest = _latest(hass, api)
    await latest.latest()  # reads INV1
    api.history = [PastBill("INV1", date(2027, 2, 7))]

    past = PastBills(hass, cast(AlliantEnergyApi, api), "acct", "entry1", latest=latest)
    rates = await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 12, 1))

    assert rates == [MarginalRate(0.15235, date(2027, 1, 6))]
    assert api.downloads == ["INV1"]
