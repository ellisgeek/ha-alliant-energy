"""Tests for the bill PDF parser, on the text layer and end to end on a PDF."""

from datetime import date
from pathlib import Path

import pytest

from custom_components.alliant_energy.api import SERVICE_ELECTRIC, SERVICE_GAS
from custom_components.alliant_energy.billparser import ItemizedBill, parse_bill, parse_bill_text

FIXTURES = Path(__file__).parent / "fixtures"
# Text pdfplumber extracted from a real bill. Personal details are replaced, and
# usage, meter readings, rates, and dates are fake (dates moved +117 days, so
# the bill is from February 2027; dollar amounts recomputed to match). It keeps
# the real layout quirks: two electric sections (a 10-day one when service
# started, crossing the new year, then the main 31-day one) and side-column text
# interleaved with the charges.
REAL_BILL = (FIXTURES / "bill_2026_10.txt").read_text(encoding="utf-8")


def test_pdf_end_to_end_matches_the_text_layer() -> None:
    """The cleaned real PDF parses, through pdfplumber, to the same bill.

    bill_2026_10.pdf is the same bill with personal text redacted and replaced,
    all images and insert pages removed, and metadata cleared. pdfplumber orders
    some blocks differently than in the text fixture, so this also covers a
    second real layout.
    """
    bill = parse_bill((FIXTURES / "bill_2026_10.pdf").read_bytes())
    assert bill == parse_bill_text(REAL_BILL)
    assert bill.electric is not None and bill.electric.meter_reading == 41736
    assert bill.gas is not None and bill.gas.heat_factor == pytest.approx(1.04)


def test_electric_uses_the_latest_section() -> None:
    electric = parse_bill_text(REAL_BILL).electric
    assert electric is not None
    assert electric.period_start == date(2027, 1, 6)
    assert electric.period_end == date(2027, 2, 6)
    assert electric.billed_days == 31
    assert electric.meter_number == "300000003"
    assert electric.meter_reading == 41736
    assert electric.previous_meter_reading == 41324
    assert electric.meter_unit == "kWh"
    assert electric.heat_factor is None


def test_electric_rates_use_the_newest_prorated_adjustment() -> None:
    electric = parse_bill_text(REAL_BILL).electric
    assert electric is not None
    assert electric.components == {"energy_charge": 0.15847, "fuel_adjustment": -0.00612}
    assert electric.marginal_rate == pytest.approx(0.15847 - 0.00612)
    assert electric.customer_charge_per_day == pytest.approx(0.497)
    assert electric.rate_plan == "RG1"
    assert electric.rate_plan_name == "Residential Electric Service"


def test_gas_section() -> None:
    gas = parse_bill_text(REAL_BILL).gas
    assert gas is not None
    assert gas.marginal_rate == pytest.approx(0.4123 + 0.5582 - 0.218465)
    assert gas.components["supply_market_adj"] == pytest.approx(-0.218465)
    assert gas.customer_charge_per_day == pytest.approx(0.4218)
    assert gas.rate_plan == "GG1"
    assert gas.meter_number == "000000004"
    assert gas.meter_reading == 3165
    assert gas.previous_meter_reading == 3118
    assert gas.meter_unit == "CCF"
    assert gas.heat_factor == pytest.approx(1.04)
    assert gas.period_end == date(2027, 2, 6)


def test_bill_level_fields() -> None:
    bill = parse_bill_text(REAL_BILL)
    assert bill.bill_date == date(2027, 2, 7)
    assert bill.next_read_start == date(2027, 3, 6)
    assert bill.next_read_end == date(2027, 3, 9)
    assert bill.average_temperature == 60
    assert bill.degree_days == 157


def test_period_crossing_new_year_takes_the_year_from_the_bill_date() -> None:
    text = """Bill Date Jan 08, 2027
Rate: RG1 - Residential Electric Service Billed for: 32 Days
Dec 05 - Jan 06 300000003 1,000 900 100 kWh 1 100 kWh
Energy Charge 100.000 kWhX$0.17011 $17.01
"""
    electric = parse_bill_text(text).electric
    assert electric is not None
    assert electric.period_start == date(2026, 12, 5)
    assert electric.period_end == date(2027, 1, 6)


