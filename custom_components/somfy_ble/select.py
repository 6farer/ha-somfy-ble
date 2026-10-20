"""Domain-level selector for the blind the calibration dashboard controls."""

from __future__ import annotations

from typing import Any, Final

from homeassistant.components.select import SelectEntity
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .const import DOMAIN
from .entity import _safe_display_name, opaque_motor_id


SIGNAL_MOTORS_CHANGED: Final = f"{DOMAIN}_motors_changed"


class SomfySelectedBlind(SelectEntity, RestoreEntity):
    """Pick one configured motor; expose its entity IDs for scripts and cards."""

    _attr_has_entity_name = False
    _attr_name = "Somfy BLE selected blind"
    _attr_unique_id = f"{DOMAIN}_selected_blind"
    _attr_icon = "mdi:blinds-horizontal"
    _attr_should_poll = False
    _unrecorded_attributes = frozenset({"entities"})

    def __init__(self) -> None:
        self._attr_options: list[str] = []
        self._attr_current_option: str | None = None
        self._entry_ids: dict[str, str] = {}

    def _refresh(self) -> None:
        """Rebuild options from config entries; keep the selection when it still exists."""
        entries = [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if not entry.disabled_by
        ]
        self._entry_ids = {_safe_display_name(entry.title): entry.entry_id for entry in entries}
        self._attr_options = sorted(self._entry_ids)
        if self._attr_current_option not in self._entry_ids:
            self._attr_current_option = self._attr_options[0] if self._attr_options else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        entry_id = self._entry_ids.get(self._attr_current_option or "")
        if entry_id is None:
            return {"entities": {}}
        prefix = f"{opaque_motor_id(entry_id)}_"
        registry = er.async_get(self.hass)
        entities = {
            entry.unique_id.removeprefix(prefix): entry.entity_id
            for entry in er.async_entries_for_config_entry(registry, entry_id)
            if entry.unique_id.startswith(prefix)
        }
        return {"entities": dict(sorted(entities.items()))}

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if (last := await self.async_get_last_state()) is not None:
            self._attr_current_option = last.state
        self._refresh()

        @callback
        def _async_changed(*_: Any) -> None:
            self._refresh()
            self.async_write_ha_state()

        @callback
        def _async_registry_updated(event: Event[er.EventEntityRegistryUpdatedData]) -> None:
            entity = er.async_get(self.hass).async_get(event.data["entity_id"])
            if entity is None or entity.platform == DOMAIN:
                _async_changed()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_MOTORS_CHANGED, _async_changed)
        )
        self.async_on_remove(
            self.hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _async_registry_updated)
        )

    async def async_select_option(self, option: str) -> None:
        if option not in self._entry_ids:
            raise ValueError(f"unknown blind: {option}")
        self._attr_current_option = option
        self.async_write_ha_state()


async def async_setup_platform(
    hass: HomeAssistant,
    config: Any,
    async_add_entities: AddEntitiesCallback,
    discovery_info: Any = None,
) -> None:
    """Create the single selector; it outlives any one motor's config entry."""
    async_add_entities([SomfySelectedBlind()])


__all__ = ["SIGNAL_MOTORS_CHANGED", "SomfySelectedBlind", "async_setup_platform"]
