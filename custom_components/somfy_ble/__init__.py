"""Home Assistant lifecycle wiring for the shared Somfy Bluetooth runtime."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import TypeAlias

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, discovery
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.typing import ConfigType
from homeassistant.loader import async_get_integration

from .ble import BleTransport
from .config_file import WRITABLE_KEYS
from .const import CONF_ADDRESS, CONF_NAME, CONF_PIN, DOMAIN
from .coordinator import Coordinator, SharedRuntime
from .safety import Controller
from .select import SIGNAL_MOTORS_CHANGED


CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CARD_FILE = "somfy-blind-card.js"
CARD_URL = f"/{DOMAIN}/{CARD_FILE}"

PLATFORMS: tuple[Platform, ...] = (
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SENSOR,
)


@dataclass(slots=True)
class SomfyRuntimeData:
    """Volatile per-entry references into the domain-level execution runtime."""

    runtime: SharedRuntime
    controller: Controller
    motor_id: str
    transport: BleTransport
    coordinator: Coordinator


SomfyConfigEntry: TypeAlias = ConfigEntry[SomfyRuntimeData]


def _clear_runtime_data(entry: SomfyConfigEntry) -> None:
    """Remove volatile setup data without assuming a HA ConfigEntry shape."""
    try:
        delattr(entry, "runtime_data")
    except AttributeError:
        pass


READ_CONFIG_SCHEMA = vol.Schema({vol.Required("motor"): cv.string})
WRITE_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("motor"): cv.string,
        vol.Required("key"): vol.In(sorted(WRITABLE_KEYS)),
        vol.Required("value"): vol.Any(bool, int, str),
    }
)


def _motor_runtime(hass: HomeAssistant, name: str) -> tuple[SharedRuntime, str]:
    """Resolve a motor by its entry title or configured name."""
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if name in (entry.title, entry.data.get(CONF_NAME)):
            return entry.runtime_data.runtime, entry.runtime_data.motor_id
    raise ServiceValidationError(f"Unknown Somfy motor: {name}")


async def _async_register_card(hass: HomeAssistant) -> None:
    """Serve the blind card and load it on every dashboard, so it needs no manual resource."""
    # The frontend isn't loaded in some setups (and in tests); the card is optional there.
    if hass.http is None or "frontend" not in hass.config.components:
        return
    from homeassistant.components.frontend import add_extra_js_url
    from homeassistant.components.http import StaticPathConfig

    version = (await async_get_integration(hass, DOMAIN)).version
    await hass.http.async_register_static_paths(
        [StaticPathConfig(CARD_URL, str(Path(__file__).parent / "frontend" / CARD_FILE), True)]
    )
    add_extra_js_url(hass, f"{CARD_URL}?v={version}")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Load the one blind selector that spans every motor entry, and the blind card."""
    hass.async_create_task(
        discovery.async_load_platform(hass, Platform.SELECT, DOMAIN, {}, config)
    )
    await _async_register_card(hass)

    async def _read_config(call: ServiceCall) -> ServiceResponse:
        runtime, motor_id = _motor_runtime(hass, call.data["motor"])
        return await runtime.async_read_config(motor_id)

    async def _write_config(call: ServiceCall) -> ServiceResponse:
        runtime, motor_id = _motor_runtime(hass, call.data["motor"])
        value = call.data["value"]
        # UI text fields send numbers as strings.
        if isinstance(value, str) and re.fullmatch(r"-?\d+", value):
            value = int(value)
        return await runtime.async_write_config(motor_id, call.data["key"], value)

    hass.services.async_register(
        DOMAIN, "read_config", _read_config, READ_CONFIG_SCHEMA, SupportsResponse.ONLY
    )
    hass.services.async_register(
        DOMAIN, "write_config", _write_config, WRITE_CONFIG_SCHEMA, SupportsResponse.ONLY
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: SomfyConfigEntry) -> bool:
    """Register one motor, then forward all entity platforms."""
    runtime = hass.data.get(DOMAIN)
    created_runtime = runtime is None
    if runtime is None:
        runtime = SharedRuntime()
        hass.data[DOMAIN] = runtime
    if not isinstance(runtime, SharedRuntime):
        raise RuntimeError("invalid Somfy domain runtime")

    motor_id = entry.entry_id
    display_name = entry.data.get(CONF_NAME, entry.title)
    address = entry.data.get(CONF_ADDRESS)
    pin = entry.data.get(CONF_PIN)
    transport = BleTransport(
        hass,
        address or "",
        display_name=display_name,
        motor_id=motor_id,
    )
    try:
        await runtime.async_register_motor(
            motor_id, transport, address, pin, display_name
        )
    except Exception:
        if created_runtime:
            await runtime.async_shutdown()
            hass.data.pop(DOMAIN, None)
        raise

    entry.runtime_data = SomfyRuntimeData(
        runtime=runtime,
        controller=runtime.controller,
        motor_id=motor_id,
        transport=transport,
        coordinator=runtime.coordinator,
    )

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        try:
            await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
        except Exception:
            pass
        try:
            last_entry = await runtime.async_unregister_motor(motor_id)
            if last_entry and hass.data.get(DOMAIN) is runtime:
                hass.data.pop(DOMAIN, None)
        finally:
            _clear_runtime_data(entry)
        raise

    async def _async_update_listener(
        hass: HomeAssistant, updated_entry: SomfyConfigEntry
    ) -> None:
        """Schedule a serialized reload after config-entry data changes."""
        hass.config_entries.async_schedule_reload(updated_entry.entry_id)

    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    async_dispatcher_send(hass, SIGNAL_MOTORS_CHANGED)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: SomfyConfigEntry) -> bool:
    """Unload platforms before unregistering the motor/runtime."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False

    runtime = entry.runtime_data.runtime
    last_entry = await runtime.async_unregister_motor(entry.runtime_data.motor_id)
    if last_entry and hass.data.get(DOMAIN) is runtime:
        hass.data.pop(DOMAIN, None)
    async_dispatcher_send(hass, SIGNAL_MOTORS_CHANGED)
    return True
