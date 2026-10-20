"""Config-flow and lifecycle tests for the Somfy Bluetooth scaffold."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_NAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.somfy_ble import DOMAIN
from custom_components.somfy_ble.config_flow import _validate_pin
from custom_components.somfy_ble.const import CONF_ADDRESS, CONF_PIN
from custom_components.somfy_ble.models import CommandRequest, Result
from custom_components.somfy_ble.protocol import Command

pytestmark = pytest.mark.usefixtures("enable_custom_integrations")


@pytest.fixture(autouse=True)
def bypass_external_bluetooth_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep config-flow tests independent from the host Bluetooth adapter."""
    process_dependencies = AsyncMock()
    monkeypatch.setattr(
        "homeassistant.config_entries.async_process_deps_reqs", process_dependencies
    )
    monkeypatch.setattr(
        "homeassistant.setup.async_process_deps_reqs", process_dependencies
    )


async def _configure_user_flow(hass: HomeAssistant, user_input: dict[str, str]) -> dict:
    """Complete the user config flow and return its result."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "user"},
    )
    assert result["type"] is FlowResultType.FORM
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        user_input=user_input,
    )


def _assert_secret_equal(actual: object, expected: str) -> None:
    """Check secret storage without placing either value in assertion output."""
    if actual != expected:
        raise AssertionError("PIN storage mismatch")


def _assert_not_exposed(secret: str, *values: object) -> None:
    """Check redaction without echoing the sensitive value on failure."""
    if any(secret in str(value) for value in values):
        raise AssertionError("sensitive value was exposed")


def _entry_data(name: str, *, address: str | None = None, pin: str | None = None) -> dict[str, str]:
    data = {CONF_NAME: name}
    if address is not None:
        data[CONF_ADDRESS] = address
    if pin is not None:
        data[CONF_PIN] = pin
    return data


@pytest.mark.asyncio
async def test_address_and_pin_are_saved_with_canonical_identity(
    hass: HomeAssistant, somfy_pin: str
) -> None:
    result = await _configure_user_flow(
        hass,
        _entry_data(
            "Living Room",
            address="AA-BB-CC-DD-EE-FF",
            pin=somfy_pin,
        ),
    )

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Living Room"
    assert result["data"][CONF_NAME] == "Living Room"
    assert result["data"][CONF_ADDRESS] == "aa:bb:cc:dd:ee:ff"
    _assert_secret_equal(result["data"].get(CONF_PIN), somfy_pin)
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.title == "Living Room"
    assert entry.unique_id == "address:aa:bb:cc:dd:ee:ff"
    _assert_secret_equal(entry.data.get(CONF_PIN), somfy_pin)
    _assert_not_exposed(somfy_pin, repr(entry), entry.title, entry.unique_id)


def test_protocol_supported_hex_pin_is_accepted_without_transformation() -> None:
    hex_pin = "0xA1B2C3"

    _assert_secret_equal(_validate_pin(hex_pin), hex_pin)


def test_protocol_supported_variable_length_decimal_pin_is_accepted_without_transformation() -> None:
    decimal_pin = "12345"

    _assert_secret_equal(_validate_pin(decimal_pin), decimal_pin)


@pytest.mark.asyncio
async def test_name_only_entry_preserves_display_name_and_uses_normalized_identity(
    hass: HomeAssistant,
) -> None:
    result = await _configure_user_flow(hass, _entry_data("  Kitchen  "))

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Kitchen"
    assert result["data"] == {CONF_NAME: "Kitchen"}
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.title == "Kitchen"
    assert entry.unique_id == "name:kitchen"
    assert CONF_PIN not in entry.data


@pytest.mark.asyncio
async def test_malformed_unicode_name_is_rejected_without_persistence_or_echo(
    hass: HomeAssistant,
) -> None:
    malformed_name = "Living\ud800 Room"

    result = await _configure_user_flow(hass, _entry_data(malformed_name))
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {CONF_NAME: "invalid_name"}
    assert not hass.config_entries.async_entries(DOMAIN)
    _assert_not_exposed(malformed_name, result, result.get("errors"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    (
        (CONF_ADDRESS, "AA:BB:CC:DD:EE"),
        (CONF_ADDRESS, "AA:BB:CC:DD:EE:GG"),
        (CONF_PIN, "9" * 6 + "X"),
        (CONF_PIN, "16777216"),
    ),
)
async def test_malformed_optional_identity_values_are_rejected_without_echo(
    hass: HomeAssistant,
    somfy_pin: str,
    field: str,
    value: str,
) -> None:
    user_input = _entry_data("Study", address="11:22:33:44:55:66", pin=somfy_pin)
    user_input[field] = value

    result = await _configure_user_flow(hass, user_input)

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: "invalid_value"}
    _assert_not_exposed(value, result, result.get("errors"))


@pytest.mark.asyncio
async def test_duplicate_address_is_aborted_after_formatting_and_case_normalization(
    hass: HomeAssistant, somfy_pin: str
) -> None:
    first = await _configure_user_flow(
        hass,
        _entry_data("First Blind", address="AA:BB:CC:DD:EE:FF", pin=somfy_pin),
    )
    assert first["type"] is FlowResultType.CREATE_ENTRY

    duplicate = await _configure_user_flow(
        hass,
        _entry_data("Second Blind", address="aabbccddeeff"),
    )

    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.asyncio
async def test_duplicate_name_only_entry_is_aborted_by_normalized_name(
    hass: HomeAssistant,
) -> None:
    first = await _configure_user_flow(hass, _entry_data("Reading Room"))
    assert first["type"] is FlowResultType.CREATE_ENTRY

    duplicate = await _configure_user_flow(hass, _entry_data(" reading   room "))

    assert duplicate["type"] is FlowResultType.ABORT
    assert duplicate["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


@pytest.mark.asyncio
async def test_address_is_primary_identity_so_distinct_addresses_may_share_display_name(
    hass: HomeAssistant,
) -> None:
    first = await _configure_user_flow(
        hass,
        _entry_data("Office", address="10:20:30:40:50:60"),
    )
    second = await _configure_user_flow(
        hass,
        _entry_data(" office ", address="10:20:30:40:50:61"),
    )

    assert first["type"] is FlowResultType.CREATE_ENTRY
    assert second["type"] is FlowResultType.CREATE_ENTRY
    assert [entry.unique_id for entry in hass.config_entries.async_entries(DOMAIN)] == [
        "address:10:20:30:40:50:60",
        "address:10:20:30:40:50:61",
    ]


@pytest.mark.asyncio
async def test_pin_is_not_exposed_by_strings_or_identity_surfaces(
    hass: HomeAssistant, somfy_pin: str
) -> None:
    result = await _configure_user_flow(
        hass,
        _entry_data("Bedroom", address="01:23:45:67:89:ab", pin=somfy_pin),
    )
    entry = hass.config_entries.async_entries(DOMAIN)[0]
    resource_text = (
        Path(__file__).parents[1] / "custom_components/somfy_ble/translations/en.json"
    ).read_text(encoding="utf-8")

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_PIN] == somfy_pin
    assert somfy_pin not in entry.title
    assert somfy_pin not in (entry.unique_id or "")
    assert somfy_pin not in repr(entry)
    assert somfy_pin not in resource_text


@pytest.mark.asyncio
async def test_reconfigure_updates_bare_hex_pin_and_reloads_entry(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office Sheer",
        data=_entry_data(
            "Office Sheer", address="aa:bb:cc:dd:ee:ff", pin="012345"
        ),
        unique_id="address:aa:bb:cc:dd:ee:ff",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    defaults = result["data_schema"]({})
    assert defaults[CONF_NAME] == "Office Sheer"
    assert defaults[CONF_ADDRESS] == "aa:bb:cc:dd:ee:ff"
    assert defaults[CONF_PIN] == ""

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Office Sheer",
            CONF_ADDRESS: "aa:bb:cc:dd:ee:ff",
            CONF_PIN: "FE8D0B",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_PIN] == "FE8D0B"
    await hass.async_block_till_done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "address, expected_address, expected_id",
    [
        ("AA-BB-CC-DD-EE-01", "aa:bb:cc:dd:ee:01", "address:aa:bb:cc:dd:ee:01"),
        ("", None, "name:office blackout updated"),
    ],
)
async def test_reconfigure_updates_identity_and_preserves_pin_when_blank(
    hass: HomeAssistant, address: str, expected_address: str | None, expected_id: str,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Office Blackout",
        data=_entry_data(
            "Office Blackout", address="aa:bb:cc:dd:ee:ff", pin="012345"
        ),
        unique_id="address:aa:bb:cc:dd:ee:ff",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": "reconfigure", "entry_id": entry.entry_id},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Office Blackout Updated",
            CONF_ADDRESS: address,
            CONF_PIN: "",
        },
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.title == "Office Blackout Updated"
    assert entry.unique_id == expected_id
    assert entry.data == _entry_data(
        "Office Blackout Updated",
        address=expected_address,
        pin="012345",
    )
    await hass.async_block_till_done()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field, value, error",
    [
        (CONF_NAME, "", "invalid_value"),
        (CONF_NAME, "Living\ud800 Room", "invalid_name"),
        (CONF_ADDRESS, "invalid-address", "invalid_value"),
        (CONF_PIN, "not-a-pin", "invalid_value"),
    ],
)
async def test_reconfigure_rejects_invalid_fields_without_changing_entry(
    hass: HomeAssistant, field: str, value: str, error: str,
) -> None:
    original = _entry_data("Office", address="aa:bb:cc:dd:ee:ff", pin="012345")
    entry = MockConfigEntry(domain=DOMAIN, title="Office", data=original)
    entry.add_to_hass(hass)
    flow = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": "reconfigure", "entry_id": entry.entry_id}
    )

    result = await hass.config_entries.flow.async_configure(
        flow["flow_id"], {**original, field: value}
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {field: error}
    assert entry.data == original


@pytest.mark.asyncio
async def test_setup_update_reload_unload_and_remove_clear_controller_ownership(
    hass: HomeAssistant,
) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Hallway",
        data=_entry_data("Hallway", address="de:ad:be:ef:00:01"),
        unique_id="address:de:ad:be:ef:00:01",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    first_runtime = entry.runtime_data
    assert first_runtime.controller.is_configured(first_runtime.motor_id)
    assert len(entry.update_listeners) == 1

    hass.config_entries.async_update_entry(entry, title="Hallway Updated")
    await hass.async_block_till_done()

    second_runtime = entry.runtime_data
    assert second_runtime is not first_runtime
    assert not first_runtime.controller.is_configured(first_runtime.motor_id)
    assert (
        first_runtime.controller.enqueue(
            CommandRequest(first_runtime.motor_id, Command.IDENTIFY)
        ).result
        is Result.NOT_FOUND
    )
    assert first_runtime.controller.pending_count() == 0
    assert not first_runtime.controller.has_active()
    assert entry.state is ConfigEntryState.LOADED
    assert entry.title == "Hallway Updated"
    assert len(entry.update_listeners) == 1

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert not second_runtime.controller.is_configured(second_runtime.motor_id)
    assert (
        second_runtime.controller.enqueue(
            CommandRequest(second_runtime.motor_id, Command.IDENTIFY)
        ).result
        is Result.NOT_FOUND
    )
    assert second_runtime.controller.pending_count() == 0
    assert not second_runtime.controller.has_active()
    assert not entry.update_listeners

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    third_runtime = entry.runtime_data
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert not third_runtime.controller.is_configured(third_runtime.motor_id)
    assert (
        third_runtime.controller.enqueue(
            CommandRequest(third_runtime.motor_id, Command.IDENTIFY)
        ).result
        is Result.NOT_FOUND
    )
    assert third_runtime.controller.pending_count() == 0
    assert not third_runtime.controller.has_active()
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not hasattr(entry, "runtime_data")


@pytest.mark.asyncio
async def test_multiple_entries_share_domain_runtime_and_final_removal_shuts_it_down(
    hass: HomeAssistant,
) -> None:
    first = MockConfigEntry(
        domain=DOMAIN,
        title="Living Room",
        data=_entry_data("Living Room", address="de:ad:be:ef:00:01", pin="012345"),
        unique_id="address:de:ad:be:ef:00:01",
    )
    second = MockConfigEntry(
        domain=DOMAIN,
        title="Bedroom",
        data=_entry_data("Bedroom", address="de:ad:be:ef:00:02", pin="012346"),
        unique_id="address:de:ad:be:ef:00:02",
    )
    first.add_to_hass(hass)
    second.add_to_hass(hass)

    assert await hass.config_entries.async_setup(first.entry_id)
    await hass.async_block_till_done()

    first_runtime = first.runtime_data
    second_runtime = second.runtime_data
    assert first_runtime.runtime is second_runtime.runtime
    assert first_runtime.controller is second_runtime.controller
    assert first_runtime.coordinator is second_runtime.coordinator
    assert first_runtime.motor_id != second_runtime.motor_id
    assert first_runtime.controller.is_configured(first_runtime.motor_id)
    assert first_runtime.controller.is_configured(second_runtime.motor_id)

    await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN in hass.data
    assert second_runtime.controller.is_configured(second_runtime.motor_id)
    assert not second_runtime.controller.is_configured(first_runtime.motor_id)

    await hass.config_entries.async_remove(second.entry_id)
    await hass.async_block_till_done()
    assert DOMAIN not in hass.data
