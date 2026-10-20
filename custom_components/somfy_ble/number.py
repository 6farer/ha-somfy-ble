"""Jog-step number entity."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SomfyEntity, require_success, runtime_context, runtime_errors


JOG_STEP_MIN: Final = 1
JOG_STEP_MAX: Final = 500
JOG_STEP_DEFAULT: Final = 100


def _jog_step(value: object) -> int | None:
    """Accept finite integral values within the motor's jog range."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not JOG_STEP_MIN <= value <= JOG_STEP_MAX:
        return None
    integer = int(value)
    return integer if integer == value else None


class SomfyJogStep(SomfyEntity, NumberEntity):
    """Integer jog distance setting; it is not a physical limit value."""

    description = NumberEntityDescription(
        key="jog_step",
        translation_key="jog_step",
        native_min_value=JOG_STEP_MIN,
        native_max_value=JOG_STEP_MAX,
        native_step=1,
        mode=NumberMode.BOX,
    )
    entity_description = description
    _attr_entity_category = None
    _attr_native_min_value = JOG_STEP_MIN
    _attr_native_max_value = JOG_STEP_MAX
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX

    @property
    def native_value(self) -> int:
        try:
            value = self._runtime.get_jog_step(self._runtime_motor_id)
        except Exception:
            return JOG_STEP_DEFAULT
        return _jog_step(value) or JOG_STEP_DEFAULT

    async def async_set_native_value(self, value: float) -> None:
        """Accept only finite integral values in the protocol range."""
        integer = _jog_step(value)
        if integer is None:
            raise HomeAssistantError("invalid_payload")
        with runtime_errors("invalid_payload"):
            result = self._runtime.set_jog_step(self._runtime_motor_id, integer)
        require_success(result)
        self._write_state()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the jog-step control from the shared runtime context."""
    runtime, motor_id, display_name = runtime_context(config_entry)
    async_add_entities([SomfyJogStep(runtime, motor_id, display_name)])


__all__ = [
    "JOG_STEP_DEFAULT",
    "JOG_STEP_MAX",
    "JOG_STEP_MIN",
    "SomfyJogStep",
    "async_setup_entry",
]
