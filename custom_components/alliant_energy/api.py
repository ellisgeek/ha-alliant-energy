"""Low-level client for the Alliant Energy (SEW SmartCMobile) JSON API.

Alliant Energy does not publish a customer API. Its self-service portal at
https://myaccount.alliantenergy.com/ is a Smart Energy Water (SEW)
"SmartCMobile" single-page app that talks to a JSON REST backend at
``alliant-svc.smartcmobile.com``. This module reproduces the subset of that
backend the portal uses to read electric and gas usage. The values that belong
to one utility's deployment (host, utility id, codes) come from a
:class:`.utility.Utility` (ADR 0014).

The backend sits behind Cloudflare bot management that blocks ordinary HTTP
clients (``aiohttp``, ``requests``, plain ``curl``) by TLS fingerprint, before
the request ever reaches the application. This client therefore uses
``curl_cffi`` with Chrome TLS impersonation, which is accepted; Home
Assistant's shared ``aiohttp`` session would be rejected with HTTP 403.

Everything else (field names, the request-signing scheme) mirrors what the
portal's own JavaScript sends, so it can change without notice.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

from curl_cffi.requests import AsyncSession
from curl_cffi.requests.exceptions import RequestException

if TYPE_CHECKING:
    from curl_cffi.requests.session import HttpMethod

    from .utility import Utility

_LOGGER = logging.getLogger(__name__)

# TLS fingerprint to impersonate. A recent stable Chrome is a safe default.
IMPERSONATE = "chrome"

# Pause between consecutive requests, to keep the load on the account gentle.
REQUEST_PAUSE_SECONDS = 0.5

# Backend service roots, relative to the utility's host. The portal splits its
# API across several paths; these are the only ones needed for authentication,
# discovery, and usage.
_USER_MANAGEMENT = "UsermanagementAPI/api/1/"
_SCM_SERVICE = "Services/api/1/"
_USAGE = "UsageAPI/api/V1/"

# Device attributes the portal sends on auth-related calls. Left blank as a
# generic web client; device-remembering is disabled on Alliant's deployment.
_CUSTOM_ATTRS = {
    "deviceId": "",
    "deviceName": "",
    "client": "Web",
    "version": "1.0",
    "os": "",
    "ip": "#1",
}

SERVICE_ELECTRIC = "electric"
SERVICE_GAS = "gas"

# Usage endpoints keyed by service type, plus the response array each returns.
_USAGE_ENDPOINT = {SERVICE_ELECTRIC: "Electric", SERVICE_GAS: "Gas"}
_USAGE_RESULT_KEY = {SERVICE_ELECTRIC: "electricUsages", SERVICE_GAS: "gasUsages"}

# Valid periodicity codes accepted by the usage API.
PERIODICITY_DAILY = "DA"
PERIODICITY_HOURLY = "HH"
PERIODICITY_MONTHLY = "MO"
PERIODICITY_INTERVAL = "IN"

# Two-factor delivery channels (SendTfa "mode"). Alliant enforces MFA only in
# its web UI today, so these are a forward-looking fallback; the integer
# mapping is taken from the portal and not yet exercised against a live
# challenge. TEXT/EMAIL cover the channels the login response advertises.
TFA_MODE_TEXT = 1
TFA_MODE_EMAIL = 2
TFA_MODE_VOICE = 3


class AlliantEnergyError(Exception):
    """Base error for the Alliant Energy client."""


class AlliantEnergyAuthError(AlliantEnergyError):
    """Raised when authentication fails or the session cannot be refreshed."""


@dataclass(frozen=True)
class Account:
    """An account/premise pairing for the logged-in user."""

    account_number: str
    premise_number: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class Meter:
    """A metering point returned by GetMeterAndPremise."""

    meter_number: str
    premise_number: str
    service_type: str  # SERVICE_ELECTRIC or SERVICE_GAS
    raw: dict[str, Any]


@dataclass(frozen=True)
class UsageReading:
    """A single usage record for a meter over a reading interval.

    ``reading_date`` carries the timestamp for hourly/daily records; monthly
    (billing-period) records leave it as a bogus ``0001-…`` value and put the
    real period in ``reading_from``/``reading_to``.
    """

    usage: float | None
    cost: float | None
    reading_date: str | None
    reading_from: str | None
    reading_to: str | None
    unit: str | None


@dataclass(frozen=True)
class BillSummary:
    """The headline figures of an account's most recent bill (bill/Current)."""

    amount_due: float | None = None
    current_charges: float | None = None
    past_due_amount: float | None = None
    previous_balance: float | None = None
    due_date: date | None = None
    invoice_date: date | None = None
    invoice_id: str | None = None  # identifies the bill's itemized PDF


@dataclass(frozen=True)
class PastBill:
    """One of an account's past bills (bill/History): its invoice and date."""

    invoice_id: str  # identifies the bill's itemized PDF
    bill_date: date | None


