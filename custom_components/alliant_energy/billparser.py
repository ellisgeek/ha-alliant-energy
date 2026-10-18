"""Parse Alliant itemized bill PDFs.

The JSON API exposes cost only per billing cycle, rounded to whole dollars, and
nothing else from the bill. The itemized bill PDF (see :mod:`.api`
``async_get_bill_pdf``) has much more, which this module reads:

  per service: the rate plan, the marginal rate and its components, the daily
               customer charge, the billing period and days billed, the meter
               register readings, and (gas) the heat factor
  per bill:    the bill date, the next meter reading window, and the average
               temperature and degree days for the period

The marginal rate is the sum of the usage-proportional charges:

  electric: Energy Charge + Fuel Adjustment ($/kWh)
  gas:      Distribution Charge + Gas Supply Base Rate + Gas Supply Market Adj
            ($/therm)

A service can have more than one section on a bill (for example a short period
when service started). Each section starts at its "Rate:" line; the section
with the latest billing period is used.

Parsing is best-effort and depends on the bill layout. pdfplumber can
interleave text from side columns, so every pattern is anchored to one line.
Anything that cannot be read is left as None.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import asdict, dataclass, field, fields
from datetime import date, datetime
from typing import Any

from .api import SERVICE_ELECTRIC, SERVICE_GAS

_LOGGER = logging.getLogger(__name__)

# Bump when parsing changes what a bill yields, so saved bills are parsed again.
PARSER_VERSION = 1

# The component each service's adjustment sensor shows.
_ADJUSTMENT_COMPONENT = {SERVICE_ELECTRIC: "fuel_adjustment", SERVICE_GAS: "supply_market_adj"}

# Per-unit rate on a labelled charge line, e.g. "... 339.000 kWhX$0.17011 ...".
_KWH_RATE = re.compile(r"kWhX\$(-?\d+(?:\.\d+)?)")
_THERM_RATE = re.compile(r"thermsX\$(-?\d+(?:\.\d+)?)")
_DAYS_RATE = re.compile(r"DaysX\$(-?\d+(?:\.\d+)?)")
# Section start, e.g. "Rate: RG1 - Residential Electric Service Billed for: 31 Days".
_RATE_PLAN = re.compile(r"^Rate:\s*(\S+)\s*-\s*(.+?)(?:\s+Billed for:\s*(\d+)\s*Days?)?$")
# Meter line, e.g.
#   "Sep 11 - Oct 12 300000003 70,190 69,851 339 kWh 1 339 kWh"
#   "Sep 11 - Oct 12 000000004 6,207 6,195 12 CCF 1 1.06 13therms"
_METER_LINE = re.compile(
    r"^(?P<start>[A-Z][a-z]{2} \d{1,2}) - (?P<end>[A-Z][a-z]{2} \d{1,2})\s+"
    r"(?P<meter>\d+)\s+(?P<current>[\d,]+(?:\.\d+)?)\s+(?P<previous>[\d,]+(?:\.\d+)?)\s+"
    r"[\d,.]+\s*(?P<unit>kWh|CCF|therms?)\s+[\d.]+"
    r"(?:\s+(?P<heat>\d+\.\d+))?\s+[\d,.]+\s*(?:kWh|therms?)$"
)
_BILL_DATE = re.compile(r"Bill Date\s+([A-Z][a-z]{2} \d{1,2}, \d{4})")
_NEXT_READ = re.compile(
    r"Next Meter Reading:\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*-\s*([A-Z][a-z]{2} \d{1,2}, \d{4})"
)
_AVG_TEMP = re.compile(r"^Avg\. Temp\s+(-?\d+(?:\.\d+)?)\s*°\s*F$")
_DEGREE_DAYS = re.compile(r"^Degree Days\s+(-?\d+(?:\.\d+)?)$")


@dataclass(frozen=True)
class ServiceBill:
    """What one service's section of a bill says."""

    marginal_rate: float | None = None  # $/unit (kWh or therm), usage charges only
    customer_charge_per_day: float | None = None
    components: dict[str, float] = field(default_factory=dict)
    rate_plan: str | None = None  # rate schedule code, e.g. "RG1"
    rate_plan_name: str | None = None  # e.g. "Residential Electric Service"
    period_start: date | None = None
    period_end: date | None = None  # the meter reading date that closed the period
    billed_days: int | None = None
    meter_number: str | None = None
    meter_reading: float | None = None  # current register reading
    previous_meter_reading: float | None = None
    meter_unit: str | None = None  # "kWh" or "CCF"
    heat_factor: float | None = None  # gas: therms per CCF


