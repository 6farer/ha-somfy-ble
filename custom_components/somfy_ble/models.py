from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .protocol import Command as ProtocolCommand
from .protocol import CommandPacket, JogDirection


COMMAND_TTL_SECONDS = 45.0
MAX_QUEUE_DEPTH = 8
# A full-queue Stop may terminalize displaced work while admitting itself.
# This is the exact maximum number of admitted plus deferred records: 8 + 1.
MAX_TRACKED_RECORDS = MAX_QUEUE_DEPTH + 1


class Result(str, Enum):
    """Machine-readable outcomes for the safety coordinator boundary."""

    SUCCESS = "success"
    SENT_UNCONFIRMED = "sent_unconfirmed"
    QUEUE_FULL = "queue_full"
    EXPIRED = "expired"
    PREEMPTED = "preempted"
    OWNER_CONFLICT = "owner_conflict"
    INVALID_PAYLOAD = "invalid_payload"
    TIMEOUT_BEFORE_WRITE = "timeout_before_write"
    UNKNOWN_AFTER_WRITE = "unknown_after_write"
    AUTH_FAILED = "auth_failed"
    NOT_FOUND = "not_found"
    BUSY = "busy"
    STALE_CALLBACK = "stale_callback"
    DISCONNECTED = "disconnected"
    ERROR = "error"


def result_name(result: Result) -> str:
    return result.value


@dataclass(frozen=True, slots=True)
class CommandRequest:
    """Validated command intent; it intentionally contains no PIN or credential."""

    motor_id: str
    command: ProtocolCommand
    direction: JogDirection = JogDirection.UP
    step: int = 100


@dataclass(frozen=True, slots=True)
class AdmittedCommand:
    """Immutable command admitted to the bounded coordinator."""

    id: int
    request: CommandRequest
    packet: CommandPacket
    enqueued_at: float


@dataclass(frozen=True, slots=True)
class EnqueueResult:
    result: Result
    command_id: int


@dataclass(frozen=True, slots=True)
class BeginResult:
    result: Result
    command_id: int = 0
    command: AdmittedCommand | None = None


@dataclass(frozen=True, slots=True)
class CompletionResult:
    result: Result
    command_id: int
