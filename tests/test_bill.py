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
from custom_components.alliant_energy.billparser import (
    PARSER_VERSION,
    ItemizedBill,
    ServiceBill,
    bill_to_dict,
    parse_bill,
)
from custom_components.alliant_energy.rates import MarginalRate

PDF = (Path(__file__).parent / "fixtures" / "bill_2026_10.pdf").read_bytes()
SUMMARY = BillSummary(amount_due=97.12, due_date=date(2026, 11, 3), invoice_id="INV1")


class FakeApi:
    """Serves a bill summary and its PDF, counting PDF downloads."""

    def __init__(self, summary: BillSummary = SUMMARY, pdf: object = PDF):
        self.summary = summary
        self.pdf = pdf
        self.downloads: list[str] = []
        self.history: list[PastBill] | Exception = []
        self.history_requests: list[tuple[date, date]] = []

    async def async_get_bill_history(
        self, account_number: str, start: date, end: date
    ) -> list[PastBill]:
        self.history_requests.append((start, end))
        if isinstance(self.history, Exception):
            raise self.history
        return [b for b in self.history if b.bill_date is None or start <= b.bill_date <= end]

    async def async_get_current_bill(self, account_number: str) -> BillSummary:
        return self.summary

    async def async_get_bill_pdf(self, account_number: str, invoice_id: str) -> bytes | None:
        self.downloads.append(invoice_id)
        pdf = self.pdf.get(invoice_id, PDF) if isinstance(self.pdf, dict) else self.pdf
        if isinstance(pdf, Exception):
            raise pdf
        assert pdf is None or isinstance(pdf, bytes)
        return pdf


def _latest(hass: HomeAssistant, api: FakeApi) -> LatestBill:
    return LatestBill(hass, cast(AlliantEnergyApi, api), "acct", "entry1", parse_bill)


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


async def test_corrupt_saved_bill_is_downloaded_again(hass: HomeAssistant, hass_storage) -> None:
    """A saved bill that cannot be read (here, no invoice id) is not fatal."""
    hass_storage["alliant_energy.bill.entry1"] = {
        "version": 1,
        "key": "alliant_energy.bill.entry1",
        "data": {"parser_version": PARSER_VERSION, "bill": bill_to_dict(parse_bill(PDF))},
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
    return PastBills(hass, cast(AlliantEnergyApi, api), "acct", "entry1", parse_bill)


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
    api.history = [PastBill("INV1", date(2026, 10, 13))]
    await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))
    await _past(hass, api).marginal_rates(SERVICE_GAS, date(2026, 9, 1))

    assert api.downloads == ["INV1"]


async def test_unreadable_past_pdf_is_skipped_and_tried_again_another_day(
    hass: HomeAssistant, freezer
) -> None:
    freezer.move_to("2026-10-18T12:00:00-05:00")
    api = FakeApi(pdf=AlliantEnergyError("HTTP 500"))
    api.history = [PastBill("INV1", date(2026, 10, 13))]
    past = _past(hass, api)

    assert await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1)) == []
    api.pdf = PDF
    freezer.move_to("2026-10-19T12:00:00-05:00")
    assert await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1)) != []
    assert api.downloads == ["INV1", "INV1"]


async def test_auth_error_while_reading_past_bills_is_raised(hass: HomeAssistant) -> None:
    api = FakeApi(pdf=AlliantEnergyAuthError("expired"))
    api.history = [PastBill("INV1", date(2026, 10, 13))]

    with pytest.raises(AlliantEnergyAuthError):
        await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))


async def test_forgetting_the_bill_removes_saved_past_bills_too(
    hass: HomeAssistant, hass_storage
) -> None:
    api = FakeApi()
    api.history = [PastBill("INV1", date(2026, 10, 13))]
    await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))
    assert "alliant_energy.bills.entry1" in hass_storage

    await async_forget_bill(hass, "entry1")

    assert "alliant_energy.bills.entry1" not in hass_storage


async def test_past_bills_reuse_the_latest_bill_already_read(hass: HomeAssistant) -> None:
    """The bill history includes the latest invoice; its PDF is not read twice."""
    api = FakeApi()
    latest = _latest(hass, api)
    await latest.latest()  # reads INV1
    api.history = [PastBill("INV1", date(2026, 10, 13))]

    past = PastBills(hass, cast(AlliantEnergyApi, api), "acct", "entry1", parse_bill, latest=latest)
    rates = await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))

    assert rates == [MarginalRate(0.15235, date(2027, 1, 6))]
    assert api.downloads == ["INV1"]


