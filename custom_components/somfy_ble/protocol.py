from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


_UUID_SUFFIX = "-cad9-46c6-a2ea-2ca16d57b4a5"
_MAX_PIN = 0xFFFFFF
_MAX_PIN_TEXT_LENGTH = 8
_MIN_STEP = 1
_MAX_STEP = 500
_UINT32_MAX = 0xFFFFFFFF


class Command(Enum):
    IDENTIFY = "identify"
    STOP = "stop"
    JOG = "jog"
    RANGE_START = "range_start"
    SET_UPPER_LIMIT = "set_upper_limit"
    SET_LOWER_LIMIT = "set_lower_limit"
    SET_DIRECTION_COUNTERCLOCKWISE = "set_direction_counterclockwise"
    SET_DIRECTION_CLOCKWISE = "set_direction_clockwise"


class Characteristic(Enum):
    """Somfy GATT characteristics: key, 32-bit identifier, and display label."""

    IDENTIFY = (0x00000001, "Identify blind")
    STOP = (0x00000006, "Stop")
    AUTH = (0x0000000B, "PIN authentication")
    DIRECTION = (0x00010005, "Set direction")
    LIMIT = (0x00010007, "Save open / closed limit")
    JOG_DOWN = (0x00010008, "Jog down")
    JOG_UP = (0x00010009, "Jog up")
    RANGE_START = (0x0001000B, "Start range setup")

    def __init__(self, identifier: int, label: str) -> None:
        self.identifier = identifier
        self.label = label

    @property
    def uuid(self) -> str:
        return somfy_uuid(self.identifier)


class JogDirection(Enum):
    UP = "up"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class CommandPacket:
    characteristic_uuid: str
    payload: bytes


def somfy_uuid(identifier: int) -> str:
    """Return a Somfy custom UUID for a 32-bit characteristic identifier."""
    if isinstance(identifier, bool) or not isinstance(identifier, int) or not 0 <= identifier <= _UINT32_MAX:
        raise ValueError("UUID identifier must be an unsigned 32-bit integer")
    return f"{identifier:08x}{_UUID_SUFFIX}"


def encode_pin(pin: str) -> bytes:
    """Encode six-character hex PINs, 0x-prefixed hex, or other-length decimal PINs."""
    if not isinstance(pin, str) or not pin:
        raise ValueError("PIN must be a non-empty decimal or 0x-prefixed hexadecimal string")
    if len(pin) > _MAX_PIN_TEXT_LENGTH:
        raise ValueError("PIN text is too long")

    is_prefixed_hex = len(pin) == 8 and pin[0] == "0" and pin[1] in "xX"
    # Motors print a six-character hex PIN. All-digit ones are hex too: sending
    # them as decimal was rejected by every such motor (2026-10-18).
    is_bare_hex = len(pin) == 6 and all(
        digit in "0123456789abcdefABCDEF" for digit in pin
    )
    if is_prefixed_hex or is_bare_hex:
        digits = pin[2:] if is_prefixed_hex else pin
        if any(digit not in "0123456789abcdefABCDEF" for digit in digits):
            raise ValueError("PIN hexadecimal digits are invalid")
        return bytes.fromhex(digits)
    else:
        if any(digit not in "0123456789" for digit in pin):
            raise ValueError("PIN decimal digits are invalid")
        value = int(pin, 10)

    if value > _MAX_PIN:
        raise ValueError("PIN is outside the unsigned 24-bit range")
    return value.to_bytes(3, byteorder="little")


def encode_step(step: int) -> bytes:
    """Encode a Somfy jog step as an unsigned 16-bit little-endian integer."""
    if isinstance(step, bool) or not isinstance(step, int) or not _MIN_STEP <= step <= _MAX_STEP:
        raise ValueError("step is outside the Somfy protocol range")
    return step.to_bytes(2, byteorder="little")


def make_command(
    command: Command,
    *,
    direction: JogDirection = JogDirection.UP,
    step: int = 100,
) -> CommandPacket:
    """Build an immutable Somfy characteristic write packet."""
    if not isinstance(command, Command):
        raise ValueError("command is not a Somfy command")

    if command is Command.IDENTIFY:
        return CommandPacket(Characteristic.IDENTIFY.uuid, b"\x01")
    if command is Command.STOP:
        return CommandPacket(Characteristic.STOP.uuid, b"\x01")
    if command is Command.JOG:
        if not isinstance(direction, JogDirection):
            raise ValueError("jog direction is invalid")
        characteristic = Characteristic.JOG_UP if direction is JogDirection.UP else Characteristic.JOG_DOWN
        return CommandPacket(characteristic.uuid, encode_step(step))
    if command is Command.RANGE_START:
        return CommandPacket(Characteristic.RANGE_START.uuid, b"\x00\x00")
    if command is Command.SET_UPPER_LIMIT:
        return CommandPacket(Characteristic.LIMIT.uuid, b"\x00")
    if command is Command.SET_LOWER_LIMIT:
        return CommandPacket(Characteristic.LIMIT.uuid, b"\x01")
    if command is Command.SET_DIRECTION_COUNTERCLOCKWISE:
        return CommandPacket(Characteristic.DIRECTION.uuid, b"\x00")
    if command is Command.SET_DIRECTION_CLOCKWISE:
        return CommandPacket(Characteristic.DIRECTION.uuid, b"\x01")
    raise ValueError("command is not a Somfy command")
