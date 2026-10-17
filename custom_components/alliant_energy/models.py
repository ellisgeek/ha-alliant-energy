"""Shared data models for utility readings."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date


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
