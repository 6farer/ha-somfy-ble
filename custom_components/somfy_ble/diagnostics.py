"""Secret-safe diagnostics for the Somfy config entry."""

from __future__ import annotations

from typing import Any, Mapping

from homeassistant.config_entries import ConfigEntry

from .const import CONF_ADDRESS
from .entity import (
    _safe_command_id,
    _safe_display_name,
    _safe_label,
    _safe_seconds,
    safe_error,
    safe_recovery,
    safe_result,
    safe_status,
)
from .models import MAX_QUEUE_DEPTH

_SAFE_RUNTIME_FIELDS = {
    "reachable",
    "last_result",
    "status",
    "selected_source",
    "last_error",
    "last_command_id",
    "last_contact_age",
    "queue_depth",
    "active_command_id",
    "recovery_state",
}


def _runtime_snapshot(runtime: object, motor_id: str) -> Mapping[str, Any]:
    try:
        snapshot = runtime.diagnostics_snapshot(motor_id)  # type: ignore[attr-defined]
    except Exception:
        return {}
    return snapshot if isinstance(snapshot, Mapping) else {}


def _safe_runtime(runtime: object, motor_id: str) -> dict[str, Any]:
    raw = _runtime_snapshot(runtime, motor_id)
    safe: dict[str, Any] = {}
    for key in _SAFE_RUNTIME_FIELDS:
        if key not in raw:
            continue
        value = raw[key]
        if key == "last_result":
            safe[key] = safe_result(value)
        elif key == "status":
            safe[key] = safe_status(value)
        elif key == "selected_source":
            safe[key] = _safe_label(value)
        elif key == "last_error":
            safe[key] = safe_error(value)
        elif key == "recovery_state":
            safe[key] = safe_recovery(value)
        elif key == "last_contact_age":
            number = _safe_seconds(value)
            if number is not None:
                safe[key] = number
        elif key in {"last_command_id", "active_command_id"}:
            command_id = _safe_command_id(value)
            if command_id is not None:
                safe[key] = command_id
        elif key == "queue_depth":
            if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_QUEUE_DEPTH:
                safe[key] = value
        elif key == "reachable" and isinstance(value, bool):
            safe[key] = value
    return safe


async def async_get_config_entry_diagnostics(
    hass: Any,
    entry: ConfigEntry[Any],
) -> dict[str, Any]:
    """Return an allowlisted snapshot without config data/options or raw runtime objects."""
    entry_data = {
        "entry_id": entry.entry_id,
        "title": _safe_display_name(entry.title),
    }
    address = entry.data.get(CONF_ADDRESS)
    if isinstance(address, str):
        entry_data[CONF_ADDRESS] = address
    runtime_data = getattr(entry, "runtime_data", None)
    runtime = getattr(runtime_data, "runtime", runtime_data)
    motor_id = getattr(runtime_data, "motor_id", "")
    return {"entry": entry_data, "runtime": _safe_runtime(runtime, motor_id)}


__all__ = ["async_get_config_entry_diagnostics"]
