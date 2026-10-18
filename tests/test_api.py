"""Tests for the low-level Alliant Energy API client.

These run fully offline against a fake curl_cffi-style session, so they
exercise signing, header construction, and response parsing without any
network.
"""

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import date
from typing import Any

import pytest
from curl_cffi.requests.exceptions import RequestException

from custom_components.alliant_energy.api import (
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    Account,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
    BillSummary,
    Meter,
    UsageReading,
    _sign,
)
from custom_components.alliant_energy.utility import ALLIANT_ENERGY


class _FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status_code = status
        self.text = json.dumps(payload)
        self.content = self.text.encode()


class _FakeSession:
    """Records requests and returns queued responses in order."""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def request(self, method, url, *, data=None, params=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "data": data,
                "params": params,
                "headers": headers or {},
            }
        )
        return self._responses.pop(0)


def _api(*responses: _FakeResponse) -> tuple[AlliantEnergyApi, _FakeSession]:
    session = _FakeSession(list(responses))
    return AlliantEnergyApi(ALLIANT_ENERGY, session=session), session


def test_sign_matches_portal_scheme() -> None:
    """Signature is the uppercased MD5 of the serialized body, less two chars."""
    body = {"a": 1, "b": "two"}
    serialized = json.dumps(body, separators=(",", ":"))
    full = hashlib.md5(serialized.encode()).hexdigest().upper()

    signature = _sign(body)

    assert signature == full[:-2]
    assert len(signature) == 30


async def test_login_stores_tokens_and_signs_request() -> None:
    """A successful login captures tokens/uuid and signs the request body."""
    api, session = _api(
        _FakeResponse(
            200,
            {
                "status": {"code": 200},
                "data": {
                    "user": {"uuid": "u-1"},
                    "accessToken": "access-abc",
                    "refreshToken": "refresh-xyz",
                },
            },
        )
    )

    await api.async_login("user", "pass")

    assert api.user_uuid == "u-1"
    headers = session.calls[0]["headers"]
    assert headers["x-signature"] == headers["x-inputchecksum"]
    # The signature covers exactly the bytes sent on the wire.
    assert headers["x-signature"] == _sign(json.loads(session.calls[0]["data"]))


async def test_alliant_requests_go_to_its_backend_with_its_headers() -> None:
    """The values confirmed live for Alliant (docs/backend-api.md)."""
    api, session = _api(_FakeResponse(200, {"StatusCode": 200, "Result": {}}))
    api._access_token = "token"  # noqa: SLF001

    await api.async_get_usage(SERVICE_GAS, "acct", "P1", "G1", "09/01/2026", "10/15/2026")

    call = session.calls[0]
    assert call["url"] == "https://alliant-svc.smartcmobile.com/UsageAPI/api/V1/Gas"
    assert call["headers"]["uid"] == "1"
    assert call["headers"]["Origin"] == "https://myaccount.alliantenergy.com"
    assert call["headers"]["Referer"] == "https://myaccount.alliantenergy.com/Portal/"
    assert call["params"]["Uom"] == "Therm"


async def test_requests_use_the_utilitys_deployment() -> None:
    """Host, utility id, persona, and codes all come from the utility."""
    other = replace(
        ALLIANT_ENERGY,
        api_host="other-svc.example",
        portal_url="https://portal.example",
        utility_id="7",
        persona_type="3",
        meter_types={5: SERVICE_GAS},
        monthly_uom={SERVICE_ELECTRIC: "KWH", SERVICE_GAS: "CCF"},
    )
    session = _FakeSession(
        [
            _FakeResponse(
                200,
                {
                    "status": {"code": 200},
                    "data": [
                        {"meterNumber": "E1", "premiseNumber": "P1", "meterType": 1},
                        {"meterNumber": "G1", "premiseNumber": "P1", "meterType": 5},
                    ],
                },
            ),
            _FakeResponse(200, {"StatusCode": 200, "Result": {}}),
        ]
    )
    api = AlliantEnergyApi(other, session=session)
    api._access_token = "token"  # noqa: SLF001

    meters = await api.async_get_meters("acct", "P1")
    await api.async_get_usage(
        SERVICE_GAS, "acct", "P1", "G1", "09/01/2026", "10/15/2026", periodicity="MO"
    )

    assert [(m.meter_number, m.service_type) for m in meters] == [("G1", SERVICE_GAS)]
    meters_call, usage_call = session.calls
    assert (
        meters_call["url"] == "https://other-svc.example/Services/api/1/Usages/GetMeterAndPremise"
    )
    assert meters_call["headers"]["uid"] == "7"
    assert meters_call["headers"]["Origin"] == "https://portal.example"
    assert meters_call["headers"]["Referer"] == "https://portal.example/Portal/"
    assert usage_call["url"] == "https://other-svc.example/UsageAPI/api/V1/Gas"
    assert usage_call["headers"]["pt"] == "3"
    assert usage_call["params"]["Uom"] == "CCF"


