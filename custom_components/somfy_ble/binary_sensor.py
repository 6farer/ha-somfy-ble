"""BLE reachability binary sensor."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .entity import SomfyEntity, runtime_context


class SomfyBleReachable(SomfyEntity, BinarySensorEntity):
    """Report BLE reachability independently of HA/entity availability."""

    description = BinarySensorEntityDescription(
        key="ble_reachable",
        translation_key="ble_reachable",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    )
    entity_description = description
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def is_on(self) -> bool:
        value = self._snapshot().reachable
        return value if isinstance(value, bool) else False


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: Any,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create the BLE reachability diagnostic entity."""
    runtime, motor_id, display_name = runtime_context(config_entry)
    async_add_entities([SomfyBleReachable(runtime, motor_id, display_name)])


__all__ = ["SomfyBleReachable", "async_setup_entry"]
