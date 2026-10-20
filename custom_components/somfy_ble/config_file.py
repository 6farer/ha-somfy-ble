"""Somfy motor configuration file over BLE: framing, bounded CBOR, write allowlist.

Recovered by static analysis of TaHoma's ``ZigbeeCBORConfigurator`` (it runs over
Bluetooth despite the name). Commands go to the transfer characteristic; file
``00C4`` is the motor configuration. Reply framing is not yet confirmed on a
real motor, so callers keep the raw hex alongside any decoded value.
"""

from __future__ import annotations

import math
import re
import struct
from typing import Any, Final

from .protocol import somfy_uuid


TRANSFER_UUID: Final = somfy_uuid(0x00040001)
STATUS_UUID: Final = somfy_uuid(0x00040003)

MOTOR_FILE: Final = b"\xc4\x00"  # file 00C4, little endian
OP_OPEN: Final = 0x00
OP_SIZE: Final = 0x01
OP_WRITE: Final = 0x02
OP_READ: Final = 0x03
OP_CLOSE: Final = 0x04
MODE_READ: Final = 0x00
MODE_WRITE: Final = 0x01

# The app waits 500 ms between configuration operations.
STEP_DELAY: Final = 0.5
# After OPEN the app polls file status until 05xx (TRANSFERRED) or 06xx (FAILURE).
FILE_STATUS_POLLS: Final = 20
FILE_STATUS_INTERVAL: Final = 0.25
MAX_READ_CHUNKS: Final = 64
MAX_FILE_BYTES: Final = 16384
CONFIG_MAX_AGE_SECONDS: Final = 15 * 60

# Keys the TaHoma BLE action map writes to the motor file. Resets, firmware,
# network and remote pairing are deliberately absent.
WRITABLE_KEYS: Final = frozenset(
    {
        "NominalSpeed",
        "TiltSpeed",
        "StartUpRamp",
        "StartDownRamp",
        "StopUpRamp",
        "StopDownRamp",
        "DisabledSoftRamps",
        "DisabledSoftStartRamp",
        "DisabledSoftStopRamp",
    }
)

READ_SEQUENCE: Final = (
    bytes((OP_OPEN,)) + MOTOR_FILE + bytes((MODE_READ,)),
    bytes((OP_SIZE,)),
    bytes((OP_READ,)),
)
OPEN_FOR_WRITE: Final = bytes((OP_OPEN,)) + MOTOR_FILE + bytes((MODE_WRITE,))
CLOSE: Final = bytes((OP_CLOSE,))

_MAX_DEPTH: Final = 16
_MAX_ITEMS: Final = 512
_ADDRESS_RE: Final = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")


class CborError(ValueError):
    """The bytes are not a complete, bounded CBOR item."""


def write_payload(key: str, value: Any) -> bytes:
    """Return the WRITE command for one parameter, as the app sends it."""
    return bytes((OP_WRITE,)) + cbor_encode({key: [value]})


def redact(text: str) -> str:
    """Strip Bluetooth addresses from transport error text."""
    return _ADDRESS_RE.sub("<address>", text)[:200]


def cbor_encode(value: Any) -> bytes:
    """Encode maps, arrays, text, ints, bools, None and floats (definite lengths)."""
    if value is None:
        return b"\xf6"
    if value is True:
        return b"\xf5"
    if value is False:
        return b"\xf4"
    if isinstance(value, int):
        return _head(0, value) if value >= 0 else _head(1, -1 - value)
    if isinstance(value, float):
        return b"\xfb" + struct.pack(">d", value)
    if isinstance(value, str):
        data = value.encode("utf-8")
        return _head(3, len(data)) + data
    if isinstance(value, (list, tuple)):
        return _head(4, len(value)) + b"".join(cbor_encode(item) for item in value)
    if isinstance(value, dict):
        return _head(5, len(value)) + b"".join(
            cbor_encode(key) + cbor_encode(item) for key, item in value.items()
        )
    raise CborError(f"cannot encode {type(value).__name__}")


def _head(major: int, length: int) -> bytes:
    if length < 24:
        return bytes((major << 5 | length,))
    for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
        if length < 1 << (8 * size):
            return bytes((major << 5 | info,)) + length.to_bytes(size, "big")
    raise CborError("integer too large")


def cbor_decode(data: bytes) -> Any:
    """Decode exactly one CBOR item; byte strings become hex text."""
    value, offset = _decode(data, 0, 0)
    if offset != len(data):
        raise CborError(f"{len(data) - offset} trailing bytes")
    return value