async def test_login_rejects_error_status() -> None:
    """A non-200 envelope status raises an auth error."""
    api, _ = _api(_FakeResponse(200, {"status": {"code": 401, "message": "Invalid"}}))

    with pytest.raises(AlliantEnergyAuthError):
        await api.async_login("user", "bad")


async def test_get_accounts_parses_account_and_premise() -> None:
    """Account discovery returns account/premise pairs, skipping incomplete rows."""
    api, _ = _api(
        _FakeResponse(
            200,
            {
                "status": {"code": 200},
                "data": [
                    {"accountNumber": "1000000001", "premiseNumber": "2000000002"},
                    {"accountNumber": "", "premiseNumber": "x"},  # dropped
                ],
            },
        )
    )
    api._access_token = "token"  # noqa: SLF001
    api._user_uuid = "u-1"  # noqa: SLF001

    accounts = await api.async_get_accounts()

    assert len(accounts) == 1
    assert accounts[0].account_number == "1000000001"
    assert accounts[0].premise_number == "2000000002"


async def test_get_meters_classifies_service_types() -> None:
    """Meter type 1 maps to electric and type 2 to gas; others are dropped."""
    api, _ = _api(
        _FakeResponse(
            200,
            {
                "status": {"code": 200},
                "data": [
                    {"meterNumber": "E1", "premiseNumber": "P1", "meterType": 1},
                    {"meterNumber": "G1", "premiseNumber": "P1", "meterType": 2},
                    {"meterNumber": "W1", "premiseNumber": "P1", "meterType": 3},
                ],
            },
        )
    )
    api._access_token = "token"  # noqa: SLF001

    meters = await api.async_get_meters("acct", "P1")

    assert [m.service_type for m in meters] == [SERVICE_ELECTRIC, SERVICE_GAS]
    assert meters[0].meter_number == "E1"


@pytest.mark.parametrize("meter_type", ["1", None, 1.0, [1]])
async def test_get_meters_drops_a_meter_type_that_is_not_an_integer(meter_type) -> None:
    api, _ = _api(
        _FakeResponse(
            200,
            {"status": {"code": 200}, "data": [{"meterNumber": "X1", "meterType": meter_type}]},
        )
    )
    api._access_token = "token"  # noqa: SLF001

    assert await api.async_get_meters("acct", "P1") == []


async def test_get_usage_parses_readings_and_builds_account_param() -> None:
    """Usage maps consumption/amount and sends AccountNumber as premise-account."""
    api, session = _api(
        _FakeResponse(
            200,
            {
                "StatusCode": 200,
                "Result": {
                    "electricUsages": [
                        {
                            "consumption": 12.5,
                            "amount": 0,
                            "readDate": "2026-10-15T00:00:00+01:00",
                            "uom": "KWH",
                        }
                    ]
                },
            },
        )
    )
    api._access_token = "token"  # noqa: SLF001

    readings = await api.async_get_usage(
        SERVICE_ELECTRIC, "1000000001", "2000000002", "E1", "09/01/2026", "10/15/2026"
    )

    assert len(readings) == 1
    assert readings[0].usage == 12.5
    assert readings[0].cost == 0.0
    assert readings[0].unit == "KWH"
    call = session.calls[0]
    assert call["headers"]["pt"] == "1"  # persona-type header on usage calls
    assert call["params"]["AccountNumber"] == "2000000002-1000000001"
    assert call["params"]["Periodicity"] == "DA"