def test_unreadable_text_gives_empty_bill() -> None:
    bill = parse_bill_text("nothing useful here")
    assert bill.electric is None
    assert bill.gas is None
    assert bill.bill_date is None
    assert bill.degree_days is None


def test_section_without_charges_has_no_marginal_rate() -> None:
    text = "Rate: GG1 - Residential Gas Service Billed for: 31 Days\n"
    gas = parse_bill_text(text).gas
    assert gas is not None
    assert gas.marginal_rate is None
    assert gas.rate_plan == "GG1"
    assert gas.period_end is None  # no bill date, no meter line


def test_saved_bill_round_trips() -> None:
    """A bill saved as JSON loads back unchanged."""
    import json

    from custom_components.alliant_energy.billparser import bill_from_dict, bill_to_dict

    bill = parse_bill_text(REAL_BILL)
    saved = json.loads(json.dumps(bill_to_dict(bill)))
    assert bill_from_dict(saved) == bill


def test_section_and_adjustment_by_service() -> None:
    """The adjustment is the fuel adjustment for electric, the supply adjustment for gas."""
    bill = parse_bill_text(REAL_BILL)
    assert bill.electric is not None and bill.gas is not None

    assert bill.section(SERVICE_ELECTRIC) is bill.electric
    assert bill.section(SERVICE_GAS) is bill.gas
    assert bill.adjustment(SERVICE_ELECTRIC) == bill.electric.components["fuel_adjustment"]
    assert bill.adjustment(SERVICE_GAS) == bill.gas.components["supply_market_adj"]
    assert ItemizedBill().section(SERVICE_GAS) is None
    assert ItemizedBill().adjustment(SERVICE_GAS) is None


def test_period_ending_in_a_later_month_than_the_bill_is_from_the_year_before() -> None:
    text = """Bill Date Jan 08, 2027
Rate: RG1 - Residential Electric Service Billed for: 31 Days
Nov 05 - Dec 06 300000003 1,000 900 100 kWh 1 100 kWh
"""
    electric = parse_bill_text(text).electric
    assert electric is not None
    assert electric.period_start == date(2026, 11, 5)
    assert electric.period_end == date(2026, 12, 6)


@pytest.mark.parametrize(
    ("bill_date", "period"),
    [("", "Jan 06 - Feb 06"), ("Bill Date Feb 07, 2027\n", "Jan 06 - Feb 30")],
    ids=["no_bill_date", "impossible_date"],
)
def test_period_is_unknown_without_a_valid_date(bill_date: str, period: str) -> None:
    text = f"""{bill_date}Rate: RG1 - Residential Electric Service Billed for: 31 Days
{period} 300000003 1,000 900 100 kWh 1 100 kWh
"""
    electric = parse_bill_text(text).electric
    assert electric is not None
    assert (electric.period_start, electric.period_end) == (None, None)
    assert electric.meter_number == "300000003"  # the rest of the line still counts


def test_impossible_bill_date_is_unknown() -> None:
    assert parse_bill_text("Bill Date Feb 30, 2027").bill_date is None


def test_section_of_another_service_is_ignored() -> None:
    """A section for a service the integration does not read keeps its charges."""
    text = """Rate: RG1 - Residential Electric Service Billed for: 31 Days
Energy Charge 412.000 kWhX$0.15847 $65.29
Rate: WS1 - Water Service Billed for: 31 Days
Energy Charge 100.000 kWhX$9.99999 $999.99
"""
    bill = parse_bill_text(text)
    assert bill.electric is not None
    assert bill.electric.components == {"energy_charge": 0.15847}
    assert bill.gas is None


def test_an_older_section_after_a_newer_one_does_not_replace_it() -> None:
    text = """Bill Date Feb 07, 2027
Rate: RG1 - Residential Electric Service Billed for: 31 Days
Jan 06 - Feb 06 300000003 41,736 41,324 412 kWh 1 412 kWh
Energy Charge 412.000 kWhX$0.15847 $65.29
Rate: RG1 - Residential Electric Service Billed for: 10 Days
Dec 27 - Jan 06 300000003 41,324 41,206 118 kWh 1 118 kWh
Energy Charge 118.000 kWhX$0.14000 $16.52
"""
    electric = parse_bill_text(text).electric
    assert electric is not None
    assert electric.period_end == date(2027, 2, 6)
    assert electric.marginal_rate == pytest.approx(0.15847)
