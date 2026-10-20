"""Presentation-layer tests for Somfy entities against the minimal runtime protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import Mock

import pytest
from homeassistant.exceptions import HomeAssistantError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.somfy_ble.button import (
    BUTTON_DESCRIPTIONS,
    SomfyButton,
    SomfyInterrogateButton,
    _format_gatt_report,
)
from custom_components.somfy_ble.binary_sensor import SomfyBleReachable
from custom_components.somfy_ble.diagnostics import async_get_config_entry_diagnostics
from custom_components.somfy_ble.entity import (
    MAX_COMMAND_ID,
    RuntimeState,
    opaque_motor_id,
    safe_category,
)
from custom_components.somfy_ble.models import EnqueueResult, Result
from custom_components.somfy_ble.number import SomfyJogStep
from custom_components.somfy_ble.protocol import Command, JogDirection
from custom_components.somfy_ble.sensor import SENSOR_DESCRIPTIONS, SomfySensor


ADDRESS = "aa:bb:cc:dd:ee:ff"
PIN = "012345"
MOTOR_ID = "opaque-motor-7"
DISPLAY_NAME = "Living Room Blind"
CREDENTIAL_SHAPED_TITLES = (
    "privateKey-abcdef",
    "private_key-abcdef",
    "private-key-abcdef",
    "PRIVATE KEY-abcdef",
    "PSK-abcdef",
    "wifiPSK-abcdef",
    "wifi_psk-abcdef",
    "wifi-psk-abcdef",
    "wifi psk abcdef",
    "passphrase-abcdef",
    "pass_phrase-abcdef",
    "pass-phrase-abcdef",
    "pass phrase abcdef",
    "wifiPassword-abcdef",
    "wifi_password-abcdef",
    "wifi-password-abcdef",
    "WIFI PASSWORD abcdef",
    "otaPassword-abcdef",
    "ota_password-abcdef",
    "ota-password-abcdef",
    "OTA PASSWORD abcdef",
    "clientSecret-abcdef",
    "client_secret-abcdef",
    "client-secret-abcdef",
    "CLIENT SECRET abcdef",
    "refreshToken-abcdef",
    "refresh_token-abcdef",
    "refresh-token-abcdef",
    "REFRESH TOKEN abcdef",
)


def test_opaque_motor_id_hashes_all_identities_and_canonicalizes_addresses() -> None:
    address_forms = (
        "AA:BB:CC:DD:EE:FF",
        "aa-bb-cc-dd-ee-ff",
        "aabb.ccdd.eeff",
        "aa bb cc dd ee ff",
        "AABBCCDDEEFF",
    )
    ids = {opaque_motor_id(value) for value in address_forms}
    assert len(ids) == 1
    opaque = ids.pop()
    assert opaque.startswith("motor_")
    assert len(opaque) == len("motor_") + 16
    assert all(
        opaque_motor_id(value).startswith("motor_")
        for value in ("192.168.1.2", "012345", "token_abc123", "x\\x00y", "\\ud800")
    )
    assert all(value not in opaque_motor_id(value) for value in ("192.168.1.2", "012345", "token_abc123"))


@pytest.mark.parametrize("value", [None, 0, b"address", "", "   "])
def test_opaque_motor_id_rejects_invalid_identities(value: Any) -> None:
    with pytest.raises(ValueError):
        opaque_motor_id(value)


def test_device_info_uses_generic_name_for_unsafe_titles() -> None:
    unsafe_titles = (
        "192.168.1.2",
        "aa.bb.cc.dd.ee.ff",
        "012345",
        "1234",
        "12345678",
        "token_abc123",
        "sk" + "_live_abc123",
        "sk" + "_test_abc123",
        "pk" + "_live_abc123",
        "AuthToken-abc",
        "auth_token",
        "Bearer abc",
        "access-token-abc",
        "secret_token-abc",
        *CREDENTIAL_SHAPED_TITLES,
        ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjMifQ", "signature")),
        "unsafe" + chr(0) + "title",
        chr(0xD800),
    )
    for title in unsafe_titles:
        runtime = FakeRuntime()
        entities = [
            *(SomfyButton(runtime, MOTOR_ID, title, description) for description in BUTTON_DESCRIPTIONS),
            SomfyJogStep(runtime, MOTOR_ID, title, SomfyJogStep.description),
            SomfyBleReachable(runtime, MOTOR_ID, title, SomfyBleReachable.description),
            *(SomfySensor(runtime, MOTOR_ID, title, description) for description in SENSOR_DESCRIPTIONS),
        ]
        assert all(entity.device_info["name"] == "Somfy Blind" for entity in entities)
        assert all(title not in repr(entity.device_info) for entity in entities)


def test_device_info_preserves_bounded_printable_human_title() -> None:
    for title in ("Living Room Blind", "Floor 2 Sheer", "Café · Living Room", "A" * 64):
        entity = SomfyButton(FakeRuntime(), MOTOR_ID, title, BUTTON_DESCRIPTIONS[0])
        assert entity.device_info["name"] == title


def test_safe_category_uses_exact_semantic_allowlists() -> None:
    assert safe_category(Result.SUCCESS, semantic="result") == "success"
    assert safe_category("ready", semantic="status") == "ready"
    assert safe_category("auth_failed", semantic="error") == "auth_failed"
    assert safe_category("status_secret_1", semantic="status") == "unknown"
    assert safe_category("result_secret", semantic="result") == "unknown"
    assert safe_category("token_012345", semantic="error", fallback="error") == "error"


@pytest.mark.parametrize("value", ["yes", "false", 1, 0, None])
def test_boolean_runtime_values_fail_closed(value: Any) -> None:
    runtime = FakeRuntime(state=RuntimeState(reachable=value))
    reachable = SomfyBleReachable(runtime, MOTOR_ID, DISPLAY_NAME, SomfyBleReachable.description)
    assert reachable.is_on is False


@pytest.mark.parametrize(
    "value, expected",
    [
        (1, 1),
        (42, 42),
        (MAX_COMMAND_ID, MAX_COMMAND_ID),
        (0, None),
        (-1, None),
        (MAX_COMMAND_ID + 1, None),
        (True, None),
        (10**1000, None),
        ("42", None),
    ],
)
def test_last_command_id_requires_positive_non_boolean_integer(value: Any, expected: Any) -> None:
    runtime = FakeRuntime(state=RuntimeState(last_command_id=value))
    sensor = SomfySensor(runtime, MOTOR_ID, DISPLAY_NAME, next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "last_command_id"
    ))
    assert sensor.native_value == expected


@pytest.mark.parametrize("value, expected", [(0, 0), (3.5, 3.5), (-1, None), (math.inf, None), (10**1000, None)])
def test_last_contact_age_is_bounded_without_numeric_overflow(value: Any, expected: Any) -> None:
    runtime = FakeRuntime(state=RuntimeState(last_contact_age=value))
    sensor = SomfySensor(runtime, MOTOR_ID, DISPLAY_NAME, next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "last_contact_age"
    ))
    assert sensor.native_value == expected


@dataclass
class FakeRuntime:
    """Atomic, normally non-throwing synchronous runtime adapter for tests.

    ``subscribe`` publishes a callback before returning its idempotent cleanup;
    injected failures model provider faults without changing the normal contract.
    """

    jog_step: Any = 100
    requests: list[Any] = field(default_factory=list)
    state: RuntimeState = field(default_factory=RuntimeState)
    diagnostics: dict[str, Any] = field(default_factory=dict)
    subscribers: dict[str, list[Callable[[], None]]] = field(default_factory=dict)
    subscribe_error: BaseException | None = None
    unsubscribe_error: BaseException | None = None
    notify_during_subscribe: bool = False
    unsubscribe_calls: int = 0
    set_jog_step_calls: list[tuple[str, int]] = field(default_factory=list)

    def enqueue(self, request: Any) -> EnqueueResult:
        self.requests.append(request)
        return EnqueueResult(Result.SUCCESS, len(self.requests))


    def get_jog_step(self, motor_id: str) -> int:
        return self.jog_step

    def set_jog_step(self, motor_id: str, value: int) -> Result:
        self.set_jog_step_calls.append((motor_id, value))
        self.jog_step = value
        return Result.SUCCESS

    def snapshot(self, motor_id: str) -> RuntimeState:
        return self.state

    def diagnostics_snapshot(self, motor_id: str) -> dict[str, Any]:
        return self.diagnostics

    def subscribe(self, motor_id: str, callback: Callable[[], None]) -> Callable[[], None]:
        if self.subscribe_error is not None:
            raise self.subscribe_error
        callbacks = self.subscribers.setdefault(motor_id, [])
        callbacks.append(callback)
        if self.notify_during_subscribe:
            callback()

        def unsubscribe() -> None:
            if callback in callbacks:
                callbacks.remove(callback)
            self.unsubscribe_calls += 1
            if self.unsubscribe_error is not None:
                raise self.unsubscribe_error

        return unsubscribe

    def notify(self, motor_id: str) -> None:
        for callback in tuple(self.subscribers.get(motor_id, ())):
            callback()


def make_entity(entity_type: type, description: Any) -> Any:
    return entity_type(FakeRuntime(), MOTOR_ID, DISPLAY_NAME, description)


def test_entities_have_stable_opaque_ids_device_info_and_display_name() -> None:
    entities = [
        *(make_entity(SomfyButton, description) for description in BUTTON_DESCRIPTIONS),
        make_entity(SomfyJogStep, SomfyJogStep.description),
        make_entity(SomfyBleReachable, SomfyBleReachable.description),
        *(make_entity(SomfySensor, description) for description in SENSOR_DESCRIPTIONS),
    ]

    unique_ids = {entity.unique_id for entity in entities}
    assert len(unique_ids) == len(entities)
    assert all(entity.unique_id.startswith(f"{opaque_motor_id(MOTOR_ID)}_") for entity in entities)
    for entity in entities:
        assert entity.device_info["name"] == DISPLAY_NAME
        assert entity.device_info["identifiers"] == {("somfy_ble", opaque_motor_id(MOTOR_ID))}
        assert ADDRESS not in repr(entity.device_info)
        assert PIN not in repr(entity.device_info)


def test_entity_name_resolves_from_translation_key() -> None:
    entity = make_entity(SomfyButton, BUTTON_DESCRIPTIONS[0])
    translation_key = "component.somfy_ble.entity.button.identify.name"
    entity.platform_data = SimpleNamespace(
        platform_name="somfy_ble",
        domain="button",
        platform_translations={translation_key: "Identify"},
        component_translations={},
    )

    assert entity.name == "Identify"


@pytest.mark.asyncio
async def test_identify_and_stop_are_admitted() -> None:
    runtime = FakeRuntime()
    identify = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    stop = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[1])

    await identify.async_press()
    await stop.async_press()

    assert [request.command for request in runtime.requests] == [Command.IDENTIFY, Command.STOP]


@pytest.mark.asyncio
async def test_buttons_map_commands_and_jog_direction_through_admission_only() -> None:
    runtime = FakeRuntime()
    for description in BUTTON_DESCRIPTIONS:
        button = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, description)
        await button.async_press()

    assert [request.command for request in runtime.requests] == [
        Command.IDENTIFY,
        Command.STOP,
        Command.JOG,
        Command.JOG,
        Command.RANGE_START,
        Command.SET_DIRECTION_CLOCKWISE,
        Command.SET_DIRECTION_COUNTERCLOCKWISE,
        Command.SET_UPPER_LIMIT,
        Command.SET_LOWER_LIMIT,
    ]
    assert [request.direction for request in runtime.requests[2:4]] == [
        JogDirection.UP,
        JogDirection.DOWN,
    ]
    assert all(request.step == 100 for request in runtime.requests[2:4])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_type, operation, action, args, code",
    [
        (SomfyButton, "enqueue", "async_press", (), "error"),
        (SomfyButton, "get_jog_step", "async_press", (), "error"),
        (SomfyJogStep, "set_jog_step", "async_set_native_value", (25,), "invalid_payload"),
        (SomfyInterrogateButton, "async_interrogate", "async_press", (), "error"),
    ],
)
async def test_control_errors_do_not_expose_runtime_exception_details(
    entity_type, operation, action, args, code, monkeypatch
) -> None:
    runtime = FakeRuntime()

    def fail(*args):
        raise RuntimeError(f"{ADDRESS} {PIN}")

    monkeypatch.setattr(runtime, operation, fail, raising=False)
    if entity_type is SomfyButton:
        entity = entity_type(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[2])
    else:
        entity = entity_type(runtime, MOTOR_ID, DISPLAY_NAME)

    with pytest.raises(HomeAssistantError) as error:
        await getattr(entity, action)(*args)

    assert str(error.value) == code
    assert error.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [1, 500, 100.0])
async def test_jog_step_accepts_integer_values_inclusive(value: float) -> None:
    runtime = FakeRuntime()
    entity = SomfyJogStep(runtime, MOTOR_ID, DISPLAY_NAME, SomfyJogStep.description)

    await entity.async_set_native_value(value)

    assert runtime.jog_step == int(value)
    assert entity.native_value == int(value)


@pytest.mark.parametrize("runtime_value", [1, 500, 100, 100.0])
def test_jog_step_native_value_accepts_bounded_integral_runtime_values(runtime_value: Any) -> None:
    runtime = FakeRuntime(jog_step=runtime_value)
    entity = SomfyJogStep(runtime, MOTOR_ID, DISPLAY_NAME, SomfyJogStep.description)

    assert entity.native_value == int(runtime_value)


@pytest.mark.parametrize(
    "runtime_value",
    [0, 501, -1, 100.5, math.nan, math.inf, -math.inf, True, "100", None],
)
def test_jog_step_native_value_uses_safe_default_for_invalid_runtime_values(runtime_value: Any) -> None:
    runtime = FakeRuntime(jog_step=runtime_value)
    entity = SomfyJogStep(runtime, MOTOR_ID, DISPLAY_NAME, SomfyJogStep.description)

    assert entity.native_value == 100


@pytest.mark.asyncio
async def test_entities_subscribe_on_add_and_unsubscribe_on_remove(hass) -> None:
    runtime = FakeRuntime()
    entities = [
        *(SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, description) for description in BUTTON_DESCRIPTIONS),
        SomfyJogStep(runtime, MOTOR_ID, DISPLAY_NAME, SomfyJogStep.description),
        SomfyBleReachable(runtime, MOTOR_ID, DISPLAY_NAME, SomfyBleReachable.description),
        *(SomfySensor(runtime, MOTOR_ID, DISPLAY_NAME, description) for description in SENSOR_DESCRIPTIONS),
    ]

    for entity in entities:
        entity.hass = hass
        await entity.async_added_to_hass()

    assert len(runtime.subscribers[MOTOR_ID]) == len(entities)

    for entity in entities:
        entity._call_on_remove_callbacks()

    assert runtime.subscribers[MOTOR_ID] == []


@pytest.mark.asyncio
async def test_subscribe_failure_leaves_entity_inactive_without_listener(hass) -> None:
    runtime = FakeRuntime(subscribe_error=RuntimeError("provider unavailable"))
    entity = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    entity.hass = hass

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await entity.async_added_to_hass()

    assert entity._runtime_subscription_active is False
    assert runtime.subscribers.get(MOTOR_ID, []) == []


@pytest.mark.asyncio
async def test_notification_during_subscribe_is_ignored_until_subscription_is_active(hass, monkeypatch) -> None:
    runtime = FakeRuntime(notify_during_subscribe=True)
    entity = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    writes = Mock()
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", writes)

    await entity.async_added_to_hass()
    await hass.async_block_till_done()

    assert writes.call_count == 0
    entity._call_on_remove_callbacks()


@pytest.mark.asyncio
async def test_unsubscribe_failure_is_suppressed_and_entity_stays_inactive(hass, monkeypatch) -> None:
    runtime = FakeRuntime(unsubscribe_error=RuntimeError("late provider failure"))
    entity = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    writes = Mock()
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", writes)

    await entity.async_added_to_hass()
    runtime.notify(MOTOR_ID)
    entity._call_on_remove_callbacks()
    await hass.async_block_till_done()

    assert runtime.subscribers[MOTOR_ID] == []
    assert runtime.unsubscribe_calls == 1
    assert entity._runtime_subscription_active is False
    assert writes.call_count == 0

    entity._call_on_remove_callbacks()
    runtime.notify(MOTOR_ID)
    await hass.async_block_till_done()
    assert runtime.unsubscribe_calls == 1
    assert writes.call_count == 0


@pytest.mark.asyncio
async def test_remove_cleanup_is_idempotent_when_called_twice(hass) -> None:
    runtime = FakeRuntime()
    entity = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    entity.hass = hass

    await entity.async_added_to_hass()
    entity._call_on_remove_callbacks()
    entity._call_on_remove_callbacks()

    assert runtime.unsubscribe_calls == 1
    assert runtime.subscribers[MOTOR_ID] == []
    assert entity._runtime_subscription_active is False


@pytest.mark.asyncio
async def test_registration_failure_runs_cleanup_once_and_reraises(hass, monkeypatch) -> None:
    runtime = FakeRuntime()
    entity = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    registered: list[Callable[[], None]] = []
    entity.hass = hass

    def fail_registration(cleanup: Callable[[], None]) -> None:
        registered.append(cleanup)
        raise RuntimeError("entity removal unavailable")

    monkeypatch.setattr(entity, "async_on_remove", fail_registration)

    with pytest.raises(RuntimeError, match="entity removal unavailable"):
        await entity.async_added_to_hass()

    assert len(registered) == 1
    assert runtime.unsubscribe_calls == 1
    assert runtime.subscribers[MOTOR_ID] == []
    assert entity._runtime_subscription_active is False

    registered[0]()
    assert runtime.unsubscribe_calls == 1


@pytest.mark.asyncio
async def test_queued_notification_after_cleanup_does_not_write_state(hass, monkeypatch) -> None:
    runtime = FakeRuntime()
    entity = SomfyButton(runtime, MOTOR_ID, DISPLAY_NAME, BUTTON_DESCRIPTIONS[0])
    writes = Mock()
    entity.hass = hass
    monkeypatch.setattr(entity, "async_write_ha_state", writes)

    await entity.async_added_to_hass()
    runtime.notify(MOTOR_ID)
    entity._call_on_remove_callbacks()
    await hass.async_block_till_done()

    assert writes.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [0, 501, -1, 100.5, math.nan, math.inf, -math.inf, True, "100", 10**1000, -10**1000],
)
async def test_jog_step_rejects_invalid_values_without_calling_runtime(value: Any) -> None:
    runtime = FakeRuntime()
    entity = SomfyJogStep(runtime, MOTOR_ID, DISPLAY_NAME, SomfyJogStep.description)

    with pytest.raises(HomeAssistantError, match=Result.INVALID_PAYLOAD.value):
        await entity.async_set_native_value(value)
    assert runtime.jog_step == 100
    assert runtime.set_jog_step_calls == []


def test_reachability_and_diagnostic_sensor_values_are_redacted_categories() -> None:
    runtime = FakeRuntime(
        state=RuntimeState(
            reachable=True,
            last_result="success",
            status="ready",
            selected_source=f"proxy-{ADDRESS}",
            last_error=f"transport failed for {ADDRESS}",
            last_command_id=42,
            last_contact_age=3.5,
        )
    )
    reachable = SomfyBleReachable(runtime, MOTOR_ID, DISPLAY_NAME, SomfyBleReachable.description)
    assert reachable.is_on is True

    values = {
        description.key: SomfySensor(runtime, MOTOR_ID, DISPLAY_NAME, description).native_value
        for description in SENSOR_DESCRIPTIONS
    }
    assert values["selected_source"] == "other"
    assert values["last_error"] == "error"
    assert values["last_result"] == "success"
    assert values["last_command_id"] == 42
    assert values["last_contact_age"] == 3.5
    assert ADDRESS not in repr(values)


def test_last_error_sensor_reports_none_when_runtime_has_no_error() -> None:
    runtime = FakeRuntime(state=RuntimeState(last_error=None))
    description = next(
        item for item in SENSOR_DESCRIPTIONS if item.key == "last_error"
    )

    sensor = SomfySensor(runtime, MOTOR_ID, DISPLAY_NAME, description)

    assert sensor.native_value == "none"


@pytest.mark.asyncio
async def test_diagnostics_whitelist_excludes_entry_data_options_and_runtime_secrets() -> None:
    runtime = FakeRuntime(
        diagnostics={
            "reachable": True,
            "status": "ready",
            "queue_depth": 2,
            "selected_source": f"proxy-{ADDRESS}",
            "last_error": f"exception contains {ADDRESS} and {PIN}",
            "address": ADDRESS,
            "pin": PIN,
            "raw_scanner": {"address": ADDRESS},
        }
    )
    entry = MockConfigEntry(
        domain="somfy_ble",
        title=DISPLAY_NAME,
        data={"name": DISPLAY_NAME, "address": ADDRESS, "pin": PIN},
        options={"address": ADDRESS, "pin": PIN},
    )
    entry.runtime_data = type("RuntimeData", (), {"runtime": runtime, "motor_id": MOTOR_ID})()

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]
    rendered = repr(result)
    assert result["entry"] == {
        "entry_id": entry.entry_id,
        "title": DISPLAY_NAME,
        "address": ADDRESS,
    }
    assert result["runtime"]["queue_depth"] == 2
    assert result["runtime"]["selected_source"] == "other"
    assert PIN not in rendered
    assert "raw_scanner" not in rendered
    assert "options" not in rendered


@pytest.mark.asyncio
async def test_diagnostics_sanitizes_unsafe_entry_title() -> None:
    entry = MockConfigEntry(domain="somfy_ble", title="token_abc123")
    entry.runtime_data = type("RuntimeData", (), {"runtime": FakeRuntime(), "motor_id": MOTOR_ID})()

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]
    assert result["entry"]["title"] == "Somfy Blind"
    assert "token_abc123" not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "title",
    [
        "1234",
        "12345678",
        "sk" + "_live_abc123",
        "sk" + "_test_abc123",
        "pk" + "_live_abc123",
        "AuthToken-abc",
        ".".join(("eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjMifQ", "signature")),
    ],
)
async def test_diagnostics_never_exposes_credential_shaped_entry_title(title: str) -> None:
    entry = MockConfigEntry(domain="somfy_ble", title=title)
    entry.runtime_data = type("RuntimeData", (), {"runtime": FakeRuntime(), "motor_id": MOTOR_ID})()

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]

    assert result["entry"]["title"] == "Somfy Blind"
    assert title not in repr(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("title", CREDENTIAL_SHAPED_TITLES)
async def test_diagnostics_never_exposes_expanded_credential_title(title: str) -> None:
    entry = MockConfigEntry(domain="somfy_ble", title=title)
    entry.runtime_data = type("RuntimeData", (), {"runtime": FakeRuntime(), "motor_id": MOTOR_ID})()

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]

    assert result["entry"]["title"] == "Somfy Blind"
    assert title not in repr(result)


@pytest.mark.asyncio
async def test_diagnostics_runtime_values_fail_closed_without_overflow() -> None:
    runtime = FakeRuntime(
        diagnostics={
            "reachable": "yes",
            "queue_depth": 10**1000,
            "last_command_id": 0,
            "active_command_id": True,
            "last_contact_age": 10**1000,
            "status": "status_secret_1",
            "last_result": "result_secret",
            "last_error": "token_012345",
            "selected_source": "192.168.1.2",
        }
    )
    entry = MockConfigEntry(domain="somfy_ble", title=DISPLAY_NAME)
    entry.runtime_data = type("RuntimeData", (), {"runtime": runtime, "motor_id": MOTOR_ID})()

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]
    assert result["runtime"] == {
        "status": "unknown",
        "last_result": "unknown",
        "last_error": "error",
        "selected_source": "other",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ["last_command_id", "active_command_id"])
@pytest.mark.parametrize("value, expected", [(MAX_COMMAND_ID, MAX_COMMAND_ID), (MAX_COMMAND_ID + 1, None)])
async def test_diagnostics_command_ids_are_bounded(
    key: str, value: Any, expected: Any
) -> None:
    runtime = FakeRuntime(diagnostics={key: value})
    entry = MockConfigEntry(domain="somfy_ble", title=DISPLAY_NAME)
    entry.runtime_data = type("RuntimeData", (), {"runtime": runtime, "motor_id": MOTOR_ID})()

    result = await async_get_config_entry_diagnostics(None, entry)  # type: ignore[arg-type]

    if expected is None:
        assert key not in result["runtime"]
    else:
        assert result["runtime"][key] == expected


def test_translation_keys_cover_all_descriptions() -> None:
    root = Path(__file__).parents[1] / "custom_components/somfy_ble"
    translations = json.loads((root / "translations/en.json").read_text(encoding="utf-8"))
    entity = translations["entity"]
    assert {item.key for item in BUTTON_DESCRIPTIONS} <= set(entity["button"])
    assert "jog_step" in entity["number"]
    assert "ble_reachable" in entity["binary_sensor"]
    assert {item.key for item in SENSOR_DESCRIPTIONS} <= set(entity["sensor"])


def test_gatt_report_labels_known_characteristics_and_leaves_unknown_uuids() -> None:
    suffix = "-cad9-46c6-a2ea-2ca16d57b4a5"
    report = _format_gatt_report(
        (
            {
                "uuid": "00000000" + suffix,
                "characteristics": [
                    {"uuid": "0000000B" + suffix, "properties": ["write"], "descriptors": []},
                    {"uuid": "00020001" + suffix, "properties": ["write"], "descriptors": []},
                ],
            },
        )
    )
    assert f"- **PIN authentication** · `0000000B{suffix}` — **write**" in report
    assert f"- `00020001{suffix}` — **write**" in report
