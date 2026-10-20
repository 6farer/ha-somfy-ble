"""Shared serialized execution runtime for all Somfy motors in one HA domain."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import math
import time
from typing import Any, Callable

from .ble import BleTransport, TransportError
from .config_file import (
    CONFIG_MAX_AGE_SECONDS,
    OP_READ,
    OPEN_FOR_WRITE,
    READ_SEQUENCE,
    check_write,
    decode_file,
    setting_value,
    write_payload,
)
from .const import EXECUTION_CANCEL_TIMEOUT, BLE_SESSION_IDLE_TIMEOUT
from .entity import (
    RuntimeState,
    _safe_command_id,
    _safe_label,
    safe_error,
    safe_recovery,
    safe_result,
    safe_status,
)
from .models import (
    MAX_QUEUE_DEPTH,
    MAX_TRACKED_RECORDS,
    BeginResult,
    CompletionResult,
    CommandRequest,
    EnqueueResult,
    Result,
)
from .safety import Controller
from .protocol import Command


@dataclass(slots=True)
class MotorRuntime:
    """Private per-motor transport configuration owned by the shared runtime."""

    motor_id: str
    transport: BleTransport = field(repr=False)
    address: str | None = field(repr=False, default=None)
    pin: str | None = field(repr=False, default=None)
    display_name: str = field(repr=False, default="")


class Coordinator:
    """Own one global execution lock and resolve each command's motor at runtime."""

    __slots__ = (
        "_controller",
        "_motors",
        "_shutdown",
        "_execution_task",
        "_transport_waiters",
        "_session_motor_id",
    )

    def __init__(self, controller: Controller) -> None:
        self._controller = controller
        self._motors: dict[str, MotorRuntime] = {}
        self._shutdown = False
        self._execution_task: asyncio.Task[Any] | None = None
        self._transport_waiters: dict[int, asyncio.Task[Any]] = {}
        self._session_motor_id: str | None = None

    @property
    def controller(self) -> Controller:
        return self._controller

    @property
    def motor_ids(self) -> tuple[str, ...]:
        return tuple(self._motors)

    def register_motor(
        self,
        motor_id: str,
        transport: BleTransport,
        address: str | None,
        pin: str | None,
        display_name: str,
    ) -> None:
        """Register one entry and refresh the Controller's configured registry."""
        if self._shutdown:
            raise RuntimeError("coordinator is shut down")
        if motor_id in self._motors:
            raise ValueError(f"motor already registered: {motor_id}")
        self._motors[motor_id] = MotorRuntime(
            motor_id=motor_id,
            transport=transport,
            address=address,
            pin=pin,
            display_name=display_name,
        )
        self._controller.set_configured_motors(self._motors)

    async def async_register_motor(
        self,
        motor_id: str,
        transport: BleTransport,
        address: str | None,
        pin: str | None,
        display_name: str,
    ) -> None:
        """Cancel any global transaction before adding a registry entry."""
        await self._cancel_execution()
        self.register_motor(motor_id, transport, address, pin, display_name)

    async def async_unregister_motor(self, motor_id: str) -> None:
        """Cancel global ownership before removing one motor from the registry."""
        if motor_id not in self._motors:
            return
        self._track_all_lingering_transports()
        await self._cancel_execution()
        await self.async_close_session(motor_id)
        self._motors.pop(motor_id, None)
        self._controller.set_configured_motors(self._motors)

    async def async_execute_next(self) -> CompletionResult:
        """Execute only the Controller-selected command under one domain lock."""
        self._track_all_lingering_transports()
        # Let a completed transport waiter run its done path before deciding
        # whether the global quiescence guard is still active.
        await asyncio.sleep(0)
        current = asyncio.current_task()
        if self._execution_task is not None or self._has_lingering_transport():
            return CompletionResult(Result.BUSY, 0)
        self._execution_task = current
        try:
            return await self._async_execute_next()
        finally:
            if self._execution_task is current:
                self._execution_task = None

    async def async_interrogate(
        self, motor_id: str
    ) -> tuple[Result, tuple[dict[str, object], ...]]:
        """Run read-only GATT discovery under the global BLE ownership lock."""
        self._track_all_lingering_transports()
        await asyncio.sleep(0)
        current = asyncio.current_task()
        if (
            self._execution_task is not None
            or self._has_lingering_transport()
            or self._controller.has_active()
            or self._controller.pending_count()
        ):
            return Result.BUSY, ()
        motor = self._motors.get(motor_id)
        if motor is None:
            return Result.NOT_FOUND, ()
        self._execution_task = current
        try:
            return await motor.transport.async_interrogate()
        finally:
            if self._execution_task is current:
                self._execution_task = None

    async def async_config_exchange(
        self, motor_id: str, payloads: tuple[bytes, ...]
    ) -> tuple[Result, dict[str, object]]:
        """Run motor-file operations under the global BLE ownership lock."""
        self._track_all_lingering_transports()
        await asyncio.sleep(0)
        current = asyncio.current_task()
        if (
            self._execution_task is not None
            or self._has_lingering_transport()
            or self._controller.has_active()
            or self._controller.pending_count()
        ):
            return Result.BUSY, {}
        motor = self._motors.get(motor_id)
        if motor is None or not motor.address or not motor.pin:
            return Result.NOT_FOUND, {}
        self._execution_task = current
        try:
            # A retained jog session would hold the motor's only connection.
            if self._session_motor_id is not None and not await self.async_close_session(
                self._session_motor_id
            ):
                return Result.BUSY, {}
            return await motor.transport.async_config_exchange(motor.pin, payloads)
        finally:
            if self._execution_task is current:
                self._execution_task = None

    async def _async_execute_next(self) -> CompletionResult:
        begun: BeginResult = self._controller.begin_next()
        if begun.command is None:
            return CompletionResult(begun.result, begun.command_id)
        command = begun.command
        if self._shutdown:
            return self._controller.complete(command.id, Result.ERROR)

        motor = self._motors.get(command.request.motor_id)
        if motor is None or not motor.address or not motor.pin:
            return self._controller.complete(command.id, Result.NOT_FOUND)

        if (
            self._session_motor_id is not None
            and self._session_motor_id != command.request.motor_id
        ):
            previous_motor_id = self._session_motor_id
            if not await self.async_close_session(previous_motor_id):
                return self._controller.complete(command.id, Result.ERROR)

        self._track_transport_if_lingering(motor.transport)

        transport_address = getattr(motor.transport, "address", motor.address)
        if transport_address != motor.address:
            return self._controller.complete(command.id, Result.STALE_CALLBACK)

        boundary_rejected = False

        def before_write() -> bool:
            nonlocal boundary_rejected
            if not self._controller.mark_write_attempted(command.id):
                boundary_rejected = True
                raise TransportError(Result.STALE_CALLBACK)
            return True

        try:
            retain_connection = command.request.command in (
                Command.JOG,
                Command.RANGE_START,
                Command.SET_DIRECTION_CLOCKWISE,
                Command.SET_DIRECTION_COUNTERCLOCKWISE,
            )
            transport_result = await motor.transport.async_execute(
                command.packet,
                motor.pin,
                before_write,
                retain_connection=retain_connection,
            )
        except TransportError as error:
            transport_result = error.result
        except asyncio.CancelledError:
            self._controller.on_error(command.request.motor_id)
            raise
        except Exception:
            transport_result = Result.ERROR
        finally:
            self._track_transport_if_lingering(motor.transport)
        if boundary_rejected:
            return CompletionResult(Result.STALE_CALLBACK, command.id)
        if (
            transport_result is Result.SUCCESS
            and retain_connection
            and getattr(motor.transport, "has_session", False)
        ):
            self._session_motor_id = command.request.motor_id
        elif (
            self._session_motor_id == command.request.motor_id
            and not getattr(motor.transport, "has_session", False)
        ):
            self._session_motor_id = None
        return self._controller.complete(command.id, transport_result)

    async def async_wait_quiescent(self) -> None:
        """Wait for detached transport operations without polling."""
        while True:
            self._track_all_lingering_transports()
            waiters = tuple(self._transport_waiters.values())
            if not waiters:
                return
            await asyncio.wait(waiters)

    async def async_close_session(self, motor_id: str) -> bool:
        """Close one motor's retained authenticated connection, if present."""
        motor = self._motors.get(motor_id)
        if motor is None or not getattr(motor.transport, "has_session", False):
            if self._session_motor_id == motor_id:
                self._session_motor_id = None
            return True
        close = getattr(motor.transport, "async_close_session", None)
        if not callable(close):
            return False
        try:
            result = await close()
        except asyncio.CancelledError:
            raise
        except Exception:
            result = False
        finally:
            self._track_transport_if_lingering(motor.transport)
        if result is True and self._session_motor_id == motor_id:
            self._session_motor_id = None
        return result is True

    async def async_shutdown(self) -> None:
        """Stop execution and release all Controller ownership exactly once."""
        if self._shutdown:
            return
        self._shutdown = True
        self._track_all_lingering_transports()
        await self._cancel_execution()
        for motor_id in tuple(self._motors):
            await self.async_close_session(motor_id)
        self._track_all_lingering_transports()
        self._controller.on_config_removed()
        self._motors.clear()

    async def _cancel_execution(self) -> None:
        running = self._execution_task
        current = asyncio.current_task()
        if running is None or running is current or running.done():
            return
        if running.cancelling() == 0:
            running.cancel()
        cancelled = False
        deadline = asyncio.get_running_loop().time() + EXECUTION_CANCEL_TIMEOUT
        while not running.done():
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                await asyncio.wait({running}, timeout=remaining)
            except asyncio.CancelledError:
                cancelled = True
                continue
            break
        if running.done():
            self._consume_task_exception(running)
        else:
            # Keep _execution_task pointing at the live task. New work remains
            # BUSY until the stubborn transport actually exits.
            running.add_done_callback(self._consume_task_exception)
        self._track_all_lingering_transports()
        if cancelled:
            raise asyncio.CancelledError

    def _track_all_lingering_transports(self) -> None:
        for motor in tuple(self._motors.values()):
            self._track_transport_if_lingering(motor.transport)

    def _track_transport_if_lingering(self, transport: object) -> None:
        has_lingering = getattr(transport, "has_lingering_operations", False)
        if callable(has_lingering):
            has_lingering = has_lingering()
        if not has_lingering:
            return
        key = id(transport)
        existing = self._transport_waiters.get(key)
        if existing is not None and not existing.done():
            return
        wait_quiescent = getattr(transport, "async_wait_quiescent", None)
        if not callable(wait_quiescent):
            return
        waiter = asyncio.create_task(wait_quiescent())
        self._transport_waiters[key] = waiter
        waiter.add_done_callback(
            lambda task, transport_key=key: self._transport_quiescent(
                transport_key, task
            )
        )

    def _has_lingering_transport(self) -> bool:
        for key, task in tuple(self._transport_waiters.items()):
            if task.done():
                self._transport_quiescent(key, task)
        return bool(self._transport_waiters)

    def _transport_quiescent(
        self, key: int, task: asyncio.Task[Any]
    ) -> None:
        if self._transport_waiters.get(key) is task:
            self._transport_waiters.pop(key, None)
        self._consume_task_exception(task)

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass


