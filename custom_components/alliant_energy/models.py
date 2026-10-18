"""Shared data models for utility readings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

# Where a rate came from, in order of precedence.
RATE_SOURCE_OPTIONS = "options"  # fixed rate set in the integration options
RATE_SOURCE_BILL = "bill"  # marginal rate parsed from the latest bill PDF
RATE_SOURCE_BILLING_CYCLE = "billing_cycle"  # latest cycle's amount / usage


@dataclass(frozen=True)
class RateInfo:
    """The rate and rate plan for one service (electric or gas)."""

    rate: float | None = None  # $/kWh or $/therm, as used for cost
    source: str | None = None  # one of the RATE_SOURCE_* values
    rate_plan: str | None = None  # rate schedule code from the bill, e.g. "RG1"
    rate_plan_name: str | None = None
    customer_charge_per_day: float | None = None


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

    # Rate and rate plan per service, keyed by "electric"/"gas".
    rates: dict[str, RateInfo] = field(default_factory=dict)
