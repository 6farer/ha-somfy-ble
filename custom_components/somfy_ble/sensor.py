"""Redacted runtime diagnostic sensors."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.sensor import SensorEntity, SensorEntityDescription
from homeassistant.const import UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import (
    SomfyEntity,
    _safe_command_id,
    _safe_label,
    _safe_seconds,
    safe_error,
    safe_result,
    safe_status,
    runtime_context,
)


SENSOR_DESCRIPTIONS: Final = (
    SensorEntityDescription(key="last_result", translation_key="last_result"),
    SensorEntityDescription(key="status", translation_key="status"),
    SensorEntityDescription(key="selected_source", translation_key="selected_source"),
    SensorEntityDescription(key="last_error", translation_key="last_error"),
    SensorEntityDescription(key="last_command_id", translation_key="last_command_id"),
    SensorEntityDescription(
        key="last_contact_age",
        translation_key="last_contact_age",
        native_unit_of_measurement=UnitOfTime.SECONDS,
    ),
)


class SomfySensor(SomfyEntity, SensorEntity):
    """Expose only bounded machine categories and scalar runtime values."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | int | float | None:
        key = self.entity_description.key
        state = self._snapshot()
        if key == "last_result":
            return safe_result(state.last_result)
        if key == "status":
            return safe_status(state.status)
        if key == "selected_source":
            return _safe_label(state.selected_source)
        if key == "last_error":
            return safe_error(state.last_error) if state.last_error else "none"
        if key == "last_command_id":
            return _safe_command_id(state.last_command_id)
        if key == "last_contact_age":
            return _safe_seconds(state.last_contact_age)
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the redacted diagnostic sensors from the shared runtime context."""
    runtime, motor_id, display_name = runtime_context(config_entry)
    async_add_entities(
        [
            SomfySensor(runtime, motor_id, display_name, description)
            for description in SENSOR_DESCRIPTIONS
        ]
    )


__all__ = ["SENSOR_DESCRIPTIONS", "SomfySensor", "async_setup_entry"]