def _sign(body: Any) -> str:
    """Return the portal's request signature for a JSON body.

    The web app sends ``x-signature``/``x-inputchecksum`` equal to the
    uppercased MD5 hex digest of the serialized body with the final two
    characters removed. The body must be serialized exactly as the signed
    payload, so the same string is used for both the signature and the request.
    """
    serialized = json.dumps(body, separators=(",", ":"))
    digest = hashlib.md5(serialized.encode()).hexdigest().upper()  # noqa: S324
    return digest[:-2]


class AlliantEnergyApi:
    """Authenticated client for one utility's SmartCMobile backend.

    Owns a ``curl_cffi`` session unless one is injected (used by tests). Call
    :meth:`close` when finished if the session is owned.

    Requests are paced per client: one at a time, each ``pause`` seconds
    (default ``REQUEST_PAUSE_SECONDS``) after the previous one ended, even when
    callers sharing this client overlap.
    """

    def __init__(
        self,
        utility: Utility,
        session: Any | None = None,
        pause: float | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._utility = utility
        self._user_management = utility.api_root + _USER_MANAGEMENT
        self._scm_service = utility.api_root + _SCM_SERVICE
        self._usage = utility.api_root + _USAGE
        self._session = session or AsyncSession(impersonate=IMPERSONATE)
        self._owns_session = session is None
        self._pause = REQUEST_PAUSE_SECONDS if pause is None else pause
        self._sleep = sleep
        self._clock = clock
        self._send_lock = asyncio.Lock()
        self._last_sent: float | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._user_uuid: str | None = None
        self._tfa_required = False
        self._tfa_details: dict[str, Any] = {}
        self._tfa_persona = "1"

    @property
    def user_uuid(self) -> str | None:
        """The authenticated user's uuid, available after login."""
        return self._user_uuid

    @property
    def tfa_required(self) -> bool:
        """Whether the account has two-factor authentication enabled.

        Note: Alliant currently enforces MFA only in its web UI; API calls
        succeed with the login token regardless. This flag lets callers offer a
        challenge step as a fallback if that ever changes.
        """
        return self._tfa_required

    @property
    def tfa_details(self) -> dict[str, Any]:
        """Masked phone/email the login response offers for TFA delivery."""
        return self._tfa_details

    async def close(self) -> None:
        """Close the underlying session if this client created it."""
        if self._owns_session:
            await self._session.close()

    async def async_login(self, username: str, password: str) -> None:
        """Authenticate and store the access/refresh tokens.

        The portal's web client attaches a Google reCAPTCHA v3 token to this
        request, but the backend does not enforce it, so no token is sent.
        """
        body = {
            "username": username,
            "password": password,
            "guestToken": "",
            "customattributes": {
                "ip": "#1",
                "client": "Web",
                "version": "1.0",
                "deviceId": "",
                "deviceName": "",
                "deviceType": 0,
                "os": "",
            },
        }
        data = await self._request(
            "POST",
            self._user_management + "Login/auth",
            body=body,
            signed=True,
            authenticated=False,
        )
        self._store_tokens(data)

    async def async_refresh_token(self) -> None:
        """Exchange the refresh token for a new token pair."""
        if not self._refresh_token:
            raise AlliantEnergyAuthError("No refresh token available")
        body = {"refreshToken": self._refresh_token}
        data = await self._request(
            "POST",
            self._user_management + "Login/auth/refresh",
            body=body,
            signed=True,
            authenticated=False,
        )
        self._store_tokens(data)

    def _store_tokens(self, data: dict[str, Any]) -> None:
        payload = data.get("data") or {}
        access = payload.get("accessToken")
        if not access:
            raise AlliantEnergyAuthError("Login response did not contain an access token")
        self._access_token = access
        refresh = payload.get("refreshToken")
        if refresh:
            self._refresh_token = refresh
        user = payload.get("user") or {}
        self._user_uuid = user.get("uuid") or self._user_uuid
        tfa_details = payload.get("tfaDetails") or {}
        self._tfa_required = bool(user.get("tfa")) and bool(tfa_details)
        self._tfa_details = tfa_details
        self._tfa_persona = str(user.get("defaultPersona") or self._tfa_persona)

    async def async_send_tfa(self, mode: int) -> dict[str, Any]:
        """Request a two-factor code be sent over the given channel.

        Forward-looking fallback; not exercised against a live challenge.
        """
        if not self._user_uuid:
            raise AlliantEnergyAuthError("Not authenticated")
        body = {
            "uuid": self._user_uuid,
            "mode": mode,
            "CustomAttributes": _CUSTOM_ATTRS,
        }
        return await self._request(
            "POST", self._user_management + "users/SendTfa", body=body, service="tfa"
        )

    async def async_verify_tfa(self, code: str) -> dict[str, Any]:
        """Submit a two-factor code to complete authentication.

        Forward-looking fallback; not exercised against a live challenge.
        """
        if not self._user_uuid:
            raise AlliantEnergyAuthError("Not authenticated")
        body = {
            "uuid": self._user_uuid,
            "code": code,
            "CustomAttributes": _CUSTOM_ATTRS,
        }
        return await self._request(
            "POST", self._user_management + "users/VerifyTfa", body=body, service="tfa"
        )

    async def async_get_accounts(self) -> list[Account]:
        """Return the accounts/premises belonging to the logged-in user."""
        if not self._user_uuid:
            raise AlliantEnergyAuthError("Not authenticated")
        data = await self._request("GET", self._scm_service + f"Addresses/User/{self._user_uuid}")
        accounts: list[Account] = []
        for raw in data.get("data") or []:
            account_number = str(raw.get("accountNumber") or "")
            premise_number = str(raw.get("premiseNumber") or "")
            if not account_number or not premise_number:
                continue
            accounts.append(
                Account(
                    account_number=account_number,
                    premise_number=premise_number,
                    raw=raw,
                )
            )
        return accounts

    async def async_get_meters(self, account_number: str, premise_number: str) -> list[Meter]:
        """Return the meters on an account/premise."""
        body = {"accountNumber": account_number, "premiseNumber": premise_number}
        data = await self._request(
            "POST",
            self._scm_service + "Usages/GetMeterAndPremise",
            body=body,
            signed=True,
        )
        meters: list[Meter] = []
        for raw in data.get("data") or []:
            service_type = self._service_for_meter_type(raw.get("meterType"))
            if service_type is None:
                continue
            meters.append(
                Meter(
                    meter_number=str(raw.get("meterNumber", "")),
                    premise_number=str(raw.get("premiseNumber", premise_number)),
                    service_type=service_type,
                    raw=raw,
                )
            )
        return meters

    async def async_get_current_bill(self, account_number: str) -> BillSummary:
        """Return the bill summary of the account's most recent bill."""
        data = await self._request(
            "POST",
            self._scm_service + "bill/Current",
            body={"accountNumbers": [account_number]},
            signed=True,
        )
        bill = data.get("data") or {}
        return BillSummary(
            amount_due=_to_float(bill.get("totalAmountDue")),
            current_charges=_to_float(bill.get("currentCharges")),
            past_due_amount=_to_float(bill.get("pastDueAmount")),
            previous_balance=_to_float(bill.get("previousBalance")),
            due_date=_parse_mdy(bill.get("netDueDate")),
            invoice_date=_parse_mdy(bill.get("invoiceDate")),
            invoice_id=bill.get("invoiceId") or None,
        )

    async def async_get_bill_history(
        self, account_number: str, start: date, end: date
    ) -> list[PastBill]:
        """Return the account's bills issued between two dates, newest first."""
        data = await self._request(
            "POST",
            _SCM_SERVICE + "bill/History",
            body={
                "accountNumbers": [account_number],
                "startDate": start.strftime("%m/%d/%Y"),
                "endDate": end.strftime("%m/%d/%Y"),
            },
            signed=True,
        )
        rows = data.get("data") or []
        return [
            PastBill(invoice_id=str(row["invoiceId"]), bill_date=_parse_mdy(row.get("billDate")))
            for row in rows
            if isinstance(row, dict) and row.get("invoiceId")
        ]

    async def async_get_bill_pdf(
        self, account_number: str, invoice_id: str, doc_type: str = "BILL"
    ) -> bytes | None:
        """Return the itemized bill PDF bytes for an invoice, if available.

        The API returns a signed, short-lived URL on a third-party eBill host
        rather than the bytes directly, so this follows that URL.
        """
        data = await self._request(
            "POST",
            self._scm_service + "bill/GetBillPdf",
            body={
                "accountNumber": account_number,
                "invoiceId": invoice_id,
                "documentType": doc_type,
            },
            signed=True,
        )
        url = (data.get("data") or {}).get("billPdf")
        if not url:
            return None
        try:
            resp = await self._send("GET", url)
        except RequestException as err:
            raise AlliantEnergyError(f"Bill PDF download failed: {err}") from err
        if resp.status_code >= 400:
            raise AlliantEnergyError(f"Bill PDF download failed: HTTP {resp.status_code}")
        content: bytes = resp.content
        return content

    async def async_get_usage(
        self,
        service_type: str,
        account_number: str,
        premise_number: str,
        meter_number: str,
        from_date: str,
        to_date: str,
        periodicity: str = PERIODICITY_DAILY,
        unit: str | None = None,
    ) -> list[UsageReading]:
        """Return usage readings for a meter between two ``MM/DD/YYYY`` dates.

        The usage API expects ``AccountNumber`` as ``"{premise}-{account}"``.
        """
        endpoint = _USAGE_ENDPOINT.get(service_type)
        if endpoint is None:
            raise AlliantEnergyError(f"Unsupported service type: {service_type}")
        if unit:
            uom = unit
        elif periodicity == PERIODICITY_MONTHLY:
            uom = self._utility.monthly_uom[service_type]
        else:
            uom = self._utility.interval_uom[service_type]
        params = {
            "AccountNumber": f"{premise_number}-{account_number}",
            "MeterNumber": meter_number,
            "From": from_date,
            "To": to_date,
            "Uom": uom,
            "Periodicity": periodicity,
        }
        data = await self._request("GET", self._usage + endpoint, params=params, service="usage")
        result = data.get("Result") or data.get("result") or {}
        records = result.get(_USAGE_RESULT_KEY[service_type]) or []
        return [_parse_reading(record, uom) for record in records]

    def _service_for_meter_type(self, meter_type: Any) -> str | None:
        if not isinstance(meter_type, int):
            return None
        return self._utility.meter_types.get(meter_type)

    async def _send(self, method: HttpMethod, url: str, **kwargs: Any) -> Any:
        """Send one request on the session, paced after the previous one."""
        async with self._send_lock:
            if self._last_sent is not None:
                wait = self._pause - (self._clock() - self._last_sent)
                if wait > 0:
                    await self._sleep(wait)
            try:
                return await self._session.request(method, url, **kwargs)
            finally:
                self._last_sent = self._clock()

    async def _request(
        self,
        method: HttpMethod,
        url: str,
        *,
        body: Any = None,
        params: dict[str, Any] | None = None,
        signed: bool = False,
        authenticated: bool = True,
        service: str | None = None,
        _retry: bool = True,
    ) -> dict[str, Any]:
        """Perform a request, handling signing, auth headers, and 401 refresh."""
        headers: dict[str, str] = {
            "Accept": "application/json",
            "uid": self._utility.utility_id,
            "Origin": self._utility.portal_url,
            "Referer": self._utility.referer,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        if signed and body is not None:
            signature = _sign(body)
            headers["x-signature"] = signature
            headers["x-inputchecksum"] = signature
        if service == "usage":
            headers["pt"] = self._utility.persona_type
        elif service == "tfa":
            headers["pt"] = self._tfa_persona
        if authenticated:
            if not self._access_token:
                raise AlliantEnergyAuthError("Not authenticated")
            headers["Authorization"] = f"Bearer {self._access_token}"

        # Serialize the body ourselves so it byte-for-byte matches the signature.
        payload = json.dumps(body, separators=(",", ":")) if body is not None else None
        try:
            resp = await self._send(method, url, data=payload, params=params, headers=headers)
        except RequestException as err:
            raise AlliantEnergyError(f"{method} {url} failed: {err}") from err

        status = resp.status_code
        text = resp.text
        if status == 401 and authenticated and _retry:
            await self.async_refresh_token()
            return await self._request(
                method,
                url,
                body=body,
                params=params,
                signed=signed,
                authenticated=authenticated,
                service=service,
                _retry=False,
            )
        if status >= 400:
            raise AlliantEnergyError(f"{method} {url} failed: HTTP {status}: {text[:300]}")
        try:
            data = json.loads(text) if text else {}
        except json.JSONDecodeError as err:
            raise AlliantEnergyError(f"{method} {url} returned invalid JSON") from err

        _raise_for_status_code(data)
        return data


def _to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except TypeError, ValueError:
        return None


def _parse_mdy(value: Any) -> date | None:
    """Parse the bill API's ``MM-DD-YYYY`` dates."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.strptime(value, "%m-%d-%Y").date()
    except ValueError:
        return None


def _parse_reading(record: dict[str, Any], unit: str) -> UsageReading:
    return UsageReading(
        usage=_to_float(record.get("consumption")),
        cost=_to_float(record.get("amount")),
        reading_date=record.get("readDate"),
        reading_from=record.get("readingFrom"),
        reading_to=record.get("readingTo"),
        unit=record.get("uom") or unit,
    )


def _raise_for_status_code(data: dict[str, Any]) -> None:
    """Raise if the SEW envelope reports a non-success status code.

    Responses wrap results in ``{"status": {"code": 200, ...}, "data": ...}``.
    Usage responses use a flatter ``{"StatusCode": 200, "Result": ...}`` shape
    and are left to the caller.
    """
    status = data.get("status")
    if not isinstance(status, dict):
        return
    code = status.get("code")
    if code in (None, 200):
        return
    message = status.get("message") or status.get("messageCode") or "request failed"
    if code in (401, 403):
        raise AlliantEnergyAuthError(f"Authentication failed ({code}): {message}")
    raise AlliantEnergyError(f"API error ({code}): {message}")
