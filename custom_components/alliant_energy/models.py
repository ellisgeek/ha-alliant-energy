"""Shared data models for utility readings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Where a rate came from, in order of precedence.
RATE_SOURCE_OPTIONS = "options"  # fixed rate set in the integration options
RATE_SOURCE_BILL = "bill"  # marginal rate parsed from the latest bill PDF
RATE_SOURCE_BILLING_CYCLE = "billing_cycle"  # latest cycle's amount / usage


@dataclass(frozen=True)
class ServiceData:
    """Rate, bill, and projection details for one service (electric or gas)."""

    # Rate used for cost, and where it came from.
    rate: float | None = None  # $/kWh or $/therm
    rate_source: str | None = None  # one of the RATE_SOURCE_* values
    # From the latest bill PDF.
    rate_plan: str | None = None  # rate schedule code, e.g. "RG1"
    rate_plan_name: str | None = None
    customer_charge_per_day: float | None = None
    adjustment: float | None = None  # electric fuel adj. or gas supply market adj., $/unit
    meter_number: str | None = None
    meter_reading: float | None = None  # register reading at the end of the period
    previous_meter_reading: float | None = None
    meter_unit: str | None = None  # "kWh" or "CCF"
    heat_factor: float | None = None  # gas: therms per CCF
    # Projection for the billing cycle in progress.
    usage_to_date: float | None = None
    days_to_date: int | None = None  # days of usage data in the cycle so far
    cycle_days: int | None = None  # expected length of the cycle
    cost_to_date: float | None = None
    projected_cost: float | None = None


@dataclass(frozen=True)
class EnergyData:
    """Latest readings and bill summary returned to the coordinator.

    Usage values are the most recent daily reading available; cost fields hold
    the most recent billing-cycle amount. Gas usage is in therms. Bill fields
    are account-level (the bill combines electric and gas).
    """

    electricity_kwh: float | None = None
    gas_therms: float | None = None
    electricity_cost: float | None = None
    gas_cost: float | None = None
    currency: str | None = None
    # reading_date per service, keyed by "electric"/"gas", for diagnostics.
    reading_dates: dict[str, str | None] = field(default_factory=dict)

    # Account-level bill summary (from bill/Current).
    amount_due: float | None = None
    current_charges: float | None = None
    past_due_amount: float | None = None
    previous_balance: float | None = None
    due_date: date | None = None
    invoice_date: date | None = None
    invoice_id: str | None = None

    # Per service, keyed by "electric"/"gas".
    services: dict[str, ServiceData] = field(default_factory=dict)

    # Account-level details from the latest bill PDF.
    billing_period_start: date | None = None
    billing_period_end: date | None = None
    billed_days: int | None = None
    next_meter_read_start: date | None = None
    next_meter_read_end: date | None = None
    average_temperature: float | None = None  # °F over the billing period
    degree_days: float | None = None
    # Sum of the services' projected costs for the cycle in progress.
    projected_bill: float | None = None
