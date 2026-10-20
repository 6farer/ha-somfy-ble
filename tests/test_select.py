"""Blind selector: options follow config entries; attributes map one motor's entities."""

from __future__ import annotations

import pytest
from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    mock_restore_cache,
)

from custom_components.somfy_ble.const import DOMAIN
from custom_components.somfy_ble.entity import opaque_motor_id
from custom_components.somfy_ble.select import SIGNAL_MOTORS_CHANGED, SomfySelectedBlind


SELECT = "select.somfy_ble_selected_blind"


def _motor(hass: HomeAssistant, title: str, entities: dict[str, str]) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, title=title, data={"name": title})
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    for key, entity_id in entities.items():
        domain, object_id = entity_id.split(".")
        registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{opaque_motor_id(entry.entry_id)}_{key}",
            config_entry=entry,
            suggested_object_id=object_id,
        )
    return entry


async def _add_select(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "select", {})
    component = hass.data["entity_components"]["select"]
    await component.async_add_entities([SomfySelectedBlind()])
    await hass.async_block_till_done()


async def test_options_and_entities_follow_config_entries(hass: HomeAssistant) -> None:
    _motor(hass, "Office Sheer", {"stop": "button.office_sheer_2", "status": "sensor.office_sheer_2"})
    _motor(hass, "Basement", {"stop": "button.basement_2", "set_direction_clockwise": "button.basement_6"})

    await _add_select(hass)

    state = hass.states.get(SELECT)
    assert state.attributes["options"] == ["Basement", "Office Sheer"]
    assert state.state == "Basement"
    assert state.attributes["entities"] == {
        "set_direction_clockwise": "button.basement_6",
        "stop": "button.basement_2",
    }

    await hass.services.async_call(
        "select", "select_option", {"entity_id": SELECT, "option": "Office Sheer"}, blocking=True
    )
    state = hass.states.get(SELECT)
    assert state.state == "Office Sheer"
    assert state.attributes["entities"] == {
        "status": "sensor.office_sheer_2",
        "stop": "button.office_sheer_2",
    }


async def test_new_motor_appears_and_unknown_option_is_rejected(hass: HomeAssistant) -> None:
    _motor(hass, "Basement", {"stop": "button.basement_2"})
    await _add_select(hass)

    _motor(hass, "Kitchen Left", {"stop": "button.kitchen_left_2"})
    async_dispatcher_send(hass, SIGNAL_MOTORS_CHANGED)
    await hass.async_block_till_done()
    assert hass.states.get(SELECT).attributes["options"] == ["Basement", "Kitchen Left"]

    with pytest.raises(Exception):
        await hass.services.async_call(
            "select", "select_option", {"entity_id": SELECT, "option": "Garage"}, blocking=True
        )
    assert hass.states.get(SELECT).state == "Basement"


async def test_selection_is_restored_after_restart(hass: HomeAssistant) -> None:
    _motor(hass, "Basement", {"stop": "button.basement_2"})
    _motor(hass, "Office Sheer", {"stop": "button.office_sheer_2"})
    mock_restore_cache(hass, [State(SELECT, "Office Sheer")])

    await _add_select(hass)

    state = hass.states.get(SELECT)
    assert state.state == "Office Sheer"
    assert state.attributes["entities"] == {"stop": "button.office_sheer_2"}


async def test_integration_setup_loads_selector_without_warnings(
    hass: HomeAssistant, caplog: pytest.LogCaptureFixture, enable_custom_integrations: None
) -> None:
    from custom_components.somfy_ble import async_setup

    _motor(hass, "Basement", {"stop": "button.basement_2"})
    # The test venv lacks bluetooth's USB stack; treat the integration as already set up.
    hass.config.components.update({"usb", "bluetooth", DOMAIN})
    assert await async_setup_component(hass, "select", {})
    assert await async_setup(hass, {})
    await hass.async_block_till_done()

    assert hass.states.get(SELECT).state == "Basement"
    assert not [
        r for r in caplog.records
        if r.levelname in ("WARNING", "ERROR") and "has not been tested" not in r.message
        and r.name != "aiohttp_fast_zlib"
    ]
