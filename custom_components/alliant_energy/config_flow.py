"""Config flow for Alliant Energy.

The happy path is username/password: the login token reads usage data even
though the account has MFA enabled, because Alliant enforces MFA only in its
web UI. A two-factor challenge step (``async_step_tfa``) is included as a
fallback and engages only if a post-login probe actually fails with an auth
error while the account reports TFA enabled. That fallback path is coded from
the portal's JavaScript and has not been exercised against a live challenge.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback

from .api import (
    TFA_MODE_EMAIL,
    TFA_MODE_TEXT,
    Account,
    AlliantEnergyApi,
    AlliantEnergyAuthError,
    AlliantEnergyError,
)
from .const import (
    CONF_ACCOUNT_ID,
    CONF_ELECTRICITY_RATE,
    CONF_GAS_RATE,
    CONF_PASSWORD,
    CONF_PREMISE_ID,
    CONF_USERNAME,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_USERNAME): str,
        vol.Required(CONF_PASSWORD): str,
    }
)


class AlliantEnergyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Alliant Energy."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        """Return the options flow for setting cost rates."""
        return AlliantEnergyOptionsFlow()

    def __init__(self) -> None:
        self._credentials: dict[str, str] = {}
        self._accounts: list[Account] = []
        self._api: AlliantEnergyApi | None = None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect credentials and authenticate."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._credentials = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            result = await self._async_login_and_route(errors)
            if result is not None:
                return result

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_SCHEMA, errors=errors
        )

    async def async_step_account(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose which account to add when there are several."""
        if user_input is not None:
            account = next(
                a for a in self._accounts if a.account_number == user_input[CONF_ACCOUNT_ID]
            )
            return await self._async_create(account)

        return self.async_show_form(
            step_id="account",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ACCOUNT_ID): vol.In(
                        {a.account_number: a.account_number for a in self._accounts}
                    )
                }
            ),
        )

    async def async_step_tfa(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Fallback two-factor code entry (only reached if the API gates on it)."""
        assert self._api is not None
        errors: dict[str, str] = {}
        if user_input is None:
            # Send a code to whichever channel the account advertises.
            mode = TFA_MODE_EMAIL if self._api.tfa_details.get("email") else TFA_MODE_TEXT
            try:
                await self._api.async_send_tfa(mode)
            except AlliantEnergyError:
                return await self._async_abort("cannot_connect")
        else:
            try:
                await self._api.async_verify_tfa(user_input["code"])
                self._accounts = await self._api.async_get_accounts()
            except AlliantEnergyAuthError:
                errors["base"] = "invalid_tfa"
            except AlliantEnergyError:
                return await self._async_abort("cannot_connect")
            else:
                return await self._async_finish_accounts()

        return self.async_show_form(
            step_id="tfa",
            data_schema=vol.Schema({vol.Required("code"): str}),
            errors=errors,
        )

    async def _async_login_and_route(
        self, errors: dict[str, str]
    ) -> ConfigFlowResult | None:
        """Log in, then route to account selection, TFA, or an error."""
        self._api = AlliantEnergyApi()
        try:
            await self._api.async_login(
                self._credentials[CONF_USERNAME], self._credentials[CONF_PASSWORD]
            )
            self._accounts = await self._api.async_get_accounts()
        except AlliantEnergyAuthError:
            # Credentials were accepted but data access was refused: if the
            # account has TFA on, offer the challenge; otherwise it's bad creds.
            if self._api.tfa_required:
                return await self.async_step_tfa()
            errors["base"] = "invalid_auth"
            await self._async_close()
            return None
        except AlliantEnergyError:
            errors["base"] = "cannot_connect"
            await self._async_close()
            return None
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating Alliant Energy credentials")
            errors["base"] = "unknown"
            await self._async_close()
            return None
        return await self._async_finish_accounts()

    async def _async_finish_accounts(self) -> ConfigFlowResult:
        """Create the entry, or ask which account when there are several."""
        if not self._accounts:
            return await self._async_abort("no_accounts")
        if len(self._accounts) == 1:
            return await self._async_create(self._accounts[0])
        return await self.async_step_account()

    async def _async_create(self, account: Account) -> ConfigFlowResult:
        """Create (or update, on reauth) the config entry for an account."""
        await self._async_close()
        await self.async_set_unique_id(account.account_number)
        data = {
            **self._credentials,
            CONF_ACCOUNT_ID: account.account_number,
            CONF_PREMISE_ID: account.premise_number,
        }
        if self.source == SOURCE_REAUTH:
            self._abort_if_unique_id_mismatch(reason="account_mismatch")
            return self.async_update_reload_and_abort(self._get_reauth_entry(), data=data)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"Alliant Energy ({account.account_number})", data=data
        )

    async def _async_abort(self, reason: str) -> ConfigFlowResult:
        await self._async_close()
        return self.async_abort(reason=reason)

    async def _async_close(self) -> None:
        if self._api is not None:
            await self._api.close()
            self._api = None

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when stored credentials stop working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect new credentials and re-run discovery."""
        errors: dict[str, str] = {}
        if user_input is not None:
            self._credentials = {
                CONF_USERNAME: user_input[CONF_USERNAME],
                CONF_PASSWORD: user_input[CONF_PASSWORD],
            }
            result = await self._async_login_and_route(errors)
            if result is not None:
                return result

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME,
                        default=self._get_reauth_entry().data.get(CONF_USERNAME),
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )


class AlliantEnergyOptionsFlow(OptionsFlow):
    """Options for cost calculation.

    Leave a rate blank to use the default: cost is derived from the bill's
    marginal rate (parsed from the PDF) or an effective rate per billing period;
    a value here overrides it with a fixed price.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the cost-rate options."""
        if user_input is not None:
            data: dict[str, float] = {}
            for key in (CONF_ELECTRICITY_RATE, CONF_GAS_RATE):
                value = user_input.get(key)
                if value is not None and value > 0:
                    data[key] = value
            return self.async_create_entry(title="", data=data)

        options = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ELECTRICITY_RATE,
                    description={"suggested_value": options.get(CONF_ELECTRICITY_RATE)},
                ): vol.Coerce(float),
                vol.Optional(
                    CONF_GAS_RATE,
                    description={"suggested_value": options.get(CONF_GAS_RATE)},
                ): vol.Coerce(float),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
