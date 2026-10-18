"""What differs between utilities on the SEW SmartCMobile platform.

The backend's endpoints, request signature, and response shapes belong to the
SmartCMobile product, so :mod:`.api` holds them. Each utility runs its own
deployment, though, with its own host, utility id, and codes. A
:class:`Utility` collects those values in one place (ADR 0014).

Only Alliant Energy is defined. Its values were confirmed live; another
utility's values must be confirmed the same way before it is added, because a
wrong unit code returns an empty result, not an error. No Home Assistant
imports.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from .api import SERVICE_ELECTRIC, SERVICE_GAS
from .billparser import ItemizedBill, parse_bill


@dataclass(frozen=True)
class Utility:
    """One utility's SmartCMobile deployment."""

    name: str
    api_host: str  # backend host, e.g. "alliant-svc.smartcmobile.com"
    portal_url: str  # the customer portal, sent as Origin (and Referer + "/Portal/")
    utility_id: str  # the "uid" header
    persona_type: str  # the "pt" header on usage requests
    meter_types: Mapping[int, str]  # GetMeterAndPremise meterType -> service
    # The usage API's Uom value per service. Hourly/daily and billing-cycle
    # requests can need different spellings.
    interval_uom: Mapping[str, str]
    monthly_uom: Mapping[str, str]
    # Reads the itemized bill PDF. None when the utility's bill layout has no
    # parser; the bill then has only its summary.
    bill_parser: Callable[[bytes], ItemizedBill] | None = None

    @property
    def api_root(self) -> str:
        """The base URL of the backend's service roots."""
        return f"https://{self.api_host}/"

    @property
    def referer(self) -> str:
        """The Referer the portal sends."""
        return f"{self.portal_url}/Portal/"


ALLIANT_ENERGY = Utility(
    name="Alliant Energy",
    api_host="alliant-svc.smartcmobile.com",
    portal_url="https://myaccount.alliantenergy.com",
    utility_id="1",
    persona_type="1",
    meter_types={1: SERVICE_ELECTRIC, 2: SERVICE_GAS},
    # Gas is the trap: hourly needs "Therm" but billing-cycle needs "TH".
    interval_uom={SERVICE_ELECTRIC: "kWh", SERVICE_GAS: "Therm"},
    monthly_uom={SERVICE_ELECTRIC: "kWh", SERVICE_GAS: "TH"},
    bill_parser=parse_bill,
)