@dataclass(frozen=True)
class ItemizedBill:
    """What an itemized bill says."""

    electric: ServiceBill | None = None
    gas: ServiceBill | None = None
    bill_date: date | None = None
    next_read_start: date | None = None
    next_read_end: date | None = None
    average_temperature: float | None = None  # °F over the billing period
    degree_days: float | None = None

    def section(self, service: str) -> ServiceBill | None:
        """One service's section (SERVICE_ELECTRIC or SERVICE_GAS), if billed."""
        return self.electric if service == SERVICE_ELECTRIC else self.gas

    def adjustment(self, service: str) -> float | None:
        """The service's adjustment rate: fuel (electric) or supply market (gas)."""
        section = self.section(service)
        return section.components.get(_ADJUSTMENT_COMPONENT[service]) if section else None


def parse_bill(pdf_bytes: bytes) -> ItemizedBill:
    """Parse a bill PDF into a :class:`ItemizedBill` (best-effort)."""
    import pdfplumber  # imported lazily so the dep only loads when parsing

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_bill_text(text)


def parse_bill_text(text: str) -> ItemizedBill:
    """Parse already-extracted bill text into a :class:`ItemizedBill`."""
    lines = [line.strip() for line in text.splitlines()]
    bill_date = _search_date(_BILL_DATE, text)
    next_read = _NEXT_READ.search(text)

    best: dict[str, ServiceBill] = {}
    for service, section in _sections(lines):
        parsed = _parse_section(service, section, bill_date)
        current = best.get(service)
        if current is None or _section_key(parsed) >= _section_key(current):
            best[service] = parsed

    return ItemizedBill(
        electric=best.get(SERVICE_ELECTRIC),
        gas=best.get(SERVICE_GAS),
        bill_date=bill_date,
        next_read_start=_parse_date(next_read.group(1)) if next_read else None,
        next_read_end=_parse_date(next_read.group(2)) if next_read else None,
        average_temperature=_first_float(_AVG_TEMP, lines),
        degree_days=_first_float(_DEGREE_DAYS, lines),
    )


