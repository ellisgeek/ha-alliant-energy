"""Tests for the config flow."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.alliant_energy.api import (
    Account,
    AlliantEnergyAuthError,
)
from custom_components.alliant_energy.const import DOMAIN

USER_INPUT = {"username": "user@example.com", "password": "hunter2"}


def _mock_api(
    *,
    login=None,
    accounts=None,
    tfa_required=False,
    verify=None,
    send=None,
):
    """Build a mock AlliantEnergyApi instance for the config flow."""
    api = MagicMock()
    api.async_login = AsyncMock(side_effect=login)
    api.async_get_accounts = AsyncMock(
        side_effect=accounts if callable(accounts) else None,
        return_value=accounts if not callable(accounts) else None,
    )
    api.async_send_tfa = AsyncMock(side_effect=send)
    api.async_verify_tfa = AsyncMock(side_effect=verify)
    api.close = AsyncMock()
    api.tfa_required = tfa_required
    api.tfa_details = {"email": "u***@example.com"}
    return api


def _patch(api):
    return patch(
        "custom_components.alliant_energy.config_flow.AlliantEnergyApi",
        return_value=api,
    )


async def test_single_account_creates_entry(hass, enable_custom_integrations) -> None:
    """One discovered account is added directly with its premise."""
    api = _mock_api(accounts=[Account("A1", "P1", {})])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch(api), patch(
        "custom_components.alliant_energy.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Alliant Energy (A1)"
    assert result["data"] == {
        "username": "user@example.com",
        "password": "hunter2",
        "account_id": "A1",
        "premise_id": "P1",
    }
    api.close.assert_awaited()


async def test_multiple_accounts_shows_picker(hass, enable_custom_integrations) -> None:
    """Several accounts prompt a selection step."""
    api = _mock_api(accounts=[Account("A1", "P1", {}), Account("A2", "P2", {})])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch(api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "account"

    with _patch(api), patch(
        "custom_components.alliant_energy.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"account_id": "A2"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["account_id"] == "A2"
    assert result["data"]["premise_id"] == "P2"


async def test_invalid_auth_without_tfa(hass, enable_custom_integrations) -> None:
    """A rejected login with no TFA surfaces invalid_auth."""
    api = _mock_api(login=AlliantEnergyAuthError("bad"), tfa_required=False)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch(api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_tfa_challenge_flow(hass, enable_custom_integrations) -> None:
    """If data access is refused but TFA is on, a code step completes setup."""
    # get_accounts fails until a code is verified, then returns an account.
    calls = {"n": 0}

    async def accounts_side_effect():
        calls["n"] += 1
        if calls["n"] == 1:
            raise AlliantEnergyAuthError("tfa required")
        return [Account("A1", "P1", {})]

    api = _mock_api(accounts=accounts_side_effect, tfa_required=True)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch(api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "tfa"
    api.async_send_tfa.assert_awaited_once()

    with _patch(api), patch(
        "custom_components.alliant_energy.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={"code": "123456"}
        )
        await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    api.async_verify_tfa.assert_awaited_once_with("123456")


async def test_rejects_duplicate_account(hass, enable_custom_integrations) -> None:
    """The same account cannot be configured twice."""
    MockConfigEntry(
        domain=DOMAIN, unique_id="A1", data={"account_id": "A1"}
    ).add_to_hass(hass)
    api = _mock_api(accounts=[Account("A1", "P1", {})])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_USER}
    )
    with _patch(api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_options_flow_stores_rates(hass, enable_custom_integrations) -> None:
    """The options flow keeps positive rates and drops blank/zero ones."""
    entry = MockConfigEntry(domain=DOMAIN, unique_id="A1", data={"account_id": "A1"})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"], user_input={"electricity_rate": 0.24, "gas_rate": 0}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    # Positive electricity rate kept; zero gas rate dropped.
    assert result["data"] == {"electricity_rate": 0.24}


async def test_reauth_updates_credentials(hass, enable_custom_integrations) -> None:
    """Reauth stores the new password on the existing entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="A1",
        data={**USER_INPUT, "account_id": "A1", "premise_id": "P1"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    api = _mock_api(accounts=[Account("A1", "P1", {})])
    with _patch(api), patch(
        "custom_components.alliant_energy.async_setup_entry", return_value=True
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input={**USER_INPUT, "password": "new-pass"}
        )
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new-pass"


async def test_reauth_rejects_other_account(hass, enable_custom_integrations) -> None:
    """Reauth with a login that lacks the entry's account aborts."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="A1",
        data={**USER_INPUT, "account_id": "A1", "premise_id": "P1"},
    )
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)

    api = _mock_api(accounts=[Account("B2", "P9", {})])
    with _patch(api):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], user_input=USER_INPUT
        )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "account_mismatch"
