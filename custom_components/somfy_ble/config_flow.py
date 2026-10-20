"""Config flow for adding a Somfy Bluetooth motor."""

from __future__ import annotations

import re
from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_NAME
from homeassistant.helpers import selector

from .const import CONF_ADDRESS, CONF_PIN, DOMAIN, SOMFY_MANUFACTURER_ID
from .protocol import encode_pin

_ADDRESS_RE = re.compile(r"^[0-9a-fA-F]{12}$|^(?:[0-9a-fA-F]{2})(?P<separator>[:-])(?:[0-9a-fA-F]{2}(?P=separator)){4}[0-9a-fA-F]{2}$")


def _normalize_address(address: str) -> str:
    """Validate and return a lowercase, colon-separated Bluetooth address."""
    if not isinstance(address, str):
        raise ValueError("invalid Bluetooth address")
    value = address.strip()
    if not _ADDRESS_RE.fullmatch(value):
        raise ValueError("invalid Bluetooth address")
    compact = value.replace(":", "").replace("-", "").lower()
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def _normalize_name(name: str) -> str:
    """Return the stable comparison identity for a motor name."""
    if not isinstance(name, str):
        raise ValueError("invalid motor name")
    display_name = " ".join(name.split())
    if not display_name:
        raise ValueError("invalid motor name")
    return display_name.casefold()


def _display_name(name: str) -> str:
    """Validate and preserve human-readable name casing for the entry title."""
    if not isinstance(name, str):
        raise ValueError("invalid motor name")
    display_name = name.strip()
    if not display_name:
        raise ValueError("invalid motor name")
    display_name.encode("utf-8")
    return display_name


def _validate_pin(pin: str) -> str:
    """Validate a protocol-supported decimal or hexadecimal Somfy PIN."""
    try:
        encode_pin(pin)
    except ValueError as error:
        raise ValueError("invalid PIN") from error
    return pin


def _identity(*, address: str | None, name: str) -> str:
    """Build a secret-free unique identity for a config entry."""
    if address is not None:
        return f"address:{address}"
    return f"name:{_normalize_name(name)}"


def _validate_input(user_input: dict[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    """Use the same field validation for setup and reconfiguration."""
    try:
        data = {CONF_NAME: _display_name(user_input[CONF_NAME])}
    except UnicodeEncodeError:
        return {}, {CONF_NAME: "invalid_name"}
    except (KeyError, ValueError):
        return {}, {CONF_NAME: "invalid_value"}

    errors: dict[str, str] = {}
    for key, validate in ((CONF_ADDRESS, _normalize_address), (CONF_PIN, _validate_pin)):
        value = user_input.get(key)
        if value not in (None, ""):
            try:
                data[key] = validate(value)
            except ValueError:
                errors[key] = "invalid_value"
    return data, errors


class SomfyBleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle user setup for one Somfy Bluetooth motor."""

    VERSION = 1

    _discovered_address: str

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Collect and validate the motor identity and optional PIN."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate_input(user_input)
            if not errors:
                name = data[CONF_NAME]
                address = data.get(CONF_ADDRESS)
                await self.async_set_unique_id(_identity(address=address, name=name))
                if self._identity_is_configured(address=address, name=name):
                    return self.async_abort(reason="already_configured")
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title=name, data=data)

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_ADDRESS, default=""): self._address_selector(),
                vol.Optional(CONF_PIN, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Offer a motor found by Home Assistant Bluetooth discovery."""
        address = _normalize_address(discovery_info.address)
        title = f"Somfy {address[-5:].upper()}"
        await self.async_set_unique_id(_identity(address=address, name=title))
        if self._identity_is_configured(address=address, name=title):
            return self.async_abort(reason="already_configured")
        self._abort_if_unique_id_configured()
        self._discovered_address = address
        self.context["title_placeholders"] = {"name": title}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask only for the motor name and printed PIN of a discovered motor."""
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate_input(
                {**user_input, CONF_ADDRESS: self._discovered_address}
            )
            if not errors:
                return self.async_create_entry(title=data[CONF_NAME], data=data)

        default_name = self.context["title_placeholders"]["name"]
        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=default_name): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_PIN, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="bluetooth_confirm", data_schema=schema, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Update the visible motor identity and optional printed PIN."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = _validate_input(user_input)
            if not errors:
                name = data[CONF_NAME]
                address = data.get(CONF_ADDRESS)
                if self._identity_is_configured(
                    address=address, name=name, exclude_entry_id=entry.entry_id
                ):
                    errors[CONF_ADDRESS if address is not None else CONF_NAME] = (
                        "already_configured"
                    )
                else:
                    updated_data = {**entry.data, **data}
                    if address is None:
                        updated_data.pop(CONF_ADDRESS, None)
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=_identity(address=address, name=name),
                        title=name,
                        data=updated_data,
                    )

        current_name = entry.data.get(CONF_NAME, entry.title)
        current_address = entry.data.get(CONF_ADDRESS, "")
        shown_name = (
            user_input.get(CONF_NAME, current_name) if user_input is not None else current_name
        )
        shown_address = (
            user_input.get(CONF_ADDRESS, current_address)
            if user_input is not None
            else current_address
        )

        schema = vol.Schema(
            {
                vol.Required(CONF_NAME, default=shown_name): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_ADDRESS, default=shown_address): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
                ),
                vol.Optional(CONF_PIN, default=""): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
            }
        )
        return self.async_show_form(
            step_id="reconfigure", data_schema=schema, errors=errors
        )

    def _address_selector(self) -> selector.Selector:
        """Offer discovered, unconfigured motors while still allowing typed input."""
        options = []
        for info in async_discovered_service_info(self.hass, connectable=True):
            if SOMFY_MANUFACTURER_ID not in info.manufacturer_data or not (
                info.name or ""
            ).startswith("Sonesse"):
                continue
            try:
                address = _normalize_address(info.address)
            except ValueError:
                continue
            name = (info.name or "").strip() or "Sonesse"
            if self._identity_is_configured(address=address, name=name):
                continue
            label = f"{name} ({address.upper()})"
            options.append(selector.SelectOptionDict(value=address, label=label))
        if not options:
            return selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT)
            )
        return selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=options,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        )

    @property
    def _current_entries(self):
        return self.hass.config_entries.async_entries(DOMAIN)

    def _identity_is_configured(
        self,
        *,
        address: str | None,
        name: str,
        exclude_entry_id: str | None = None,
    ) -> bool:
        """Check canonical address or name-only identities without exposing secrets."""
        normalized_name = _normalize_name(name)
        for entry in self._current_entries:
            if entry.entry_id == exclude_entry_id:
                continue
            existing_address = entry.data.get(CONF_ADDRESS)
            if address is not None and existing_address:
                try:
                    if _normalize_address(existing_address) == address:
                        return True
                except ValueError:
                    continue
            elif address is None and not existing_address:
                existing_name = entry.data.get(CONF_NAME, entry.title)
                try:
                    if _normalize_name(existing_name) == normalized_name:
                        return True
                except ValueError:
                    continue
        return False
