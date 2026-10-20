"""Somfy command buttons."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components import persistent_notification
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SomfyEntity, require_success, runtime_context, runtime_errors
from .models import CommandRequest, Result
from .protocol import Characteristic, Command, JogDirection


BUTTON_DESCRIPTIONS: Final = (
    ButtonEntityDescription(key="identify", translation_key="identify"),
    ButtonEntityDescription(key="stop", translation_key="stop"),
    ButtonEntityDescription(key="jog_up", translation_key="jog_up"),
    ButtonEntityDescription(key="jog_down", translation_key="jog_down"),
    ButtonEntityDescription(key="range_start", translation_key="range_start"),
    ButtonEntityDescription(key="set_direction_clockwise", translation_key="set_direction_clockwise"),
    ButtonEntityDescription(
        key="set_direction_counterclockwise",
        translation_key="set_direction_counterclockwise",
    ),
    ButtonEntityDescription(key="set_upper_limit", translation_key="set_upper_limit"),
    ButtonEntityDescription(key="set_lower_limit", translation_key="set_lower_limit"),
)

INTERROGATE_DESCRIPTION: Final = ButtonEntityDescription(
    key="interrogate", translation_key="interrogate"
)

_COMMANDS: Final = {
    "identify": (Command.IDENTIFY, JogDirection.UP),
    "stop": (Command.STOP, JogDirection.UP),
    "jog_up": (Command.JOG, JogDirection.UP),
    "jog_down": (Command.JOG, JogDirection.DOWN),
    "range_start": (Command.RANGE_START, JogDirection.UP),
    "set_direction_clockwise": (Command.SET_DIRECTION_CLOCKWISE, JogDirection.UP),
    "set_direction_counterclockwise": (
        Command.SET_DIRECTION_COUNTERCLOCKWISE,
        JogDirection.UP,
    ),
    "set_upper_limit": (Command.SET_UPPER_LIMIT, JogDirection.UP),
    "set_lower_limit": (Command.SET_LOWER_LIMIT, JogDirection.UP),
}


class SomfyButton(SomfyEntity, ButtonEntity):
    """Admit one command; never perform BLE transport in the entity."""

    _attr_entity_category = None

    async def async_press(self) -> None:
        """Submit a command to the runtime admission boundary."""
        command, direction = _COMMANDS[self.entity_description.key]
        step = 100
        if command is Command.JOG:
            with runtime_errors():
                step = self._runtime.get_jog_step(self._runtime_motor_id)
        request = CommandRequest(
            motor_id=self._runtime_motor_id,
            command=command,
            direction=direction,
            step=step,
        )
        with runtime_errors():
            result = self._runtime.enqueue(request)
        require_success(result)


class SomfyInterrogateButton(SomfyEntity, ButtonEntity):
    """Perform read-only GATT discovery and display a notification dialog."""

    entity_description = INTERROGATE_DESCRIPTION
    _attr_entity_category = None

    async def async_press(self) -> None:
        with runtime_errors():
            result, report = await self._runtime.async_interrogate(
                self._runtime_motor_id
            )
        message = (
            _format_gatt_report(report)
            if result is Result.SUCCESS
            else (
                f"**Interrogation failed: `{result.value}`**\n\n"
                "No PIN, BLE read, BLE write, or motor command was sent."
            )
        )
        persistent_notification.async_create(
            self.hass,
            message,
            title=f"{self.device_info['name']} · Bluetooth Interrogation",
            notification_id=f"somfy_gatt_{self.unique_id}",
        )
        require_success(result)


_CHARACTERISTIC_LABELS: Final = {item.uuid: item.label for item in Characteristic}


def _format_gatt_report(report: tuple[dict[str, object], ...]) -> str:
    """Format bounded GATT metadata as readable Home Assistant Markdown."""
    lines = [
        f"**{len(report)} services discovered. No PIN or BLE writes were used.**",
        "",
    ]
    for service in report:
        lines.append(f"### Service `{service.get('uuid', 'unknown')}`")
        characteristics = service.get("characteristics", ())
        if not isinstance(characteristics, (list, tuple)):
            characteristics = ()
        for characteristic in characteristics:
            if not isinstance(characteristic, dict):
                continue
            properties = characteristic.get("properties", ())
            property_text = ", ".join(str(value) for value in properties) or "none"
            uuid = characteristic.get("uuid", "unknown")
            label = _CHARACTERISTIC_LABELS.get(str(uuid).lower())
            prefix = f"**{label}** · " if label else ""
            lines.append(f"- {prefix}`{uuid}` — **{property_text}**")
            descriptors = characteristic.get("descriptors", ())
            if isinstance(descriptors, (list, tuple)):
                for descriptor in descriptors:
                    lines.append(f"  - descriptor `{descriptor}`")
        lines.append("")
    return "\n".join(lines)[:16000]


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create command controls from the shared runtime context."""
    runtime, motor_id, display_name = runtime_context(config_entry)
    async_add_entities(
        [
            SomfyButton(runtime, motor_id, display_name, description)
            for description in BUTTON_DESCRIPTIONS
        ]
        + [SomfyInterrogateButton(runtime, motor_id, display_name)]
    )


__all__ = [
    "BUTTON_DESCRIPTIONS",
    "INTERROGATE_DESCRIPTION",
    "SomfyButton",
    "SomfyInterrogateButton",
    "async_setup_entry",
]