async def test_get_usage_handles_empty_gas_result() -> None:
    """A missing result array yields no readings rather than an error."""
    api, _ = _api(_FakeResponse(200, {"StatusCode": 200, "Result": {}}))
    api._access_token = "token"  # noqa: SLF001

    readings = await api.async_get_usage(
        SERVICE_GAS, "acct", "P1", "G1", "09/01/2026", "10/15/2026"
    )

    assert readings == []


async def test_login_detects_tfa_requirement() -> None:
    """tfa_required is set when the user has TFA on and details are present."""
    api, _ = _api(
        _FakeResponse(
            200,
            {
                "status": {"code": 200},
                "data": {
                    "accessToken": "t",
                    "user": {"uuid": "u-1", "tfa": True, "defaultPersona": 2},
                    "tfaDetails": {"phone": "***1234", "email": "u***@x.com"},
                },
            },
        )
    )

    await api.async_login("user", "pass")

    assert api.tfa_required is True
    assert api.tfa_details["email"] == "u***@x.com"


async def test_send_and_verify_tfa_carry_persona_header() -> None:
    """SendTfa/VerifyTfa post the uuid and the account's persona type header."""
    api, session = _api(
        _FakeResponse(200, {"status": {"code": 200}}),  # send
        _FakeResponse(200, {"status": {"code": 200}}),  # verify
    )
    api._access_token = "token"  # noqa: SLF001
    api._user_uuid = "u-1"  # noqa: SLF001
    api._tfa_persona = "2"  # noqa: SLF001

    await api.async_send_tfa(2)
    await api.async_verify_tfa("654321")

    send, verify = session.calls
    assert json.loads(send["data"]) == {
        "uuid": "u-1",
        "mode": 2,
        "CustomAttributes": {
            "deviceId": "",
            "deviceName": "",
            "client": "Web",
            "version": "1.0",
            "os": "",
            "ip": "#1",
        },
    }
    assert send["headers"]["pt"] == "2"
    assert json.loads(verify["data"])["code"] == "654321"
    assert verify["headers"]["pt"] == "2"


async def test_refresh_on_401() -> None:
    """A 401 triggers a token refresh and one retry with the new token."""
    api, session = _api(
        _FakeResponse(401, {}),  # first meters call: unauthorized
        _FakeResponse(200, {"status": {"code": 200}, "data": {"accessToken": "new-token"}}),
        _FakeResponse(200, {"status": {"code": 200}, "data": []}),  # retried call
    )
    api._access_token = "old-token"  # noqa: SLF001
    api._refresh_token = "refresh"  # noqa: SLF001

    meters = await api.async_get_meters("acct", "P1")

    assert meters == []
    assert session.calls[-1]["headers"]["Authorization"] == "Bearer new-token"