async def test_past_bills_are_listed_a_year_per_request_and_shared_by_services(
    hass: HomeAssistant, freezer
) -> None:
    """Gas and electric of one import share one listing; long ranges are split."""
    freezer.move_to("2027-03-01T12:00:00-06:00")
    api = FakeApi()
    api.history = [PastBill("INV1", date(2027, 2, 7))]
    past = _past(hass, api)

    await past.marginal_rates(SERVICE_ELECTRIC, date(2025, 1, 1))
    await past.marginal_rates(SERVICE_GAS, date(2025, 6, 1))

    assert api.history_requests == [
        (date(2025, 1, 1), date(2025, 12, 31)),
        (date(2026, 1, 1), date(2026, 12, 31)),
        (date(2027, 1, 1), date(2027, 3, 1)),
    ]


async def test_failed_past_pdf_is_not_read_again_for_the_other_service(
    hass: HomeAssistant,
) -> None:
    api = FakeApi(pdf=AlliantEnergyError("HTTP 500"))
    api.history = [PastBill("INV1", date(2026, 10, 13))]
    past = _past(hass, api)

    await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))
    await past.marginal_rates(SERVICE_GAS, date(2026, 9, 1))

    assert api.downloads == ["INV1"]


async def test_bills_read_before_an_auth_error_are_kept(hass: HomeAssistant, hass_storage) -> None:
    api = FakeApi(pdf={"INV1": AlliantEnergyAuthError("expired")})
    api.history = [PastBill("INV2", date(2026, 10, 13)), PastBill("INV1", date(2026, 9, 11))]

    with pytest.raises(AlliantEnergyAuthError):
        await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))

    assert set(hass_storage["alliant_energy.bills.entry1"]["data"]["bills"]) == {"INV2"}


async def test_bill_history_failure_reads_no_past_bills(hass: HomeAssistant) -> None:
    """Past bills are best-effort: cost then uses the effective rates."""
    api = FakeApi()
    api.history = AlliantEnergyError("HTTP 400")

    assert await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1)) == []


async def test_without_a_parser_the_bill_is_the_summary_only(hass: HomeAssistant) -> None:
    """A utility with no parser for its bill layout never downloads the PDF."""
    api = FakeApi()

    bill = await LatestBill(hass, cast(AlliantEnergyApi, api), "acct", "entry1", None).latest()

    assert bill.summary == SUMMARY
    assert bill.itemized is None
    assert api.downloads == []


async def test_without_a_parser_past_bills_are_not_even_listed(hass: HomeAssistant) -> None:
    api = FakeApi()
    api.history = [PastBill("INV1", date(2026, 10, 13))]
    past = PastBills(hass, cast(AlliantEnergyApi, api), "acct", "entry1", None)

    assert await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1)) == []
    assert api.history_requests == []
    assert api.downloads == []


async def test_past_bill_without_a_rate_for_the_service_gives_none(
    hass: HomeAssistant,
) -> None:
    """Like a first bill with no usage charges: nothing to price that cycle with."""

    def _parse(pdf: bytes) -> ItemizedBill:
        return ItemizedBill(gas=ServiceBill(marginal_rate=None, period_start=date(2026, 9, 1)))

    api = FakeApi()
    api.history = [PastBill("INV1", date(2026, 9, 11))]
    past = PastBills(hass, cast(AlliantEnergyApi, api), "acct", "entry1", _parse)

    assert await past.marginal_rates(SERVICE_GAS, date(2026, 9, 1)) == []
    assert await past.marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1)) == []


async def test_auth_error_while_listing_past_bills_is_raised(hass: HomeAssistant) -> None:
    api = FakeApi()
    api.history = AlliantEnergyAuthError("expired")

    with pytest.raises(AlliantEnergyAuthError):
        await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))


async def test_empty_past_pdf_gives_no_rate(hass: HomeAssistant, hass_storage) -> None:
    api = FakeApi(pdf=None)
    api.history = [PastBill("INV1", date(2026, 10, 13))]

    assert await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1)) == []
    assert "alliant_energy.bills.entry1" not in hass_storage


async def test_corrupt_saved_past_bills_are_read_again(hass: HomeAssistant, hass_storage) -> None:
    hass_storage["alliant_energy.bills.entry1"] = {
        "version": 1,
        "key": "alliant_energy.bills.entry1",
        "data": {"parser_version": PARSER_VERSION, "bills": {"INV1": {"electric": 5}}},
    }
    api = FakeApi()
    api.history = [PastBill("INV1", date(2026, 10, 13))]

    rates = await _past(hass, api).marginal_rates(SERVICE_ELECTRIC, date(2026, 9, 1))

    assert rates == [MarginalRate(0.15235, date(2027, 1, 6))]
    assert api.downloads == ["INV1"]
