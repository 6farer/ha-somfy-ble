"""Proxy-aware, single-transaction Somfy BLE transport."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from typing import Any, Awaitable, Callable

import bleak_retry_connector

from .const import (
    BLE_AUTH_SETTLE_DELAY,
    BLE_AUTH_TIMEOUT,
    BLE_COMMAND_TIMEOUT,
    BLE_CONNECT_TIMEOUT,
    BLE_DISCOVERY_TIMEOUT,
    BLE_DISCONNECT_TIMEOUT,
)
from .config_file import (
    CLOSE,
    FILE_STATUS_INTERVAL,
    FILE_STATUS_POLLS,
    MAX_FILE_BYTES,
    MAX_READ_CHUNKS,
    OP_CLOSE,
    OP_OPEN,
    OP_READ,
    OP_SIZE,
    OP_WRITE,
    STATUS_UUID,
    STEP_DELAY,
    TRANSFER_UUID,
    redact,
)
from .models import Result
from .protocol import Characteristic, CommandPacket, encode_pin, somfy_uuid


@dataclass(frozen=True, slots=True)
class BleDiscovery:
    """The one device/source snapshot used for a complete BLE transaction."""

    device: object
    source: str | None
    metadata: object | None = None


class TransportError(Exception):
    """A pre-command transport failure with a machine-readable result."""

    def __init__(self, result: Result) -> None:
        self.result = result
        super().__init__(result.value)


DiscoveryCallable = Callable[[], BleDiscovery | None | Awaitable[BleDiscovery | None]]
ConnectorCallable = Callable[..., object | Awaitable[object]]
BoundaryCallable = Callable[[], object]
AbandonedResultCallable = Callable[[Any], object]
SleeperCallable = Callable[[float], Awaitable[None]]


class BleTransport:
    """Connect, authenticate, settle, and perform one acknowledged GATT write.

    Bluetooth discovery APIs are deliberately resolved lazily so this module can
    be unit-tested without starting Home Assistant's Bluetooth integration. The
    injected discovery and connector seams also keep proxy selection deterministic.
    """

    __slots__ = (
        "_hass",
        "_address",
        "_display_name",
        "_motor_id",
        "_discovery",
        "_connector",
        "_sleeper",
        "_session_client",
        "_selected_device",
        "_selected_source",
        "_disconnect_client",
        "_disconnect_called",
        "_disconnect_task",
        "_lingering_operations",
    )

    def __init__(
        self,
        hass: object,
        address: str,
        *,
        display_name: str = "",
        motor_id: str = "motor",
        discovery: DiscoveryCallable | None = None,
        connector: ConnectorCallable | None = None,
        sleeper: SleeperCallable = asyncio.sleep,
    ) -> None:
        self._hass = hass
        self._address = address
        self._display_name = display_name
        self._motor_id = motor_id
        self._discovery = discovery or self._async_discover_from_home_assistant
        self._connector = connector or self._async_establish_connection
        self._sleeper = sleeper
        self._session_client: object | None = None
        self._selected_device: object | None = None
        self._selected_source: str | None = None
        self._disconnect_client: object | None = None
        self._disconnect_called = False
        self._disconnect_task: asyncio.Task[Any] | None = None
        self._lingering_operations: set[asyncio.Task[Any]] = set()

    @property
    def address(self) -> str:
        return self._address

    @property
    def selected_device(self) -> object | None:
        return self._selected_device

    @property
    def selected_source(self) -> str | None:
        return self._selected_source

    @property
    def has_session(self) -> bool:
        """Whether this motor currently owns an authenticated BLE connection."""
        return self._session_client is not None

    @property
    def has_lingering_operations(self) -> bool:
        """Whether a canceled phase or cleanup operation is still running."""
        self._prune_lingering_operations()
        return bool(self._lingering_operations)

    async def async_wait_quiescent(self) -> None:
        """Wait until all detached phase and cleanup tasks have really exited."""
        while True:
            self._prune_lingering_operations()
            if not self._lingering_operations:
                return
            await asyncio.wait(tuple(self._lingering_operations))

    async def async_execute(
        self,
        packet: CommandPacket,
        pin: str,
        before_write: BoundaryCallable,
        *,
        retain_connection: bool = False,
    ) -> Result:
        """Run one transaction; ``before_write`` is the command boundary.

        The credential is encoded into a local variable and is never retained by
        this object. Once ``before_write`` returns, every failure is ambiguous to
        the caller and the coordinator owns conversion to UNKNOWN_AFTER_WRITE.
        """
        client: object | None = None
        boundary_reached = False
        result: Result | None = None
        caller_cancelled = False
        if self.has_lingering_operations:
            return Result.BUSY
        reused_session = self._session_client is not None
        if reused_session:
            client = self._session_client
        else:
            self._selected_device = None
            self._selected_source = None
            self._disconnect_client = None
            self._disconnect_called = False
            self._disconnect_task = None
        try:
            if not reused_session:
                snapshot = await self._with_timeout(
                    self._resolve_discovery(), BLE_DISCOVERY_TIMEOUT
                )
                if snapshot is None:
                    raise TransportError(Result.NOT_FOUND)
                self._selected_device = snapshot.device
                self._selected_source = snapshot.source

                try:
                    client = await self._with_timeout(
                        self._call_connector(snapshot.device),
                        BLE_CONNECT_TIMEOUT,
                        abandoned_result_callback=self._abandon_connector_result,
                    )
                except TransportError:
                    raise
                except asyncio.TimeoutError as error:
                    raise TransportError(Result.TIMEOUT_BEFORE_WRITE) from error
                except bleak_retry_connector.BleakOutOfConnectionSlotsError as error:
                    raise TransportError(Result.BUSY) from error
                except bleak_retry_connector.BleakNotFoundError as error:
                    raise TransportError(Result.NOT_FOUND) from error
                except Exception as error:
                    raise TransportError(Result.ERROR) from error

            assert client is not None
            self._disconnect_client = client
            service, auth_characteristic, command_characteristic = self._get_characteristics(
                client, packet.characteristic_uuid
            )
            del service
            auth_acknowledged = True
            if not reused_session:
                try:
                    pin_bytes = encode_pin(pin)
                except (TypeError, ValueError) as error:
                    raise TransportError(Result.INVALID_PAYLOAD) from error
                try:
                    auth_acknowledged = await self._with_timeout(
                        self._write(client, auth_characteristic, pin_bytes),
                        BLE_AUTH_TIMEOUT,
                    )
                except asyncio.TimeoutError as error:
                    raise TransportError(Result.TIMEOUT_BEFORE_WRITE) from error
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    raise TransportError(Result.AUTH_FAILED) from error

                await self._sleeper(BLE_AUTH_SETTLE_DELAY)

            try:
                boundary_result = before_write()
                if boundary_result is not True:
                    raise TransportError(Result.STALE_CALLBACK)
                boundary_reached = True
            except TransportError:
                raise
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise TransportError(Result.ERROR) from error

            try:
                command_acknowledged = await self._with_timeout(
                    self._write(client, command_characteristic, packet.payload),
                    BLE_COMMAND_TIMEOUT,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise TransportError(Result.ERROR) from error
            result = (
                Result.SUCCESS
                if auth_acknowledged and command_acknowledged
                else Result.SENT_UNCONFIRMED
            )
        except TransportError as error:
            result = error.result
        except asyncio.TimeoutError:
            result = Result.TIMEOUT_BEFORE_WRITE if not boundary_reached else Result.ERROR
        except asyncio.CancelledError:
            caller_cancelled = True
            raise
        except Exception:
            result = Result.ERROR
        finally:
            if client is not None:
                if (
                    retain_connection
                    and result is Result.SUCCESS
                    and not caller_cancelled
                ):
                    self._session_client = client
                else:
                    if self._session_client is client:
                        self._session_client = None
                    cleanup_succeeded = await self._cleanup_shielded(
                        client, immediate=caller_cancelled
                    )
                    if not cleanup_succeeded and boundary_reached:
                        result = Result.ERROR

        assert result is not None
        return result

    async def async_close_session(self) -> bool:
        """Disconnect and clear the retained authenticated client, if any."""
        client = self._session_client
        if client is None:
            return True
        self._session_client = None
        return await self._cleanup_shielded(client)

    async def async_interrogate(self) -> tuple[Result, tuple[dict[str, object], ...]]:
        """Connect and enumerate GATT metadata without authentication or I/O."""
        client: object | None = None
        report: tuple[dict[str, object], ...] = ()
        result = Result.ERROR
        if self.has_lingering_operations:
            return Result.BUSY, report
        self._selected_device = None
        self._selected_source = None
        self._disconnect_client = None
        self._disconnect_called = False
        self._disconnect_task = None
        try:
            snapshot = await self._with_timeout(
                self._resolve_discovery(), BLE_DISCOVERY_TIMEOUT
            )
            if snapshot is None:
                raise TransportError(Result.NOT_FOUND)
            self._selected_device = snapshot.device
            self._selected_source = snapshot.source
            try:
                client = await self._with_timeout(
                    self._call_connector(snapshot.device),
                    BLE_CONNECT_TIMEOUT,
                    abandoned_result_callback=self._abandon_connector_result,
                )
            except asyncio.TimeoutError as error:
                raise TransportError(Result.TIMEOUT_BEFORE_WRITE) from error
            except bleak_retry_connector.BleakOutOfConnectionSlotsError as error:
                raise TransportError(Result.BUSY) from error
            except bleak_retry_connector.BleakNotFoundError as error:
                raise TransportError(Result.NOT_FOUND) from error
            except TransportError:
                raise
            except Exception as error:
                raise TransportError(Result.ERROR) from error
            self._disconnect_client = client
            report = self._serialize_gatt_services(getattr(client, "services", None))
            result = Result.SUCCESS if report else Result.NOT_FOUND
        except TransportError as error:
            result = error.result
        except asyncio.CancelledError:
            raise
        except Exception:
            result = Result.ERROR
        finally:
            if client is not None and not await self._cleanup_shielded(client):
                result = Result.ERROR
        return result, report

    async def async_config_exchange(
        self, pin: str, payloads: tuple[bytes, ...]
    ) -> tuple[Result, dict[str, object]]:
        """Authenticate, then run motor-file operations on one connection.

        Every write, read and status notification is recorded as hex, because
        the reply framing is not yet confirmed. The first payload opens the
        file; once that is acknowledged, CLOSE is always sent.
        """
        steps: list[dict[str, object]] = []
        trace: dict[str, object] = {"mtu": None, "notify": "not subscribed", "steps": steps}
        notifications: list[str] = []
        client: object | None = None
        result = Result.ERROR
        if self.has_lingering_operations:
            return Result.BUSY, trace
        self._selected_device = None
        self._selected_source = None
        self._disconnect_client = None
        self._disconnect_called = False
        self._disconnect_task = None
        try:
            snapshot = await self._with_timeout(
                self._resolve_discovery(), BLE_DISCOVERY_TIMEOUT
            )
            if snapshot is None:
                raise TransportError(Result.NOT_FOUND)
            self._selected_device = snapshot.device
            self._selected_source = snapshot.source
            try:
                client = await self._with_timeout(
                    self._call_connector(snapshot.device),
                    BLE_CONNECT_TIMEOUT,
                    abandoned_result_callback=self._abandon_connector_result,
                )
            except asyncio.TimeoutError as error:
                raise TransportError(Result.TIMEOUT_BEFORE_WRITE) from error
            except bleak_retry_connector.BleakOutOfConnectionSlotsError as error:
                raise TransportError(Result.BUSY) from error
            except bleak_retry_connector.BleakNotFoundError as error:
                raise TransportError(Result.NOT_FOUND) from error
            except TransportError:
                raise
            except Exception as error:
                raise TransportError(Result.ERROR) from error
            self._disconnect_client = client
            trace["mtu"] = getattr(client, "mtu_size", None)

            get_characteristic = getattr(
                getattr(client, "services", None), "get_characteristic", None
            )
            if not callable(get_characteristic):
                raise TransportError(Result.NOT_FOUND)
            auth = get_characteristic(Characteristic.AUTH.uuid)
            transfer = get_characteristic(TRANSFER_UUID)
            status = get_characteristic(STATUS_UUID)
            if auth is None or transfer is None or status is None:
                raise TransportError(Result.NOT_FOUND)
            try:
                pin_bytes = encode_pin(pin)
            except (TypeError, ValueError) as error:
                raise TransportError(Result.INVALID_PAYLOAD) from error
            try:
                await self._with_timeout(
                    self._write(client, auth, pin_bytes), BLE_AUTH_TIMEOUT
                )
            except asyncio.TimeoutError as error:
                raise TransportError(Result.TIMEOUT_BEFORE_WRITE) from error
            except asyncio.CancelledError:
                raise
            except Exception as error:
                raise TransportError(Result.AUTH_FAILED) from error
            await self._sleeper(BLE_AUTH_SETTLE_DELAY)

            try:
                await self._with_timeout(
                    client.start_notify(
                        status, lambda _sender, data: notifications.append(bytes(data).hex())
                    ),
                    BLE_COMMAND_TIMEOUT,
                )
                trace["notify"] = "subscribed"
            except asyncio.CancelledError:
                raise
            except Exception as error:
                trace["notify"] = _error_text(error)

            ok = True
            size: int | None = None
            for payload in payloads:
                step = await self._config_step(
                    client, transfer, status, payload, notifications, size
                )
                steps.append(step)
                if step["error"] is not None:
                    ok = False
                    break
                if payload[:1] == bytes((OP_SIZE,)):
                    size = int.from_bytes(bytes.fromhex(step["reads"][0]), "little")
                    trace["size"] = size
            # The open write may fail its acknowledgment yet still open the file,
            # so close after any attempt, unless a timed-out operation may still
            # be running: then only the disconnect may follow it.
            if steps and not steps[-1].get("timed_out") and not self.has_lingering_operations:
                close = await self._config_step(client, transfer, status, CLOSE, notifications)
                steps.append(close)
                ok = ok and close["error"] is None
            result = Result.SUCCESS if ok else Result.ERROR
        except TransportError as error:
            result = error.result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            trace["error"] = _error_text(error)
            result = Result.ERROR
        finally:
            if client is not None and not await self._cleanup_shielded(client):
                result = Result.ERROR
        return result, trace

    async def _config_step(
        self,
        client: object,
        transfer: object,
        status: object,
        payload: bytes,
        notifications: list[str],
        size: int | None = None,
    ) -> dict[str, object]:
        """Write one file operation and judge it by the motor's reply.

        Motors have answered a file write with ATT 0x0E yet still acted on it
        (status went on to TRANSFERRED), so a failed acknowledgment is recorded
        but does not end the step by itself.
        """
        reads: list[str] = []
        step: dict[str, object] = {
            "sent": payload.hex(),
            "error": None,
            "write_error": None,
            "reads": reads,
            "status": None,
            "notifications": [],
        }
        seen = len(notifications)
        op = payload[0]
        try:
            try:
                await self._with_timeout(
                    self._write(client, transfer, payload), BLE_COMMAND_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # The write may still be in flight; no further I/O on this link.
                step["error"] = "write timed out"
                step["timed_out"] = True
                return step
            except Exception as error:
                step["write_error"] = _error_text(error)
            await self._sleeper(STEP_DELAY)
            if op in (OP_OPEN, OP_CLOSE):
                # The file is usable, or the write applied, only at 05xx.
                state = await self._file_state(client, status, notifications, seen)
                step["status"] = state
                if not state.startswith("05"):
                    step["error"] = f"file not ready: {state or 'no status'}"
            elif op == OP_WRITE and step["write_error"] is not None:
                # Status still shows the open's 05xx, so it cannot prove this write.
                step["error"] = f"write unconfirmed: {step['write_error']}"
            elif op in (OP_SIZE, OP_READ):
                # Size is one read; contents repeat reads until ``size`` bytes.
                want = (size or MAX_FILE_BYTES) if op == OP_READ else 1
                total = 0
                for _ in range(MAX_READ_CHUNKS if op == OP_READ else 1):
                    data = bytes(
                        await self._with_timeout(
                            client.read_gatt_char(transfer), BLE_COMMAND_TIMEOUT
                        )
                    )
                    if not data or (size is None and reads and data.hex() == reads[-1]):
                        break
                    reads.append(data.hex())
                    total += len(data)
                    if total >= min(want, MAX_FILE_BYTES):
                        break
                if not reads:
                    step["error"] = step["write_error"] or "no reply"
            else:
                state = bytes(
                    await self._with_timeout(
                        client.read_gatt_char(status), BLE_COMMAND_TIMEOUT
                    )
                ).hex()
                step["status"] = state
                if state[:2] not in ("03", "05"):
                    step["error"] = f"file transfer not progressing: {state or 'no status'}"
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            step["error"] = "read timed out"
            step["timed_out"] = True
        except Exception as error:
            step["error"] = _error_text(error)
        step["notifications"] = notifications[seen:]
        return step

    async def _file_state(
        self, client: object, status: object, notifications: list[str], seen: int
    ) -> str:
        """Poll file status until TRANSFERRED (05xx) or FAILURE (06xx)."""
        state = ""
        for _ in range(FILE_STATUS_POLLS):
            for note in reversed(notifications[seen:]):
                if note[:2] in ("05", "06"):
                    return note
            state = bytes(
                await self._with_timeout(
                    client.read_gatt_char(status), BLE_COMMAND_TIMEOUT
                )
            ).hex()
            if state[:2] in ("05", "06"):
                return state
            await self._sleeper(FILE_STATUS_INTERVAL)
        return state

    @staticmethod
    def _serialize_gatt_services(services: object) -> tuple[dict[str, object], ...]:
        """Return a bounded, credential-free GATT shape for operator display."""
        if services is None:
            return ()
        try:
            service_items = list(services)[:64]  # type: ignore[arg-type]
        except TypeError:
            values = getattr(getattr(services, "services", None), "values", None)
            service_items = list(values())[:64] if callable(values) else []
        report: list[dict[str, object]] = []
        characteristic_count = 0
        descriptor_count = 0
        for service in service_items:
            characteristics: list[dict[str, object]] = []
            for characteristic in list(
                getattr(service, "characteristics", ())
            ):
                if characteristic_count >= 256:
                    break
                characteristic_count += 1
                descriptors: list[str] = []
                for descriptor in list(getattr(characteristic, "descriptors", ())):
                    if descriptor_count >= 512:
                        break
                    descriptor_count += 1
                    descriptors.append(str(getattr(descriptor, "uuid", "unknown")))
                characteristics.append(
                    {
                        "uuid": str(getattr(characteristic, "uuid", "unknown")),
                        "properties": tuple(
                            sorted(str(value) for value in getattr(characteristic, "properties", ()))
                        ),
                        "descriptors": tuple(descriptors),
                    }
                )
            report.append(
                {
                    "uuid": str(getattr(service, "uuid", "unknown")),
                    "characteristics": tuple(characteristics),
                }
            )
        return tuple(report)

    async def _resolve_discovery(self) -> BleDiscovery | None:
        result = self._discovery()
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            return await result
        return result  # type: ignore[return-value]

    async def _call_connector(self, device: object) -> object:
        # Keep one pinned discovery snapshot and one bounded connector call.
        # Connector retries are allowed only before authentication; the outer
        # transaction remains bounded and no command write can be replayed.
        result = self._connector(
            bleak_retry_connector.BleakClientWithServiceCache,
            device,
            f"Somfy Bluetooth {self._safe_connector_label()}",
            max_attempts=3,
            use_services_cache=True,
            pair=False,
        )
        if asyncio.iscoroutine(result) or isinstance(result, Awaitable):
            result = await result
        # A connector is allowed to swallow CancelledError internally. Never
        # let a client produced after our owned task was canceled escape into
        # the transaction: disconnect it as an orphan and finish canceled.
        current = asyncio.current_task()
        if current is not None and current.cancelling():
            self._disconnect_client = result
            self._start_disconnect_task(result)
            raise asyncio.CancelledError
        return result

    def _safe_connector_label(self) -> str:
        """Return a bounded opaque label derived from motor-entry identity only."""
        identity = f"{self._motor_id}\x00{self._address}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()[:16]
        return f"motor-{digest}"

    @staticmethod
    async def _write(client: object, characteristic: object, payload: bytes) -> bool:
        """Write Somfy data and wait for the GATT write acknowledgment."""
        await client.write_gatt_char(characteristic, payload, response=True)
        return True

    @staticmethod
    def _get_characteristics(client: object, command_uuid: str) -> tuple[object, object, object]:
        services = getattr(client, "services", None)
        if services is None:
            raise TransportError(Result.NOT_FOUND)
        get_service = getattr(services, "get_service", None)
        service = get_service(somfy_uuid(0)) if callable(get_service) else None
        if service is None:
            raise TransportError(Result.NOT_FOUND)

        # Calibration lives in service 00010000; auth/Identify live in 00000000.
        # Resolve UUIDs across the whole GATT collection, not only the auth service.
        get_characteristic = getattr(services, "get_characteristic", None)
        if not callable(get_characteristic):
            raise TransportError(Result.NOT_FOUND)
        auth = get_characteristic(Characteristic.AUTH.uuid)
        command = get_characteristic(command_uuid)
        if auth is None or command is None:
            raise TransportError(Result.NOT_FOUND)
        return service, auth, command

    async def _async_discover_from_home_assistant(self) -> BleDiscovery | None:
        from homeassistant.components import bluetooth

        # Config flow stores a lowercase canonical identity, but HA's Bluetooth
        # manager indexes Bleak/proxy discoveries by uppercase address. Keep the
        # public transport identity unchanged for the coordinator's stale-callback
        # guard and normalize only the Home Assistant lookup key.
        lookup_address = self._address.upper()
        service_info = bluetooth.async_last_service_info(
            self._hass, lookup_address, connectable=True
        )
        if service_info is None:
            return None
        device = bluetooth.async_ble_device_from_address(
            self._hass, lookup_address, connectable=True
        )
        if device is None:
            return None
        details = getattr(device, "details", None)
        device_source = details.get("source") if isinstance(details, dict) else None
        source = (
            device_source
            if isinstance(device_source, str)
            else getattr(service_info, "source", None)
        )
        if not isinstance(source, str):
            source = None
        return BleDiscovery(device, source, service_info)

    async def _async_establish_connection(
        self,
        client_class: type[Any],
        device: object,
        name: str,
        **kwargs: object,
    ) -> object:
        return await bleak_retry_connector.establish_connection(
            client_class,
            device,
            name,
            **kwargs,
        )

    async def _with_timeout(
        self,
        awaitable: Awaitable[Any],
        timeout: float,
        *,
        abandoned_result_callback: AbandonedResultCallable | None = None,
    ) -> Any:
        """Run one owned phase with a hard parent-side deadline.

        ``asyncio.timeout`` cannot enforce a deadline when the child swallows
        cancellation. The child is intentionally not awaited after canceling;
        it remains strongly referenced and observable through quiescence until
        it eventually exits.
        """
        async def run_phase() -> Any:
            return await awaitable

        phase = asyncio.create_task(run_phase())
        self._track_operation(phase)

        def abandon_successful_result() -> None:
            if abandoned_result_callback is None or not phase.done():
                return
            try:
                result = phase.result()
            except BaseException:
                return
            try:
                abandoned_result_callback(result)
            except BaseException:
                pass

        try:
            done, _ = await asyncio.wait({phase}, timeout=timeout)
        except asyncio.CancelledError:
            if phase.done():
                abandon_successful_result()
            else:
                phase.cancel()
            raise
        if not done:
            if phase.done():
                abandon_successful_result()
            else:
                phase.cancel()
            raise asyncio.TimeoutError
        return phase.result()

    def _abandon_connector_result(self, client: object) -> None:
        self._start_disconnect_task(client)

    async def _cleanup_shielded(self, client: object, *, immediate: bool = False) -> bool:
        # Do not race a late auth/command write with its disconnect. The
        # cleanup task is detached immediately, but waits for those already
        # detached phase tasks before touching the client.
        phase_tasks = tuple(
            task for task in self._lingering_operations if not task.done()
        )
        cleanup = self._start_disconnect_task(client, wait_for=phase_tasks)
        if immediate:
            # Give the detached cleanup a couple of scheduling turns so a
            # cooperative cancellation starts disconnecting before the caller
            # resumes.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return False
        deadline = asyncio.get_running_loop().time() + BLE_DISCONNECT_TIMEOUT
        try:
            while not cleanup.done():
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                await asyncio.wait({cleanup}, timeout=remaining)
        except asyncio.CancelledError:
            raise
        if cleanup.done():
            return self._task_succeeded(cleanup)
        return False

    def _start_disconnect_task(
        self,
        client: object,
        *,
        wait_for: tuple[asyncio.Task[Any], ...] = (),
    ) -> asyncio.Task[Any]:
        if self._disconnect_task is not None:
            return self._disconnect_task
        self._disconnect_client = client
        cleanup = asyncio.create_task(
            self._disconnect_after_operations(client, wait_for)
        )
        self._disconnect_task = cleanup
        self._track_operation(cleanup)
        return cleanup

    async def _disconnect_after_operations(
        self, client: object, phase_tasks: tuple[asyncio.Task[Any], ...]
    ) -> bool:
        try:
            if phase_tasks:
                await asyncio.wait(phase_tasks)
        except asyncio.CancelledError:
            # A hard cleanup deadline must not abandon the eventual
            # disconnect merely because a preceding phase is stubborn.
            if phase_tasks:
                await asyncio.wait(phase_tasks)
        return await self._disconnect_once(client)

    def _track_operation(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            self._consume_task_exception(task)
            return
        self._lingering_operations.add(task)
        task.add_done_callback(self._operation_done)

    def _operation_done(self, task: asyncio.Task[Any]) -> None:
        self._lingering_operations.discard(task)
        self._consume_task_exception(task)

    def _prune_lingering_operations(self) -> None:
        for task in tuple(self._lingering_operations):
            if task.done():
                self._operation_done(task)

    @staticmethod
    def _task_succeeded(task: asyncio.Task[Any]) -> bool:
        try:
            return task.result() is True
        except BaseException:
            return False

    @staticmethod
    def _consume_task_exception(task: asyncio.Task[Any]) -> None:
        try:
            task.result()
        except BaseException:
            pass

    async def _disconnect_once(self, client: object) -> bool:
        if self._disconnect_called or self._disconnect_client is not client:
            return True
        self._disconnect_called = True
        try:
            await client.disconnect()
        except BaseException:
            return False
        return True


def _error_text(error: BaseException) -> str:
    """Name a transport failure for the report without Bluetooth addresses."""
    return redact(f"{type(error).__name__}: {error}")


__all__ = ["BleDiscovery", "BleTransport", "TransportError"]
