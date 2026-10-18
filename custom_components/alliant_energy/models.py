"""What the coordinator hands the sensors."""

from __future__ import annotations

from dataclasses import dataclass, field

from .api import SERVICE_ELECTRIC, SERVICE_GAS, BillSummary
from .bill import Bill
from .billparser import ServiceBill


@dataclass(frozen=True)
class ServiceData:
    """What the sensors show for one service (electric or gas)."""

    # The latest daily usage reading, in the service's usage unit.
    latest_usage: float | None = None
    # The amount of the latest billed billing cycle.
    last_bill_cost: float | None = None
    # Cost rate for the newest hour, and where it came from.
    rate: float | None = None  # $/kWh or $/therm
    rate_source: str | None = None  # one of rates.RATE_SOURCE_*
    # This service's section of the itemized bill, and its adjustment rate.
    section: ServiceBill | None = None
    adjustment: float | None = None  # electric fuel adj. or gas supply market adj., $/unit
    # Projection for the billing cycle in progress.
    usage_to_date: float | None = None
    days_to_date: int | None = None  # days of usage data in the cycle so far
    cycle_days: int | None = None  # expected length of the cycle
    cost_to_date: float | None = None
    projected_cost: float | None = None


@dataclass(frozen=True)
class EnergyData:
    """What the coordinator hands the sensors: the latest bill and each service."""

    bill: Bill = field(default_factory=lambda: Bill(BillSummary()))
    # Per service, keyed by SERVICE_ELECTRIC / SERVICE_GAS.
    services: dict[str, ServiceData] = field(default_factory=dict)
    currency: str | None = None
    # Sum of the services' projected costs for the cycle in progress.
    projected_bill: float | None = None

    @property
    def billing_period(self) -> ServiceBill | None:
        """The billed period: the electric section's, or else the gas section's."""
        return self.bill.section(SERVICE_ELECTRIC) or self.bill.section(SERVICE_GAS)

    def service(self, service: str) -> ServiceData:
        """One service's data; empty when the account has no such service."""
        return self.services.get(service) or ServiceData()