def _sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Split the bill into (service, lines) at each "Rate:" line."""
    sections: list[tuple[str, list[str]]] = []
    for line in lines:
        if (m := _RATE_PLAN.match(line)) is not None:
            name = m.group(2)
            if "Gas" in name:
                sections.append((SERVICE_GAS, [line]))
            elif "Electric" in name:
                sections.append((SERVICE_ELECTRIC, [line]))
            else:
                sections.append(("", [line]))  # unknown service: ignored
        elif sections:
            sections[-1][1].append(line)
    return [(service, body) for service, body in sections if service]


def _parse_section(service: str, lines: list[str], bill_date: date | None) -> ServiceBill:
    components: dict[str, float] = {}
    customer: float | None = None
    plan = _RATE_PLAN.match(lines[0])
    meter: re.Match[str] | None = None

    for line in lines[1:]:
        if meter is None and (m := _METER_LINE.match(line)):
            meter = m
        # Charges; the last line wins (e.g. the newest of prorated adjustments).
        elif line.startswith("Energy Charge") and (m := _KWH_RATE.search(line)):
            components["energy_charge"] = float(m.group(1))
        elif "Fuel Adjustment" in line and (m := _KWH_RATE.search(line)):
            components["fuel_adjustment"] = float(m.group(1))
        elif line.startswith("Distribution Charge") and (m := _THERM_RATE.search(line)):
            components["distribution"] = float(m.group(1))
        elif line.startswith("Gas Supply Base Rate") and (m := _THERM_RATE.search(line)):
            components["supply_base"] = float(m.group(1))
        elif "Gas Supply Market Adj" in line and (m := _THERM_RATE.search(line)):
            components["supply_market_adj"] = float(m.group(1))
        elif line.startswith("Customer Charge") and (m := _DAYS_RATE.search(line)):
            customer = float(m.group(1))

    required = (
        ("energy_charge", "fuel_adjustment")
        if service == SERVICE_ELECTRIC
        else ("distribution", "supply_base", "supply_market_adj")
    )
    # The primary charge must be present; adjustments add in when listed.
    marginal = (
        sum(components.get(key, 0.0) for key in required) if required[0] in components else None
    )
    start, end = (
        _period(meter.group("start"), meter.group("end"), bill_date) if meter else (None, None)
    )
    return ServiceBill(
        marginal_rate=marginal,
        customer_charge_per_day=customer,
        components=components,
        rate_plan=plan.group(1) if plan else None,
        rate_plan_name=plan.group(2).strip() if plan else None,
        period_start=start,
        period_end=end,
        billed_days=int(plan.group(3)) if plan and plan.group(3) else None,
        meter_number=meter.group("meter") if meter else None,
        meter_reading=_number(meter.group("current")) if meter else None,
        previous_meter_reading=_number(meter.group("previous")) if meter else None,
        meter_unit=_meter_unit(meter.group("unit")) if meter else None,
        heat_factor=float(meter.group("heat")) if meter and meter.group("heat") else None,
    )


def _section_key(section: ServiceBill) -> tuple[date, int]:
    """Prefer the latest billing period, then the longest."""
    return (section.period_end or date.min, section.billed_days or 0)


def _period(start: str, end: str, bill_date: date | None) -> tuple[date | None, date | None]:
    """Turn "Sep 11" - "Oct 12" into dates; the year comes from the bill date.

    The period ends on or before the bill date, so a period ending in a later
    month than the bill (e.g. "Dec 05" on a January bill) is from the previous
    year, as is a start month after the end month.
    """
    if bill_date is None:
        return None, None
    try:
        end_date = datetime.strptime(f"{end} {bill_date.year}", "%b %d %Y").date()
        if end_date > bill_date:
            end_date = end_date.replace(year=bill_date.year - 1)
        start_date = datetime.strptime(f"{start} {end_date.year}", "%b %d %Y").date()
        if start_date > end_date:
            start_date = start_date.replace(year=end_date.year - 1)
    except ValueError:
        return None, None
    return start_date, end_date


def _meter_unit(unit: str) -> str:
    return "CCF" if unit == "CCF" else "kWh" if unit == "kWh" else "therm"


def _number(value: str) -> float:
    return float(value.replace(",", ""))


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%b %d, %Y").date()
    except ValueError:
        return None


def _search_date(pattern: re.Pattern[str], text: str) -> date | None:
    m = pattern.search(text)
    return _parse_date(m.group(1)) if m else None


def _first_float(pattern: re.Pattern[str], lines: list[str]) -> float | None:
    for line in lines:
        if (m := pattern.match(line)) is not None:
            return float(m.group(1))
    return None


def bill_to_dict(bill: ItemizedBill) -> dict[str, Any]:
    """A JSON-safe dict of a :class:`ItemizedBill` (dates as ISO strings)."""
    return _jsonable(asdict(bill))


def bill_from_dict(data: dict[str, Any]) -> ItemizedBill:
    """Rebuild a :class:`ItemizedBill` saved with :func:`bill_to_dict`."""
    rest = {k: v for k, v in data.items() if k not in (SERVICE_ELECTRIC, SERVICE_GAS)}
    return ItemizedBill(
        electric=_service_from_dict(data.get(SERVICE_ELECTRIC)),
        gas=_service_from_dict(data.get(SERVICE_GAS)),
        **_dates(ItemizedBill, rest),
    )


def _service_from_dict(data: dict[str, Any] | None) -> ServiceBill | None:
    return ServiceBill(**_dates(ServiceBill, data)) if data else None


def _jsonable(value: Any) -> Any:
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _dates(cls: type[ItemizedBill] | type[ServiceBill], data: dict[str, Any]) -> dict[str, Any]:
    """Turn ISO strings back into dates for the date fields of a dataclass."""
    date_fields = {f.name for f in fields(cls) if "date" in str(f.type)}
    return {
        k: date.fromisoformat(v) if k in date_fields and isinstance(v, str) else v
        for k, v in data.items()
    }