@dataclass(slots=True)
class _RuntimeMotorState:
    """Bounded volatile presentation state for one configured motor."""

    motor_id: str
    transport: object = field(repr=False)
    jog_step: int = 100
    callbacks: set[Callable[[], None]] = field(default_factory=set, repr=False)
    last_result: Result | None = None
    last_error: str | None = None
    last_command_id: int | None = None
    selected_source: str = "none"
    reachable: bool = False
    status: str = "idle"
    recovery_state: str = "unknown"
    last_contact_at: float | None = None
    had_failure: bool = False
    session_idle_handle: asyncio.TimerHandle | None = field(default=None, repr=False)
    config: dict[str, Any] | None = field(default=None, repr=False)
    config_at: float | None = None


class SharedRuntime:
    """Domain-level entity adapter and automatic serialized execution worker."""

    __slots__ = (
        "controller",
        "coordinator",
        "_motors",
        "_command_motors",
        "_wake_event",
        "_idle_event",
        "_drain_task",
        "_session_close_tasks",
        "_shutdown",
    )

    def __init__(self) -> None:
        self.controller = Controller(time.monotonic)
        self.coordinator = Coordinator(self.controller)
        self._motors: dict[str, _RuntimeMotorState] = {}
        self._command_motors: dict[int, str] = {}
        self._wake_event = asyncio.Event()
        self._idle_event = asyncio.Event()
        self._idle_event.set()
        self._drain_task: asyncio.Task[Any] | None = None
        self._session_close_tasks: set[asyncio.Task[Any]] = set()
        self._shutdown = False

    def __repr__(self) -> str:
        return f"<SharedRuntime motors={len(self._motors)} shutdown={self._shutdown}>"

    def register_motor(
        self,
        motor_id: str,
        transport: BleTransport,
        address: str | None,
        pin: str | None,
        display_name: str,
    ) -> None:
        self.coordinator.register_motor(
            motor_id, transport, address, pin, display_name
        )
        self._motors[motor_id] = _RuntimeMotorState(motor_id, transport)

    async def async_register_motor(
        self,
        motor_id: str,
        transport: BleTransport,
        address: str | None,
        pin: str | None,
        display_name: str,
    ) -> None:
        await self._async_cancel_worker()
        await self.coordinator.async_register_motor(
            motor_id, transport, address, pin, display_name
        )
        self._motors[motor_id] = _RuntimeMotorState(motor_id, transport)
        self._publish_deferred_results()
        self._ensure_drain_task()

    def subscribe(
        self, motor_id: str, callback: Callable[[], None]
    ) -> Callable[[], None]:
        state = self._motors.get(motor_id)
        if state is None:
            raise ValueError("motor is not configured")
        state.callbacks.add(callback)
        removed = False

        def unsubscribe() -> None:
            nonlocal removed
            if removed:
                return
            removed = True
            state.callbacks.discard(callback)

        return unsubscribe

    def enqueue(self, request: CommandRequest) -> EnqueueResult:
        result = self.controller.enqueue(request)
        state = self._motors.get(request.motor_id)
        if state is not None:
            self._record_admission(state, result)
        if result.result is Result.SUCCESS or self.controller.deferred_count():
            self._idle_event.clear()
            self._wake_event.set()
        self._ensure_drain_task()
        return result


    def get_jog_step(self, motor_id: str) -> int:
        state = self._motors.get(motor_id)
        return state.jog_step if state is not None else 100

    def set_jog_step(self, motor_id: str, value: int) -> Result:
        state = self._motors.get(motor_id)
        if state is None:
            return Result.NOT_FOUND
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 500:
            return Result.INVALID_PAYLOAD
        state.jog_step = value
        self._notify(state)
        return Result.SUCCESS

    def snapshot(self, motor_id: str) -> RuntimeState:
        state = self._motors.get(motor_id)
        if state is None:
            return RuntimeState(
                last_result=Result.NOT_FOUND,
                status="unknown",
                selected_source="none",
                last_error=Result.NOT_FOUND.value,
            )
        age: float | None = None
        if state.last_contact_at is not None:
            now = self._now()
            if now is not None:
                age = max(0.0, now - state.last_contact_at)
        return RuntimeState(
            reachable=state.reachable,
            last_result=state.last_result,
            status=safe_status(state.status),
            selected_source=_safe_label(state.selected_source, fallback="none"),
            last_error=safe_error(state.last_error) if state.last_error else None,
            last_command_id=_safe_command_id(state.last_command_id),
            last_contact_age=age,
        )

    def diagnostics_snapshot(self, motor_id: str) -> dict[str, Any]:
        state = self.snapshot(motor_id)
        active = self.controller.active_command()
        active_id = (
            active.id
            if active is not None and active.request.motor_id == motor_id
            else None
        )
        return {
            "reachable": state.reachable,
            "last_result": safe_result(state.last_result) if state.last_result else "unknown",
            "status": safe_status(state.status),
            "selected_source": _safe_label(state.selected_source, fallback="none"),
            "last_error": safe_error(state.last_error) if state.last_error else "none",
            "last_command_id": _safe_command_id(state.last_command_id),
            "last_contact_age": state.last_contact_age,
            "queue_depth": min(MAX_QUEUE_DEPTH, self.controller.pending_count()),
            "active_command_id": _safe_command_id(active_id),
            "recovery_state": safe_recovery(
                self._motors[motor_id].recovery_state
                if motor_id in self._motors
                else "unknown"
            ),
        }

    async def async_interrogate(
        self, motor_id: str
    ) -> tuple[Result, tuple[dict[str, object], ...]]:
        """Interrogate one motor and publish only bounded connection state."""
        state = self._motors.get(motor_id)
        if state is None:
            return Result.NOT_FOUND, ()
        result, report = await self.coordinator.async_interrogate(motor_id)
        self._record_probe(state, result)
        return result, report

    async def async_read_config(self, motor_id: str) -> dict[str, Any]:
        """Read the motor configuration file; keep it to gate later writes."""
        state = self._motors.get(motor_id)
        if state is None:
            return {"result": Result.NOT_FOUND.value}
        result, trace = await self.coordinator.async_config_exchange(motor_id, READ_SEQUENCE)
        self._record_probe(state, result)
        data = b"".join(
            bytes.fromhex(chunk)
            for step in trace.get("steps", ())
            if step["sent"] == bytes((OP_READ,)).hex()
            for chunk in step["reads"]
        )
        size = trace.get("size")
        config: dict[str, Any] | None = None
        offset = -1
        # Only a successful exchange that returned the whole file counts.
        if result is Result.SUCCESS and isinstance(size, int) and len(data) >= size:
            config, offset = decode_file(data[:size])
        if config is None and result is Result.SUCCESS:
            result = Result.ERROR
        state.config = config
        state.config_at = time.monotonic() if config is not None else None
        return {"result": result.value, "config": config, "cbor_offset": offset, "trace": trace}

    async def async_write_config(self, motor_id: str, key: str, value: Any) -> dict[str, Any]:
        """Write one allowlisted parameter that a recent read showed, then re-read it."""
        state = self._motors.get(motor_id)
        if state is None:
            return {"result": Result.NOT_FOUND.value}
        fresh = state.config_at is not None and (
            time.monotonic() - state.config_at < CONFIG_MAX_AGE_SECONDS
        )
        config = state.config if fresh else None
        if (reason := check_write(config, key, value)) is not None:
            return {"result": Result.INVALID_PAYLOAD.value, "error": reason}
        assert config is not None
        before = config[key]
        result, trace = await self.coordinator.async_config_exchange(
            motor_id, (OPEN_FOR_WRITE, write_payload(key, value))
        )
        self._record_probe(state, result)
        # Any attempted write makes the stored copy stale.
        state.config = None
        state.config_at = None
        response: dict[str, Any] = {
            "result": result.value,
            "key": key,
            "before": before,
            "trace": trace,
        }
        # The write acknowledgment is unreliable; only a re-read confirms it.
        if result not in (Result.BUSY, Result.NOT_FOUND):
            reread = await self.async_read_config(motor_id)
            response["reread_result"] = reread["result"]
            response["after"] = (reread.get("config") or {}).get(key)
            response["confirmed"] = (
                reread["result"] == Result.SUCCESS.value
                and setting_value(response["after"]) == value
            )
        return response

    def _record_probe(self, state: _RuntimeMotorState, result: Result) -> None:
        """Publish connection state after an interrogation or config exchange."""
        state.last_result = result
        state.last_error = None if result is Result.SUCCESS else safe_error(result)
        if result is Result.SUCCESS:
            state.reachable = True
            state.status = "connected"
            state.recovery_state = "healthy"
            state.had_failure = False
            source = getattr(state.transport, "selected_source", None)
            state.selected_source = _safe_label(source, fallback="none")
            state.last_contact_at = self._now()
        elif result not in (Result.BUSY,):
            state.status = "degraded"
            state.recovery_state = "failure"
            state.had_failure = True
        self._notify(state)

    async def async_wait_idle(self) -> None:
        """Wait for admitted work, deferred results, and detached BLE work to quiesce."""
        if self._drain_task is None and (
            self.controller.pending_count() or self._has_owned_deferred()
        ):
            self._ensure_drain_task()
        await self._idle_event.wait()
        await asyncio.sleep(0)
        session_tasks = tuple(self._session_close_tasks)
        if session_tasks:
            await asyncio.wait(session_tasks)

    async def async_unregister_motor(self, motor_id: str) -> bool:
        if motor_id not in self._motors:
            return False
        await self._async_cancel_worker()
        await self.coordinator.async_unregister_motor(motor_id)
        self._publish_deferred_results()
        state = self._motors.pop(motor_id)
        self._cancel_session_idle_handle(state)
        state.callbacks.clear()
        if self.coordinator.motor_ids:
            self._ensure_drain_task()
            return False
        await self.coordinator.async_shutdown()
        self._publish_deferred_results()
        self._shutdown = True
        self._wake_event.set()
        self._command_motors.clear()
        self._motors.clear()
        return True

    async def async_shutdown(self) -> None:
        if self._shutdown:
            return
        await self._async_cancel_worker()
        session_tasks = tuple(self._session_close_tasks)
        if session_tasks:
            await asyncio.wait(session_tasks)
        await self.coordinator.async_shutdown()
        self._publish_deferred_results()
        self._shutdown = True
        self._wake_event.set()
        for state in self._motors.values():
            self._cancel_session_idle_handle(state)
            state.callbacks.clear()
        self._motors.clear()
        self._command_motors.clear()

    def _record_admission(
        self, state: _RuntimeMotorState, result: EnqueueResult
    ) -> None:
        state.last_result = result.result
        state.last_command_id = result.command_id
        state.last_error = None if result.result is Result.SUCCESS else safe_error(result.result)
        if result.result is Result.SUCCESS:
            self._cancel_session_idle_handle(state)
            if len(self._command_motors) >= MAX_TRACKED_RECORDS:
                state.last_result = Result.BUSY
            else:
                self._command_motors[result.command_id] = state.motor_id
        self._notify(state)


    def _publish_deferred_results(self) -> None:
        deferred_ids = self.controller.deferred_command_ids()
        if not deferred_ids or any(
            command_id not in self._command_motors for command_id in deferred_ids
        ):
            return
        for result in self.controller.drain_deferred_results():
            self._publish_terminal(result, attempted=result.result is Result.UNKNOWN_AFTER_WRITE)

    def _publish_terminal(self, result: CompletionResult, *, attempted: bool) -> None:
        motor_id = self._command_motors.pop(result.command_id, None)
        if motor_id is None:
            return
        state = self._motors.get(motor_id)
        if state is None:
            return
        self._record_terminal(state, result, attempted=attempted)

    def _record_terminal(
        self,
        state: _RuntimeMotorState,
        result: CompletionResult,
        *,
        attempted: bool,
    ) -> None:
        state.last_result = result.result
        state.last_command_id = result.command_id
        if attempted:
            source = getattr(state.transport, "selected_source", None)
            if isinstance(source, str) and source:
                state.selected_source = _safe_label(source, fallback="other")
                state.reachable = True
                now = self._now()
                state.last_contact_at = now
            else:
                state.selected_source = "none"
                state.reachable = False
        if result.result is Result.SUCCESS:
            state.last_error = None
            state.status = "ready"
            state.recovery_state = "recovery" if state.had_failure else "healthy"
            state.had_failure = False
        elif result.result is Result.SENT_UNCONFIRMED:
            state.last_error = None
            state.status = "sent_unconfirmed"
            state.recovery_state = "unknown"
            state.had_failure = False
        elif result.result is Result.PREEMPTED or result.result is Result.EXPIRED:
            state.last_error = safe_error(result.result)
        else:
            state.last_error = safe_error(result.result)
            if result.result in (Result.NOT_FOUND, Result.DISCONNECTED):
                state.status = "offline"
            else:
                state.status = "degraded"
            state.recovery_state = "failure"
            state.had_failure = True
        if getattr(state.transport, "has_session", False):
            self._schedule_session_idle(state.motor_id)
        else:
            self._cancel_session_idle_handle(state)
        self._notify(state)

    def _schedule_session_idle(self, motor_id: str) -> None:
        state = self._motors.get(motor_id)
        if state is None:
            return
        self._cancel_session_idle_handle(state)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        state.session_idle_handle = loop.call_later(
            BLE_SESSION_IDLE_TIMEOUT, self._expire_session, motor_id
        )

    def _expire_session(self, motor_id: str) -> None:
        state = self._motors.get(motor_id)
        if state is None:
            return
        state.session_idle_handle = None
        state.status = "idle"
        self._notify(state)
        self._schedule_session_close(motor_id)

    def _schedule_session_close(self, motor_id: str) -> None:
        """Schedule and track cleanup initiated by a synchronous timer callback."""
        try:
            task = asyncio.get_running_loop().create_task(
                self.coordinator.async_close_session(motor_id)
            )
        except RuntimeError:
            return
        self._session_close_tasks.add(task)
        task.add_done_callback(self._session_close_done)

    def _session_close_done(self, task: asyncio.Task[Any]) -> None:
        self._session_close_tasks.discard(task)
        self._consume_task(task)

    @staticmethod
    def _cancel_session_idle_handle(state: _RuntimeMotorState) -> None:
        handle = state.session_idle_handle
        state.session_idle_handle = None
        if handle is not None:
            handle.cancel()

    def _has_owned_deferred(self) -> bool:
        deferred_ids = self.controller.deferred_command_ids()
        return bool(deferred_ids) and all(
            command_id in self._command_motors for command_id in deferred_ids
        )

    def _ensure_drain_task(self) -> None:
        if self._shutdown:
            return
        if not (self.controller.pending_count() or self._has_owned_deferred()):
            return
        if self._drain_task is not None and not self._drain_task.done():
            return
        if self._drain_task is not None:
            self._consume_task(self._drain_task)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._idle_event.clear()
        self._drain_task = loop.create_task(self._async_drain_worker())

    async def _async_drain_worker(self) -> None:
        while not self._shutdown:
            self._publish_deferred_results()
            if self.controller.pending_count():
                try:
                    closing = tuple(self._session_close_tasks)
                    if closing:
                        await asyncio.wait(closing)
                    result = await self.coordinator.async_execute_next()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    active = self.controller.active_command()
                    if active is not None:
                        self.controller.on_error(active.request.motor_id)
                    self._publish_deferred_results()
                    continue
                if result.result is Result.BUSY:
                    await self.coordinator.async_wait_quiescent()
                    continue
                self._publish_terminal(result, attempted=True)
                continue

            await self.coordinator.async_wait_quiescent()
            self._publish_deferred_results()
            if self.controller.pending_count() or self._has_owned_deferred():
                continue
            self._idle_event.set()
            self._wake_event.clear()
            await self._wake_event.wait()
            self._idle_event.clear()

    async def _async_cancel_worker(self) -> None:
        task = self._drain_task
        if task is None or task.done() or task is asyncio.current_task():
            if task is not None:
                self._consume_task(task)
            self._drain_task = None
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        finally:
            self._consume_task(task)
            if self._drain_task is task:
                self._drain_task = None

    def _notify(self, state: _RuntimeMotorState) -> None:
        for callback in tuple(state.callbacks):
            try:
                callback()
            except Exception:
                continue

    @staticmethod
    def _consume_task(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    @staticmethod
    def _now() -> float | None:
        try:
            value = time.monotonic()
            return (
                value
                if isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
                else None
            )
        except Exception:
            return None


__all__ = ["Coordinator", "MotorRuntime", "SharedRuntime"]
