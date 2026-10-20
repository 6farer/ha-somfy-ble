"""Shared entity presentation types and the minimal runtime boundary.

The runtime protocol is deliberately small: entity classes only admit commands or
read redacted state. BLE discovery, transport, queue workers, and credential
handling belong to the runtime implementation supplied by the integration.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import re
from typing import Any, Callable, Final, Mapping, Protocol, runtime_checkable

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN, MAX_CONTACT_AGE_SECONDS
from .models import CommandRequest, EnqueueResult, Result
from .protocol import Command, JogDirection


_ADDRESS_SEPARATOR_RE = re.compile(r"[\s:.-]")
_HEX_ADDRESS_RE = re.compile(r"^[0-9a-fA-F]{12}$")
_BLUETOOTH_TOKEN_RE = re.compile(
    r"(?<![0-9a-fA-F])(?:[0-9a-fA-F]{2}[\s:.-]?){6}(?![0-9a-fA-F])"
)
_IPV4_RE = re.compile(r"(?<![0-9])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9])")
_PIN_RE = re.compile(r"(?<![0-9A-Za-z])(?:[0-9]{6}|0[xX][0-9a-fA-F]{6})(?![0-9A-Za-z])")
_CREDENTIAL_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:token|pin|password|secret|credential|bearer|auth|"
    r"api[ _-]?key|access[ _-]?key|private[ _-]?key|(?:wifi[ _-]?)?psk|"
    r"pass[ _-]?phrase|wifi[ _-]?password|ota[ _-]?password|"
    r"client[ _-]?secret|refresh[ _-]?token)(?![a-z0-9])"
)
_NUMERIC_PIN_RE = re.compile(r"(?<![0-9A-Za-z])[0-9]{4,8}(?![0-9A-Za-z])")
_TOKEN_COMPOSITE_RE = re.compile(
    r"(?i)(?<![a-z0-9])(?:auth[_-]?token|bearer(?:[_ -]?token)?|"
    r"access[_ -]?token|secret[_ -]?token)(?![a-z0-9])"
)
_STRIPE_KEY_PREFIX_RE = re.compile(r"(?i)(?<![a-z0-9])(?:sk|pk)_(?:live|test)_")
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+"
    r"(?![A-Za-z0-9_-])"
)
_RESULT_CATEGORIES = frozenset(result.value for result in Result)
_STATUS_CATEGORIES = frozenset(
    {
        "unknown",
        "online",
        "offline",
        "ready",
        "idle",
        "sent_unconfirmed",
        "degraded",
        "unavailable",
        "connected",
        "disconnected",
        "busy",
        "recovering",
    }
)
_ERROR_CATEGORIES = _RESULT_CATEGORIES | {"error", "none", "unknown"}
_RECOVERY_CATEGORIES = frozenset({"unknown", "failure", "recovery", "recovering", "healthy", "degraded"})
_CATEGORY_ALLOWLISTS = {
    "result": _RESULT_CATEGORIES,
    "status": _STATUS_CATEGORIES,
    "error": _ERROR_CATEGORIES,
    "recovery": _RECOVERY_CATEGORIES,
}
_SOURCE_LABELS = frozenset({"esphable1", "esphable2", "local", "none", "other", "unknown"})
_DEFAULT_DISPLAY_NAME = "Somfy Blind"
_MAX_DISPLAY_NAME_LENGTH = 64
# Keep presentation-layer command identifiers within a signed 64-bit range.
MAX_COMMAND_ID: Final = 2**63 - 1


@dataclass(frozen=True, slots=True)
class RuntimeState:
    """Safe, presentation-ready state returned by a runtime adapter."""

    reachable: bool = False
    last_result: str | Result | None = None
    status: str | None = None
    selected_source: str | None = None
    last_error: str | None = None
    last_command_id: int | None = None
    last_contact_age: float | None = None


@runtime_checkable
class SomfyRuntime(Protocol):
    """Minimal synchronous admission/state protocol consumed by entities.

    ``subscribe`` is transactional: an exception leaves no callback registered.
    On success it returns an idempotent, non-throwing cleanup callable that removes
    the callback.  Entity lifecycle code still tolerates an unexpected provider
    cleanup exception so removal cannot leak a raw provider failure.
    """

    def subscribe(
        self, motor_id: str, callback: Callable[[], None]
    ) -> Callable[[], None]: ...

    def enqueue(self, request: CommandRequest) -> EnqueueResult: ...


    def get_jog_step(self, motor_id: str) -> int: ...

    def set_jog_step(self, motor_id: str, value: int) -> Result: ...

    def snapshot(self, motor_id: str) -> RuntimeState: ...

    def diagnostics_snapshot(self, motor_id: str) -> Mapping[str, Any]: ...

    async def async_interrogate(
        self, motor_id: str
    ) -> tuple[Result, tuple[dict[str, object], ...]]: ...


def _canonical_bluetooth_bytes(identity: str) -> bytes | None:
    """Return canonical six-byte Bluetooth identity for supported text forms."""
    compact = _ADDRESS_SEPARATOR_RE.sub("", identity.strip())
    if not _HEX_ADDRESS_RE.fullmatch(compact):
        return None
    return bytes.fromhex(compact)


def _contains_bluetooth_address(value: str) -> bool:
    return _BLUETOOTH_TOKEN_RE.search(value) is not None


def _contains_ipv4_address(value: str) -> bool:
    return any(
        all(int(octet) <= 255 for octet in match.group().split("."))
        for match in _IPV4_RE.finditer(value)
    )


def opaque_motor_id(identity: str) -> str:
    """Return a stable opaque ID for every valid motor identity."""
    if not isinstance(identity, str) or not identity.strip():
        raise ValueError("motor identity must be a non-empty string")
    canonical = _canonical_bluetooth_bytes(identity)
    if canonical is not None:
        payload = canonical
    else:
        payload = identity.encode("utf-8", errors="surrogatepass")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    return f"motor_{digest}"


def _safe_label(value: object, *, fallback: str = "unknown") -> str:
    """Return only exact, audit-readable operational source labels."""
    if isinstance(value, str) and value in _SOURCE_LABELS:
        return value
    if isinstance(value, str) and value:
        return "other"
    return fallback if fallback in _SOURCE_LABELS else "unknown"


def _safe_command_id(value: object) -> int | None:
    """Return only bounded, non-boolean command IDs for presentation fields."""
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_COMMAND_ID:
        return value
    return None


def _safe_seconds(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if 0 <= value <= MAX_CONTACT_AGE_SECONDS else None


def _safe_display_name(value: object) -> str:
    """Preserve benign room names while rejecting identity/credential-shaped text."""
    if not isinstance(value, str) or not value.strip():
        return _DEFAULT_DISPLAY_NAME
    if not all(character.isprintable() for character in value):
        return _DEFAULT_DISPLAY_NAME
    if (
        _contains_bluetooth_address(value)
        or _contains_ipv4_address(value)
        or _PIN_RE.search(value) is not None
        or _NUMERIC_PIN_RE.search(value) is not None
        or _CREDENTIAL_RE.search(value) is not None
        or _TOKEN_COMPOSITE_RE.search(value) is not None
        or _STRIPE_KEY_PREFIX_RE.search(value) is not None
        or _JWT_RE.search(value) is not None
    ):
        return _DEFAULT_DISPLAY_NAME
    normalized = " ".join(value.split())
    return normalized[:_MAX_DISPLAY_NAME_LENGTH] or _DEFAULT_DISPLAY_NAME


def safe_category(
    value: object,
    *,
    semantic: str = "result",
    fallback: str | None = None,
) -> str:
    """Expose one exact allowlisted category for the requested semantic field."""
    allowed = _CATEGORY_ALLOWLISTS.get(semantic)
    if allowed is None:
        allowed = _CATEGORY_ALLOWLISTS["result"]
        semantic = "result"
    candidate = value.value if isinstance(value, Result) else value
    if isinstance(candidate, str) and candidate in allowed:
        return candidate
    default = "error" if semantic == "error" else "unknown"
    return fallback if fallback in allowed else default


def safe_result(value: object) -> str:
    return safe_category(value, semantic="result")


def safe_status(value: object) -> str:
    return safe_category(value, semantic="status")


def safe_error(value: object) -> str:
    return safe_category(value, semantic="error", fallback="error")


def safe_recovery(value: object) -> str:
    return safe_category(value, semantic="recovery")


def result_code(result: object) -> str:
    """Extract a machine-readable admission result without stringifying objects."""
    if isinstance(result, EnqueueResult):
        result = result.result
    return safe_category(result, semantic="result", fallback=Result.ERROR.value)


def require_success(result: object) -> None:
    """Raise a safe Home Assistant error for a rejected runtime operation."""
    code = result_code(result)
    if code != Result.SUCCESS.value:
        raise HomeAssistantError(code)


@contextmanager
def runtime_errors(code: str = "error") -> Iterator[None]:
    """Keep raw runtime exceptions out of Home Assistant action errors."""
    try:
        yield
    except Exception:
        raise HomeAssistantError(code) from None


def runtime_context(config_entry: Any) -> tuple[SomfyRuntime, str, str]:
    """Extract the future shared runtime context without changing lifecycle wiring."""
    data = config_entry.runtime_data
    runtime = getattr(data, "runtime", data)
    motor_id = getattr(data, "motor_id", config_entry.entry_id)
    return runtime, motor_id, config_entry.title


class SomfyEntity(Entity):
    """Base entity with stable opaque identity and device presentation."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        runtime: SomfyRuntime,
        motor_id: str,
        display_name: str,
        description: Any = None,
    ) -> None:
        self._runtime = runtime
        self._runtime_motor_id = motor_id
        self._motor_id = opaque_motor_id(motor_id)
        self._runtime_subscription_active = False
        if description is None:
            description = self.entity_description
        self.entity_description = description
        self._attr_unique_id = f"{self._motor_id}_{description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._motor_id)},
            name=_safe_display_name(display_name),
            manufacturer="Somfy",
            model="Sonesse2",
        )

    @property
    def available(self) -> bool:
        """Remain available while the HA entry is loaded; BLE is separate state."""
        return True

    def _snapshot(self) -> RuntimeState:
        try:
            state = self._runtime.snapshot(self._runtime_motor_id)
        except Exception:
            return RuntimeState(last_result=Result.ERROR, last_error=Result.ERROR.value)
        return state if isinstance(state, RuntimeState) else RuntimeState()

    async def async_added_to_hass(self) -> None:
        """Subscribe to runtime changes once the entity has an HA event loop."""
        await super().async_added_to_hass()
        if self.hass is None:
            return

        unsubscribe = self._runtime.subscribe(
            self._runtime_motor_id, self._async_runtime_notification
        )
        self._runtime_subscription_active = True
        cleaned_up = False

        def _unsubscribe() -> None:
            nonlocal cleaned_up
            if cleaned_up:
                return
            cleaned_up = True
            self._runtime_subscription_active = False
            try:
                unsubscribe()
            except Exception:
                pass

        try:
            self.async_on_remove(_unsubscribe)
        except Exception:
            _unsubscribe()
            raise

    def _async_runtime_notification(self) -> None:
        """Schedule a state write on HA's loop for thread-safe publication."""
        if not self._runtime_subscription_active or self.hass is None:
            return
        self.hass.loop.call_soon_threadsafe(self._async_write_runtime_state)

    def _async_write_runtime_state(self) -> None:
        if self._runtime_subscription_active:
            self.async_write_ha_state()

    def _write_state(self) -> None:
        """Publish a state change when attached; harmless for direct unit tests."""
        if self.hass is not None:
            self.async_write_ha_state()


__all__ = [
    "RuntimeState",
    "SomfyEntity",
    "SomfyRuntime",
    "opaque_motor_id",
    "result_code",
    "require_success",
    "runtime_context",
    "safe_category",
    "safe_error",
    "safe_recovery",
    "safe_result",
    "safe_status",
]
