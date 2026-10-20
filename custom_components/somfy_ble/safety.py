from __future__ import annotations

from collections import deque
import math
from typing import Callable, Deque, Iterable, List, Optional, Tuple

from .models import (
    COMMAND_TTL_SECONDS,
    MAX_QUEUE_DEPTH,
    MAX_TRACKED_RECORDS,
    AdmittedCommand,
    BeginResult,
    CommandRequest,
    CompletionResult,
    EnqueueResult,
    Result,
)
from .protocol import Command as ProtocolCommand
from .protocol import make_command


class Controller:
    """Synchronous, framework-independent Somfy safety and admission state machine.

    The caller serializes access. The clock is injected so command expiry
    can be tested without sleeping. No transport, credentials, or async task is
    owned here.

    Deferred terminal records are bounded by the exact nine-record tracked-state
    budget: a full eight-command admitted queue may admit one Stop while
    terminalizing displaced pending work. Admission is back-pressured while
    deferred records await draining, and every append checks the invariant instead
    of allowing a bounded deque to silently discard an older terminal outcome.
    """

    def __init__(self, clock: Callable[[], float]) -> None:
        self._clock = clock
        self._motors: set[str] = set()
        self._pending: List[AdmittedCommand] = []
        self._active: Optional[AdmittedCommand] = None
        self._write_attempted = False
        self._next_command_id = 1
        self._deferred: Deque[CompletionResult] = deque(maxlen=MAX_TRACKED_RECORDS)

    def set_configured_motors(self, motor_ids: Iterable[str]) -> None:
        """Replace the motor registry after validating non-empty unique string IDs."""
        try:
            ids = tuple(motor_ids)
        except TypeError as error:
            raise ValueError("motor registry must be iterable") from error
        if any(not isinstance(motor_id, str) or not motor_id for motor_id in ids):
            raise ValueError("motor IDs must be non-empty strings")
        if len(set(ids)) != len(ids):
            raise ValueError("motor IDs must be unique")

        self._clear_ownership(Result.ERROR)
        self._motors = set(ids)

    def on_config_changed(self) -> None:
        self._clear_ownership(Result.ERROR)

    def on_reboot(self) -> None:
        # Keep command IDs monotonic: callbacks from the prior lifecycle may survive.
        self._clear_ownership(Result.ERROR)

    def on_unload(self) -> None:
        self._clear_ownership(Result.ERROR)

    def on_config_removed(self) -> None:
        self.on_unload()
        self._motors.clear()

    def on_disconnect(self, motor_id: str) -> None:
        self._terminate_motor(motor_id, Result.DISCONNECTED)

    def on_auth_failure(self, motor_id: str) -> None:
        self._terminate_motor(motor_id, Result.AUTH_FAILED)

    def on_error(self, motor_id: str) -> None:
        self._terminate_motor(motor_id, Result.ERROR)


    def is_configured(self, motor_id: str) -> bool:
        return motor_id in self._motors


    def enqueue(self, request: CommandRequest) -> EnqueueResult:
        command_id = self._allocate_id()
        if request.motor_id not in self._motors:
            return EnqueueResult(Result.NOT_FOUND, command_id)
        try:
            packet = make_command(
                request.command, direction=request.direction, step=request.step
            )
        except (TypeError, ValueError):
            return EnqueueResult(Result.INVALID_PAYLOAD, command_id)

        # A prior terminal record must be drained before another command can
        # create terminal state. A Stop may continue after this call's own
        # expiry purge creates records, but never when records predate the call.
        is_stop = request.command is ProtocolCommand.STOP
        if self._deferred:
            return EnqueueResult(Result.BUSY, command_id)

        now = self._read_clock()
        if now is None:
            return EnqueueResult(Result.ERROR, command_id)
        self._purge_expired(now)
        if self._deferred and not is_stop:
            return EnqueueResult(Result.BUSY, command_id)

        displaced = self._stop_displacement_candidates(request.motor_id) if is_stop else []
        projected_admitted = self._admitted_count() - len(displaced) + 1
        if projected_admitted > MAX_QUEUE_DEPTH:
            return EnqueueResult(Result.BUSY if is_stop else Result.QUEUE_FULL, command_id)
        if len(self._deferred) + projected_admitted > MAX_TRACKED_RECORDS:
            return EnqueueResult(Result.BUSY, command_id)

        if displaced:
            self._preempt(displaced)
        self._pending.append(AdmittedCommand(command_id, request, packet, now))
        self._assert_budget()
        return EnqueueResult(Result.SUCCESS, command_id)

    def begin_next(self) -> BeginResult:
        if self._active is not None:
            return BeginResult(Result.BUSY, self._active.id)
        if not self._pending:
            return BeginResult(Result.NOT_FOUND)

        selected = next(
            (index for index, command in enumerate(self._pending) if self._is_stop(command)),
            0,
        )
        command = self._pending[selected]
        now = self._read_clock()
        if now is None:
            self._pending.pop(selected)
            self._assert_budget()
            return BeginResult(Result.ERROR, command.id)
        self._pending.pop(selected)
        if self._is_expired(command, now):
            self._assert_budget()
            return BeginResult(Result.EXPIRED, command.id)

        self._active = command
        self._write_attempted = False
        self._assert_budget()
        return BeginResult(Result.SUCCESS, command.id, command)

    def prepare_write(self, expected_command_id: int) -> Result:
        if self._active is None:
            return Result.NOT_FOUND
        if self._active.id != expected_command_id:
            return Result.STALE_CALLBACK

        command = self._active
        now = self._read_clock()
        if now is None:
            self._reset_active()
            self._assert_budget()
            return Result.ERROR
        if self._is_expired(command, now):
            self._reset_active()
            self._assert_budget()
            return Result.EXPIRED
        return Result.SUCCESS

    def mark_write_attempted(self, expected_command_id: int) -> bool:
        if self.prepare_write(expected_command_id) is not Result.SUCCESS:
            return False
        assert self._active is not None
        self._write_attempted = True
        return True

    def complete(self, expected_command_id: int, result: Result) -> CompletionResult:
        if not isinstance(result, Result):
            return CompletionResult(Result.INVALID_PAYLOAD, expected_command_id)
        if self._active is None:
            return CompletionResult(Result.NOT_FOUND, expected_command_id)
        if self._active.id != expected_command_id:
            return CompletionResult(Result.STALE_CALLBACK, expected_command_id)

        command = self._active
        final_result = result
        if self._write_attempted and result not in (
            Result.SUCCESS,
            Result.SENT_UNCONFIRMED,
        ):
            final_result = Result.UNKNOWN_AFTER_WRITE
        self._reset_active()
        self._assert_budget()
        return CompletionResult(final_result, command.id)

    def pending_count(self) -> int:
        return len(self._pending)

    def deferred_count(self) -> int:
        """Return terminal records waiting for the owning runtime to publish."""
        return len(self._deferred)

    def deferred_command_ids(self) -> tuple[int, ...]:
        """Expose only deferred IDs so an adapter can avoid draining foreign work."""
        return tuple(result.command_id for result in self._deferred)

    def has_active(self) -> bool:
        return self._active is not None

    def active_command(self) -> Optional[AdmittedCommand]:
        return self._active

    def drain_deferred_results(self) -> Tuple[CompletionResult, ...]:
        results = tuple(self._deferred)
        self._deferred.clear()
        self._assert_budget()
        return results

    def _allocate_id(self) -> int:
        command_id = self._next_command_id
        self._next_command_id += 1
        return command_id

    def _read_clock(self) -> Optional[float]:
        """Read one finite clock value without retaining clock history."""
        try:
            value = self._clock()
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        try:
            now = float(value)
        except (OverflowError, TypeError, ValueError):
            return None
        return now if math.isfinite(now) else None


    @staticmethod
    def _is_stop(command: AdmittedCommand) -> bool:
        return command.request.command is ProtocolCommand.STOP

    @staticmethod
    def _is_expired(command: AdmittedCommand, now: float) -> bool:
        if not math.isfinite(now) or not math.isfinite(command.enqueued_at):
            return True
        return now < command.enqueued_at or now - command.enqueued_at >= COMMAND_TTL_SECONDS


    def _admitted_count(self) -> int:
        return len(self._pending) + (1 if self._active is not None else 0)

    def _stop_displacement_candidates(self, motor_id: str) -> List[AdmittedCommand]:
        same_motor = [
            command
            for command in self._pending
            if command.request.motor_id == motor_id
            and command.request.command is not ProtocolCommand.STOP
        ]
        if same_motor:
            return same_motor
        if self._admitted_count() < MAX_QUEUE_DEPTH:
            return []

        jog = next(
            (
                command
                for command in self._pending
                if command.request.command is ProtocolCommand.JOG
            ),
            None,
        )
        if jog is not None:
            return [jog]
        non_stop = next(
            (
                command
                for command in self._pending
                if command.request.command is not ProtocolCommand.STOP
            ),
            None,
        )
        return [] if non_stop is None else [non_stop]

    def _append_deferred(self, result: Result, command_id: int) -> None:
        if len(self._deferred) >= MAX_TRACKED_RECORDS:
            raise RuntimeError("deferred terminal-result budget exhausted")
        self._deferred.append(CompletionResult(result, command_id))
        self._assert_budget()

    def _preempt(self, commands: Iterable[AdmittedCommand]) -> None:
        preempted = tuple(commands)
        preempted_ids = {command.id for command in preempted}
        self._pending = [command for command in self._pending if command.id not in preempted_ids]
        for command in preempted:
            self._append_deferred(Result.PREEMPTED, command.id)

    def _purge_expired(self, now: float) -> None:
        expired = [command for command in self._pending if self._is_expired(command, now)]
        if not expired:
            return
        self._pending = [command for command in self._pending if command not in expired]
        for command in expired:
            self._append_deferred(Result.EXPIRED, command.id)

    def _reset_active(self) -> None:
        self._active = None
        self._write_attempted = False

    def _clear_ownership(self, result: Result) -> None:
        pending = tuple(self._pending)
        active = self._active
        active_write_attempted = self._write_attempted
        self._pending.clear()
        self._reset_active()
        for command in pending:
            self._append_deferred(result, command.id)
        if active is not None:
            final = (
                Result.UNKNOWN_AFTER_WRITE if active_write_attempted else result
            )
            self._append_deferred(final, active.id)
        self._assert_budget()

    def _terminate_motor(self, motor_id: str, result: Result) -> None:
        removed = [
            command for command in self._pending if command.request.motor_id == motor_id
        ]
        self._pending = [
            command for command in self._pending if command.request.motor_id != motor_id
        ]
        active = None
        active_write_attempted = False
        if self._active is not None and self._active.request.motor_id == motor_id:
            active = self._active
            active_write_attempted = self._write_attempted
            self._reset_active()
        for command in removed:
            self._append_deferred(result, command.id)
        if active is not None:
            final = (
                Result.UNKNOWN_AFTER_WRITE if active_write_attempted else result
            )
            self._append_deferred(final, active.id)
        self._assert_budget()

    def _assert_budget(self) -> None:
        admitted = self._admitted_count()
        if admitted > MAX_QUEUE_DEPTH:
            raise RuntimeError("safety admitted-command queue exceeded")
        tracked = len(self._deferred) + admitted
        if tracked > MAX_TRACKED_RECORDS:
            raise RuntimeError("safety command/result budget exceeded")
