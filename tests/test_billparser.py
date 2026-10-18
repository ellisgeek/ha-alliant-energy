"""Tests for the bill PDF rate parser (text layer, no PDF dependency)."""

import pytest

from custom_components.alliant_energy.billparser import parse_rate_card_text

# Representative lines as pdfplumber extracts them from a real bill.
SAMPLE = """
Residential Electric Service
Rate: RG1 - Residential Electric Service Billed for: 31 Days
Energy Charge 339.000 kWhX$0.17011 $57.67
2026 Fuel Adjustment 339.000 kWhX$0.00 X 0.6129032 $0.00
2026 Fuel Adjustment 339.000 kWhX$-0.00788 X 0.3870968 -$1.03
Customer Charge 31.000 DaysX$0.526 $16.31
Total Current Charges $75.14
Residential Gas Service
Rate: GG1 - Residential Gas Service Billed for: 31 Days
Distribution Charge 13.00 thermsX$0.3666 $4.77
Gas Supply Base Rate 13.00 thermsX$0.6137 $7.98
Gas Supply Market Adj 13.00 thermsX$-0.471793 -$6.13
Customer Charge 31.000 DaysX$0.4603 $14.27
Total Current Charges $20.89
"""


def test_parses_electric_marginal_rate() -> None:
    card = parse_rate_card_text(SAMPLE)
    assert card.electric is not None
    # Energy charge + (most recent) fuel adjustment.
    assert card.electric.marginal_rate == pytest.approx(0.17011 - 0.00788)
    assert card.electric.customer_charge_per_day == pytest.approx(0.526)
    assert card.electric.components["energy_charge"] == pytest.approx(0.17011)


def test_parses_gas_marginal_rate() -> None:
    card = parse_rate_card_text(SAMPLE)
    assert card.gas is not None
    # Distribution + supply base + market adjustment.
    assert card.gas.marginal_rate == pytest.approx(0.3666 + 0.6137 - 0.471793)
    assert card.gas.customer_charge_per_day == pytest.approx(0.4603)


def test_missing_section_yields_none() -> None:
    card = parse_rate_card_text("nothing useful here")
    assert card.electric is None
    assert card.gas is None


def test_parses_rate_plans() -> None:
    card = parse_rate_card_text(SAMPLE)
    assert card.electric is not None and card.gas is not None
    assert card.electric.rate_plan == "RG1"
    assert card.electric.rate_plan_name == "Residential Electric Service"
    assert card.gas.rate_plan == "GG1"
    assert card.gas.rate_plan_name == "Residential Gas Service"
