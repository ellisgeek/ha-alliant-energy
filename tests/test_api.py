"""Tests for the low-level Alliant Energy API client.

These run fully offline against a fake curl_cffi-style session, so they
exercise signing, header construction, and response parsing without any
network.
"""

import hashlib
import json
from typing import Any

import pytest

from custom_components.alliant_energy.api import (
    SERVICE_ELECTRIC,
    SERVICE_GAS,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    _sign,
)


class _FakeResponse:
    def __init__(self, status: int, payload) -> None:
        self.status_code = status
        self.text = json.dumps(payload)


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
    return AlliantEnergyApi(session=session), session


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
