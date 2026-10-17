"""Parse Alliant itemized bill PDFs for exact per-unit rates.

The JSON usage API only exposes cost rounded to whole dollars and no rate
breakdown. The itemized bill PDF (see :mod:`.api` ``async_get_bill_pdf``) has
the real line items, from which the marginal energy rate per fuel can be read:

  electric: Energy Charge ($/kWh) + Fuel Adjustment ($/kWh)
  gas:      Distribution Charge + Gas Supply Base Rate + Gas Supply Market Adj
            (all $/therm)

Parsing is best-effort and layout-dependent; callers fall back to an effective
rate derived from the bill total when a rate cannot be read.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

_LOGGER = logging.getLogger(__name__)

# Per-unit rate on a labelled charge line, e.g. "... 339.000 kWhX$0.17011 ...".
_KWH_RATE = re.compile(r"kWhX\$(-?\d+(?:\.\d+)?)")
_THERM_RATE = re.compile(r"thermsX\$(-?\d+(?:\.\d+)?)")
_DAYS_RATE = re.compile(r"DaysX\$(-?\d+(?:\.\d+)?)")

_ELECTRIC_HEADER = "Residential Electric Service"
_GAS_HEADER = "Residential Gas Service"


@dataclass(frozen=True)
class FuelRate:
    """The rate breakdown for one fuel read from a bill."""

    marginal_rate: float  # $/unit (kWh or therm): usage-proportional components
    customer_charge_per_day: float | None = None
    components: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class RateCard:
    """Rates read from an itemized bill."""

    electric: FuelRate | None = None
    gas: FuelRate | None = None


def parse_rate_card(pdf_bytes: bytes) -> RateCard:
    """Parse a bill PDF into a :class:`RateCard` (best-effort)."""
    import pdfplumber  # imported lazily so the dep only loads when parsing

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_rate_card_text(text)


def parse_rate_card_text(text: str) -> RateCard:
    """Parse already-extracted bill text into a :class:`RateCard`."""
    electric_components: dict[str, float] = {}
    gas_components: dict[str, float] = {}
    electric_customer: float | None = None
    gas_customer: float | None = None
    section: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if _ELECTRIC_HEADER in line:
            section = "electric"
        elif _GAS_HEADER in line:
            section = "gas"

        # Electric components (unambiguous labels; keep the most recent value).
        if line.startswith("Energy Charge") and (m := _KWH_RATE.search(line)):
            electric_components["energy_charge"] = float(m.group(1))
        elif "Fuel Adjustment" in line and (m := _KWH_RATE.search(line)):
            electric_components["fuel_adjustment"] = float(m.group(1))
        # Gas components.
        elif line.startswith("Distribution Charge") and (m := _THERM_RATE.search(line)):
            gas_components["distribution"] = float(m.group(1))
        elif line.startswith("Gas Supply Base Rate") and (m := _THERM_RATE.search(line)):
            gas_components["supply_base"] = float(m.group(1))
        elif "Gas Supply Market Adj" in line and (m := _THERM_RATE.search(line)):
            gas_components["supply_market_adj"] = float(m.group(1))
        # Customer charge appears for both fuels; scope by section.
        elif line.startswith("Customer Charge") and (m := _DAYS_RATE.search(line)):
            if section == "gas":
                gas_customer = float(m.group(1))
            elif section == "electric":
                electric_customer = float(m.group(1))

    electric = _build_fuel_rate(
        ("energy_charge", "fuel_adjustment"), electric_components, electric_customer
    )
    gas = _build_fuel_rate(
        ("distribution", "supply_base", "supply_market_adj"),
        gas_components,
        gas_customer,
    )
    return RateCard(electric=electric, gas=gas)


def _build_fuel_rate(
    required: tuple[str, ...],
    components: dict[str, float],
    customer_charge: float | None,
) -> FuelRate | None:
    """Sum the usage-proportional components into a marginal rate.

    Requires at least the first (primary) component; optional adjustments add in
    when present.
    """
    if required[0] not in components:
        return None
    marginal = sum(components.get(key, 0.0) for key in required)
    return FuelRate(
        marginal_rate=marginal,
        customer_charge_per_day=customer_charge,
        components=dict(components),
    )