class _FakeTime:
    """A clock that only moves when something sleeps on it or a request runs."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _SlowSession(_FakeSession):
    """Each request takes ``latency`` seconds and yields while in flight."""

    def __init__(self, responses, time: _FakeTime, latency: float) -> None:
        super().__init__(responses)
        self.sent_at: list[float] = []
        self._time = time
        self._latency = latency

    async def request(self, method, url, **kwargs):
        self.sent_at.append(self._time.now)
        await asyncio.sleep(0)  # in flight: let other requests run
        self._time.now += self._latency
        if isinstance(failure := self._responses[0], Exception):
            self._responses.pop(0)
            raise failure
        return await super().request(method, url, **kwargs)


def _paced_api(
    *responses: _FakeResponse | Exception, latency: float = 0.8
) -> tuple[AlliantEnergyApi, _SlowSession, _FakeTime]:
    time = _FakeTime()
    session = _SlowSession(list(responses), time, latency)
    api = AlliantEnergyApi(
        ALLIANT_ENERGY, session=session, pause=0.5, sleep=time.sleep, clock=time.clock
    )
    api._access_token = "token"
    return api, session, time


async def test_every_request_waits_a_pause_after_the_previous_one_ended() -> None:
    """The bill PDF lookup and its download, on another host, are paced alike."""
    api, session, _time = _paced_api(
        _FakeResponse(200, {"data": {"billPdf": "https://ebill.example/bill.pdf"}}),
        _FakeResponse(200, "%PDF"),
    )

    await api.async_get_bill_pdf("acct", "INV1")

    assert session.sent_at[1] - session.sent_at[0] == pytest.approx(0.8 + 0.5)


async def test_concurrent_requests_are_sent_one_at_a_time() -> None:
    """The sensor refresh and the statistics import share one client."""
    api, session, _time = _paced_api(
        _FakeResponse(200, {"data": {}}), _FakeResponse(200, {"data": {}})
    )

    await asyncio.gather(api.async_get_current_bill("acct"), api.async_get_current_bill("acct"))

    assert session.sent_at[1] - session.sent_at[0] == pytest.approx(0.8 + 0.5)


async def test_pause_is_shortened_by_time_already_passed() -> None:
    """The first request goes at once; the next waits only what is left of the pause."""
    api, _session, time = _paced_api(
        _FakeResponse(200, {"data": {}}), _FakeResponse(200, {"data": {}}), latency=0
    )

    await api.async_get_current_bill("acct")
    time.now += 0.2  # the caller was busy for 0.2 s
    await api.async_get_current_bill("acct")

    assert time.sleeps == pytest.approx([0.3])


async def test_failed_request_still_counts_toward_the_pause() -> None:
    api, _session, time = _paced_api(
        RequestException("connection reset"), _FakeResponse(200, {"data": {}}), latency=0
    )

    with pytest.raises(AlliantEnergyError):
        await api.async_get_current_bill("acct")
    await api.async_get_current_bill("acct")

    assert time.sleeps == pytest.approx([0.5])


async def test_current_bill_is_a_typed_bill_summary() -> None:
    api, _session = _api(
        _FakeResponse(
            200,
            {
                "data": {
                    "totalAmountDue": "123.45",
                    "currentCharges": 120.0,
                    "pastDueAmount": "",
                    "previousBalance": "bad",
                    "netDueDate": "11-03-2026",
                    "invoiceDate": "not a date",
                    "invoiceId": "INV1",
                }
            },
        )
    )
    api._access_token = "token"

    summary = await api.async_get_current_bill("acct")

    assert summary == BillSummary(
        amount_due=123.45,
        current_charges=120.0,
        past_due_amount=None,
        previous_balance=None,
        due_date=date(2026, 11, 3),
        invoice_date=None,
        invoice_id="INV1",
    )


async def test_empty_current_bill_is_an_empty_summary() -> None:
    api, _session = _api(_FakeResponse(200, {"data": None}))
    api._access_token = "token"

    assert await api.async_get_current_bill("acct") == BillSummary()


async def test_close_closes_only_a_session_the_client_created(monkeypatch) -> None:
    closed: list[str] = []
    created: list[dict] = []

    class _OwnedSession:
        def __init__(self, **kwargs) -> None:
            created.append(kwargs)

        async def close(self) -> None:
            closed.append("owned")

    monkeypatch.setattr("custom_components.alliant_energy.api.AsyncSession", _OwnedSession)
    injected, _session = _api()

    await AlliantEnergyApi(ALLIANT_ENERGY).close()
    await injected.close()  # the fake session has no close(): it must not be called

    assert closed == ["owned"]
    assert created == [{"impersonate": "chrome"}]  # Cloudflare checks the TLS fingerprint


@pytest.mark.parametrize(
    "call",
    [
        lambda api: api.async_refresh_token(),
        lambda api: api.async_send_tfa(1),
        lambda api: api.async_verify_tfa("123456"),
        lambda api: api.async_get_accounts(),
        lambda api: api.async_get_meters("acct", "P1"),
    ],
    ids=["refresh", "send_tfa", "verify_tfa", "accounts", "authenticated_request"],
)
async def test_calls_need_a_login_first(call) -> None:
    """Nothing is sent without the token, refresh token, or uuid it needs."""
    api, session = _api()

    with pytest.raises(AlliantEnergyAuthError):
        await call(api)

    assert session.calls == []


async def test_login_without_an_access_token_is_an_auth_error() -> None:
    api, _ = _api(_FakeResponse(200, {"status": {"code": 200}, "data": {"user": {}}}))

    with pytest.raises(AlliantEnergyAuthError, match="access token"):
        await api.async_login("user", "pass")


async def test_bill_pdf_is_none_without_a_link() -> None:
    api, session = _api(_FakeResponse(200, {"data": {}}))
    api._access_token = "token"  # noqa: SLF001

    assert await api.async_get_bill_pdf("acct", "INV1") is None
    assert len(session.calls) == 1  # nothing to download


async def test_bill_pdf_download_failures_are_api_errors() -> None:
    link = _FakeResponse(200, {"data": {"billPdf": "https://ebill.example/bill.pdf"}})
    api, _session, _time = _paced_api(
        link, RequestException("reset"), link, _FakeResponse(404, "gone")
    )

    with pytest.raises(AlliantEnergyError, match="reset"):
        await api.async_get_bill_pdf("acct", "INV1")
    with pytest.raises(AlliantEnergyError, match="HTTP 404"):
        await api.async_get_bill_pdf("acct", "INV1")


async def test_get_usage_rejects_an_unknown_service() -> None:
    api, session = _api()

    with pytest.raises(AlliantEnergyError, match="water"):
        await api.async_get_usage("water", "acct", "P1", "W1", "09/01/2026", "10/15/2026")

    assert session.calls == []


@pytest.mark.parametrize(
    ("periodicity", "unit", "expected"),
    [("MO", None, "TH"), ("HH", None, "Therm"), ("DA", "CCF", "CCF")],
)
async def test_get_usage_picks_the_unit_code(periodicity, unit, expected) -> None:
    """Gas needs "Therm" for intervals but "TH" for billing cycles; a unit overrides."""
    api, session = _api(_FakeResponse(200, {"StatusCode": 200, "Result": {}}))
    api._access_token = "token"  # noqa: SLF001

    await api.async_get_usage(
        SERVICE_GAS, "acct", "P1", "G1", "09/01/2026", "10/15/2026", periodicity, unit
    )

    assert session.calls[0]["params"]["Uom"] == expected


async def test_http_error_is_an_api_error() -> None:
    api, _ = _api(_FakeResponse(500, {"error": "boom"}))
    api._access_token = "token"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError, match="HTTP 500"):
        await api.async_get_current_bill("acct")


async def test_invalid_json_is_an_api_error() -> None:
    response = _FakeResponse(200, {})
    response.text = "<html>maintenance</html>"
    api, _ = _api(response)
    api._access_token = "token"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError, match="invalid JSON"):
        await api.async_get_current_bill("acct")


async def test_non_auth_envelope_error_is_an_api_error() -> None:
    api, _ = _api(_FakeResponse(200, {"status": {"code": 500, "messageCode": "E500"}}))
    api._access_token = "token"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError, match="E500") as err:
        await api.async_get_current_bill("acct")

    assert not isinstance(err.value, AlliantEnergyAuthError)


# Every request exactly as the portal sends it (docs/backend-api.md). The
# backend is private and undocumented, so a changed field name, header, or path
# fails silently in production; these pin each one.
_ROOT = "https://alliant-svc.smartcmobile.com/"
_HEADERS = {
    "Accept": "application/json",
    "uid": "1",
    "Origin": "https://myaccount.alliantenergy.com",
    "Referer": "https://myaccount.alliantenergy.com/Portal/",
}
_DEVICE = {
    "deviceId": "",
    "deviceName": "",
    "client": "Web",
    "version": "1.0",
    "os": "",
    "ip": "#1",
}
_OK = {"status": {"code": 200}}


def _body(body: dict) -> str:
    return json.dumps(body, separators=(",", ":"))


def _signed(body: dict) -> dict[str, str]:
    signature = _sign(body)
    return {"x-signature": signature, "x-inputchecksum": signature}


def _post(path: str, body: dict, *, signed: bool = True, auth: bool = True, **headers) -> dict:
    return {
        "method": "POST",
        "url": _ROOT + path,
        "data": _body(body),
        "params": None,
        "headers": {
            **_HEADERS,
            "Content-Type": "application/json",
            **(_signed(body) if signed else {}),
            **headers,
            **({"Authorization": "Bearer token"} if auth else {}),
        },
    }


def _get(path: str, params: dict | None = None, **headers) -> dict:
    return {
        "method": "GET",
        "url": _ROOT + path,
        "data": None,
        "params": params,
        "headers": {**_HEADERS, **headers, "Authorization": "Bearer token"},
    }


_LOGIN_BODY = {
    "username": "user",
    "password": "pass",
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
_TOKENS = {**_OK, "data": {"accessToken": "token", "refreshToken": "r"}}

WIRE_CASES = [
    pytest.param(
        lambda api: api.async_login("user", "pass"),
        [_TOKENS],
        [_post("UsermanagementAPI/api/1/Login/auth", _LOGIN_BODY, auth=False)],
        id="login",
    ),
    pytest.param(
        lambda api: api.async_refresh_token(),
        [_TOKENS],
        [
            _post(
                "UsermanagementAPI/api/1/Login/auth/refresh",
                {"refreshToken": "refresh"},
                auth=False,
            )
        ],
        id="refresh",
    ),
    pytest.param(
        lambda api: api.async_send_tfa(2),
        [_OK],
        [
            _post(
                "UsermanagementAPI/api/1/users/SendTfa",
                {"uuid": "u-1", "mode": 2, "CustomAttributes": _DEVICE},
                signed=False,
                pt="1",
            )
        ],
        id="send_tfa",
    ),
    pytest.param(
        lambda api: api.async_verify_tfa("654321"),
        [_OK],
        [
            _post(
                "UsermanagementAPI/api/1/users/VerifyTfa",
                {"uuid": "u-1", "code": "654321", "CustomAttributes": _DEVICE},
                signed=False,
                pt="1",
            )
        ],
        id="verify_tfa",
    ),
    pytest.param(
        lambda api: api.async_get_accounts(),
        [{**_OK, "data": []}],
        [_get("Services/api/1/Addresses/User/u-1")],
        id="accounts",
    ),
    pytest.param(
        lambda api: api.async_get_meters("A1", "P1"),
        [{**_OK, "data": []}],
        [
            _post(
                "Services/api/1/Usages/GetMeterAndPremise",
                {"accountNumber": "A1", "premiseNumber": "P1"},
            )
        ],
        id="meters",
    ),
    pytest.param(
        lambda api: api.async_get_current_bill("A1"),
        [{**_OK, "data": {}}],
        [_post("Services/api/1/bill/Current", {"accountNumbers": ["A1"]})],
        id="current_bill",
    ),
    pytest.param(
        lambda api: api.async_get_bill_pdf("A1", "INV1"),
        [{**_OK, "data": {"billPdf": "https://ebill.example/bill.pdf"}}, "%PDF"],
        [
            _post(
                "Services/api/1/bill/GetBillPdf",
                {"accountNumber": "A1", "invoiceId": "INV1", "documentType": "BILL"},
            ),
            # The signed link is on a third-party host: no portal headers.
            {
                "method": "GET",
                "url": "https://ebill.example/bill.pdf",
                "data": None,
                "params": None,
                "headers": {},
            },
        ],
        id="bill_pdf",
    ),
    pytest.param(
        lambda api: api.async_get_usage(
            SERVICE_ELECTRIC, "A1", "P1", "E1", "09/01/2026", "10/15/2026", "HH"
        ),
        [{"StatusCode": 200, "Result": {}}],
        [
            _get(
                "UsageAPI/api/V1/Electric",
                {
                    "AccountNumber": "P1-A1",
                    "MeterNumber": "E1",
                    "From": "09/01/2026",
                    "To": "10/15/2026",
                    "Uom": "kWh",
                    "Periodicity": "HH",
                },
                pt="1",
            )
        ],
        id="usage",
    ),
]


@pytest.mark.parametrize(("call", "responses", "expected"), WIRE_CASES)
async def test_requests_match_the_portal_exactly(call, responses, expected) -> None:
    api, session = _api(*(_FakeResponse(200, payload) for payload in responses))
    api._access_token = "token"  # noqa: SLF001
    api._refresh_token = "refresh"  # noqa: SLF001
    api._user_uuid = "u-1"  # noqa: SLF001

    await call(api)

    assert session.calls == expected


async def test_a_401_refreshes_and_resends_the_same_request_once() -> None:
    api, session = _api(
        _FakeResponse(401, {}),
        _FakeResponse(200, {**_OK, "data": {"accessToken": "new"}}),
        _FakeResponse(401, {}),
    )
    api._access_token = "token"  # noqa: SLF001
    api._refresh_token = "refresh"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError, match="HTTP 401"):
        await api.async_get_meters("A1", "P1")

    first, _refresh, retried = session.calls
    assert retried == {
        **first,
        "headers": {**first["headers"], "Authorization": "Bearer new"},
    }


@pytest.mark.parametrize("status", [400, 404])
async def test_any_4xx_is_an_api_error(status) -> None:
    api, _ = _api(_FakeResponse(status, {}))
    api._access_token = "token"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError, match=f"POST {_ROOT}Services/api/1/bill/Current"):
        await api.async_get_current_bill("A1")


async def test_bill_pdf_link_that_fails_with_400_is_an_api_error() -> None:
    link = _FakeResponse(200, {"data": {"billPdf": "https://ebill.example/bill.pdf"}})
    api, _ = _api(link, _FakeResponse(400, "bad"))
    api._access_token = "token"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError, match="HTTP 400"):
        await api.async_get_bill_pdf("A1", "INV1")


async def test_bill_pdf_returns_the_downloaded_bytes() -> None:
    link = _FakeResponse(200, {"data": {"billPdf": "https://ebill.example/bill.pdf"}})
    pdf = _FakeResponse(200, {})
    pdf.content = b"%PDF-1.7"
    api, _ = _api(link, pdf)
    api._access_token = "token"  # noqa: SLF001

    assert await api.async_get_bill_pdf("A1", "INV1") == b"%PDF-1.7"


async def test_connection_error_names_the_request() -> None:
    api, _session, _time = _paced_api(RequestException("reset"))

    with pytest.raises(AlliantEnergyError, match=f"POST {_ROOT}Services/api/1/bill/Current"):
        await api.async_get_current_bill("A1")


async def test_an_empty_response_body_is_no_data() -> None:
    response = _FakeResponse(200, {})
    response.text = ""
    api, _ = _api(response)
    api._access_token = "token"  # noqa: SLF001
    api._user_uuid = "u-1"  # noqa: SLF001

    assert await api.async_send_tfa(1) == {}


@pytest.mark.parametrize(
    ("status", "message", "error"),
    [
        ({"code": 403, "message": "Locked"}, "Authentication failed (403): Locked", True),
        ({"code": 404, "message": "Missing"}, "API error (404): Missing", False),
        ({"code": 500}, "API error (500): request failed", False),
    ],
)
async def test_envelope_errors(status, message, error) -> None:
    api, _ = _api(_FakeResponse(200, {"status": status}))
    api._access_token = "token"  # noqa: SLF001

    with pytest.raises(AlliantEnergyError) as err:
        await api.async_get_current_bill("A1")

    assert str(err.value) == message
    assert isinstance(err.value, AlliantEnergyAuthError) is error


async def test_usage_reads_every_field_and_the_lowercase_result_key() -> None:
    record = {
        "consumption": 3.5,
        "amount": 41.0,
        "readDate": "2026-10-07T01:00:00",
        "readingFrom": "2026-09-01T00:00:00",
        "readingTo": "2026-10-01T00:00:00",
    }
    api, _ = _api(_FakeResponse(200, {"StatusCode": 200, "result": {"gasUsages": [record]}}))
    api._access_token = "token"  # noqa: SLF001

    readings = await api.async_get_usage(
        SERVICE_GAS, "A1", "P1", "G1", "09/01/2026", "10/15/2026", "MO"
    )

    assert readings == [
        UsageReading(
            usage=3.5,
            cost=41.0,
            reading_date="2026-10-07T01:00:00",
            reading_from="2026-09-01T00:00:00",
            reading_to="2026-10-01T00:00:00",
            unit="TH",  # no uom in the record: the unit requested
        )
    ]


async def test_meter_reads_every_field_and_defaults_to_the_requested_premise() -> None:
    raw = {"meterType": 1}
    api, _ = _api(_FakeResponse(200, {**_OK, "data": [raw]}))
    api._access_token = "token"  # noqa: SLF001

    assert await api.async_get_meters("A1", "P1") == [
        Meter(meter_number="", premise_number="P1", service_type=SERVICE_ELECTRIC, raw=raw)
    ]


async def test_account_keeps_its_raw_record() -> None:
    raw = {"accountNumber": "A1", "premiseNumber": "P1", "customerAttribute1": "RES"}
    api, _ = _api(_FakeResponse(200, {**_OK, "data": [raw]}))
    api._access_token = "token"  # noqa: SLF001
    api._user_uuid = "u-1"  # noqa: SLF001

    assert await api.async_get_accounts() == [Account("A1", "P1", raw)]


async def test_current_bill_reads_every_field() -> None:
    api, _ = _api(
        _FakeResponse(
            200,
            {
                "data": {
                    "totalAmountDue": 1.5,
                    "currentCharges": 2.5,
                    "pastDueAmount": 3.5,
                    "previousBalance": 4.5,
                    "netDueDate": "11-03-2026",
                    "invoiceDate": "10-13-2026",
                    "invoiceId": "INV1",
                }
            },
        )
    )
    api._access_token = "token"  # noqa: SLF001

    assert await api.async_get_current_bill("A1") == BillSummary(
        amount_due=1.5,
        current_charges=2.5,
        past_due_amount=3.5,
        previous_balance=4.5,
        due_date=date(2026, 11, 3),
        invoice_date=date(2026, 10, 13),
        invoice_id="INV1",
    )


async def test_bill_dates_that_are_not_strings_are_unknown() -> None:
    api, _ = _api(_FakeResponse(200, {"data": {"netDueDate": 20261103, "invoiceDate": None}}))
    api._access_token = "token"  # noqa: SLF001

    summary = await api.async_get_current_bill("A1")

    assert (summary.due_date, summary.invoice_date) == (None, None)


async def test_new_client_state_before_login() -> None:
    api, _ = _api()

    assert api.user_uuid is None
    assert api.tfa_required is False
    assert api.tfa_details == {}


async def test_refresh_keeps_what_the_response_leaves_out() -> None:
    """A refresh response without a refresh token, user, or persona keeps the old ones."""
    api, session = _api(
        _FakeResponse(
            200,
            {
                **_OK,
                "data": {
                    "accessToken": "a1",
                    "refreshToken": "r1",
                    "user": {"uuid": "u-1", "defaultPersona": 2},
                },
            },
        ),
        _FakeResponse(200, {**_OK, "data": {"accessToken": "a2"}}),
        _FakeResponse(200, {**_OK, "data": {"accessToken": "a3"}}),
        _FakeResponse(200, _OK),
    )
    await api.async_login("user", "pass")

    await api.async_refresh_token()
    await api.async_refresh_token()
    await api.async_send_tfa(1)

    assert json.loads(session.calls[2]["data"]) == {"refreshToken": "r1"}
    assert api.user_uuid == "u-1"
    assert session.calls[3]["headers"]["pt"] == "2"


async def test_tfa_without_delivery_details_is_not_required() -> None:
    api, session = _api(
        _FakeResponse(200, {**_OK, "data": {"accessToken": "t", "user": {"tfa": True}}}),
        _FakeResponse(200, _OK),
    )
    api._user_uuid = "u-1"  # noqa: SLF001

    await api.async_login("user", "pass")
    await api.async_send_tfa(1)

    assert api.tfa_required is False
    assert session.calls[1]["headers"]["pt"] == "1"  # the default persona