def _decode(data: bytes, offset: int, depth: int) -> tuple[Any, int]:
    if depth > _MAX_DEPTH:
        raise CborError("nested too deeply")
    if offset >= len(data):
        raise CborError("truncated")
    initial = data[offset]
    major, info = initial >> 5, initial & 0x1F
    offset += 1
    if major == 7:
        return _decode_simple(data, offset, info)
    if info == 31:
        return _decode_indefinite(data, offset, major, depth)
    length, offset = _argument(data, offset, info)
    if major == 0:
        return length, offset
    if major == 1:
        return -1 - length, offset
    if major in (2, 3):
        end = offset + length
        if end > len(data):
            raise CborError("truncated string")
        chunk = data[offset:end]
        return (chunk.hex() if major == 2 else chunk.decode("utf-8", "replace")), end
    if length > _MAX_ITEMS:
        raise CborError("too many items")
    if major == 4:
        items = []
        for _ in range(length):
            item, offset = _decode(data, offset, depth + 1)
            items.append(item)
        return items, offset
    if major == 5:
        mapping: dict[str, Any] = {}
        for _ in range(length):
            key, offset = _decode(data, offset, depth + 1)
            item, offset = _decode(data, offset, depth + 1)
            mapping[str(key)] = item
        return mapping, offset
    # Major 6: a tag; keep the tagged value.
    return _decode(data, offset, depth + 1)


def _decode_indefinite(data: bytes, offset: int, major: int, depth: int) -> tuple[Any, int]:
    if major not in (2, 3, 4, 5):
        raise CborError("invalid indefinite length")
    items: list[Any] = []
    while True:
        if offset >= len(data):
            raise CborError("truncated")
        if data[offset] == 0xFF:
            offset += 1
            break
        if len(items) > 2 * _MAX_ITEMS:
            raise CborError("too many items")
        item, offset = _decode(data, offset, depth + 1)
        items.append(item)
    if major in (2, 3):
        return "".join(items), offset
    if major == 4:
        return items, offset
    if len(items) % 2:
        raise CborError("odd map")
    return {str(items[i]): items[i + 1] for i in range(0, len(items), 2)}, offset


def _argument(data: bytes, offset: int, info: int) -> tuple[int, int]:
    if info < 24:
        return info, offset
    size = {24: 1, 25: 2, 26: 4, 27: 8}.get(info)
    if size is None or offset + size > len(data):
        raise CborError("invalid length")
    return int.from_bytes(data[offset : offset + size], "big"), offset + size


def _decode_simple(data: bytes, offset: int, info: int) -> tuple[Any, int]:
    if info == 20:
        return False, offset
    if info == 21:
        return True, offset
    if info in (22, 23):
        return None, offset
    formats = {25: (">e", 2), 26: (">f", 4), 27: (">d", 8)}
    if info in formats:
        fmt, size = formats[info]
        if offset + size > len(data):
            raise CborError("truncated float")
        value = struct.unpack(fmt, data[offset : offset + size])[0]
        return (value if math.isfinite(value) else str(value)), offset + size
    if info < 24:
        return f"simple({info})", offset
    raise CborError("invalid simple value")


def decode_file(data: bytes) -> tuple[dict[str, Any] | None, int]:
    """Find the configuration map in a file read; return it and its byte offset.

    The reply may carry a short header before the CBOR, so try the first few
    offsets and accept the first that decodes completely to a map.
    """
    for start in range(min(8, len(data))):
        try:
            value = cbor_decode(data[start:])
        except (CborError, UnicodeDecodeError, struct.error):
            continue
        if isinstance(value, dict):
            return value, start
    return None, -1


def check_write(config: dict[str, Any] | None, key: str, value: Any) -> str | None:
    """Return why a write must be refused, or None when it may proceed."""
    if key not in WRITABLE_KEYS:
        return f"{key} is not a writable motor setting"
    if isinstance(value, float) or not isinstance(value, (int, str, bool)):
        return "value must be an integer, text or true/false"
    if config is None:
        return "read the motor config first"
    if key not in config:
        return f"{key} is not in this motor's config"
    # Each entry is [value, detail]; detail has w (writable), l/h (range), e (choices).
    current = setting_value(config[key])
    detail = config[key][1] if isinstance(config[key], list) and len(config[key]) > 1 else {}
    if not isinstance(detail, dict) or detail.get("w") is not True:
        return f"the motor reports {key} as read-only"
    if type(value) is not type(current):
        return f"{key} needs a {type(current).__name__} value"
    if "l" in detail and "h" in detail and not detail["l"] <= value <= detail["h"]:
        return f"{key} must be between {detail['l']} and {detail['h']}"
    if "e" in detail and value not in detail["e"]:
        return f"{key} must be one of {', '.join(map(str, detail['e']))}"
    return None


def setting_value(entry: Any) -> Any:
    """Return the value from a config entry shaped [value, detail]."""
    return entry[0] if isinstance(entry, list) and entry else entry


__all__ = [
    "CLOSE",
    "CONFIG_MAX_AGE_SECONDS",
    "FILE_STATUS_INTERVAL",
    "FILE_STATUS_POLLS",
    "MAX_FILE_BYTES",
    "MAX_READ_CHUNKS",
    "OPEN_FOR_WRITE",
    "OP_CLOSE",
    "OP_OPEN",
    "OP_READ",
    "OP_SIZE",
    "OP_WRITE",
    "READ_SEQUENCE",
    "STATUS_UUID",
    "STEP_DELAY",
    "TRANSFER_UUID",
    "WRITABLE_KEYS",
    "CborError",
    "cbor_decode",
    "cbor_encode",
    "check_write",
    "decode_file",
    "redact",
    "setting_value",
    "write_payload",
]
