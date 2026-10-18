"""Tests for the bill PDF parser, on the text layer and end to end on a PDF."""

from datetime import date
from pathlib import Path

import pytest

from custom_components.alliant_energy.billparser import parse_bill, parse_bill_text

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
