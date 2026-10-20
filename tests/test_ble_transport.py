"""Strict transport/coordinator contract tests for Task 5."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import sys
from types import SimpleNamespace
import types
from typing import Awaitable, Callable

import pytest

from custom_components.somfy_ble import ble as ble_module
from custom_components.somfy_ble import coordinator as coordinator_module
from custom_components.somfy_ble.ble import (
    BleDiscovery,
    BleTransport,
    TransportError,
)
from custom_components.somfy_ble.const import (
    BLE_AUTH_SETTLE_DELAY,
    BLE_AUTH_TIMEOUT,
    BLE_COMMAND_TIMEOUT,
    BLE_CONNECT_TIMEOUT,
    BLE_DISCOVERY_TIMEOUT,
)
from custom_components.somfy_ble.coordinator import Coordinator, SharedRuntime
from custom_components.somfy_ble.models import (
    COMMAND_TTL_SECONDS,
    CommandRequest,
    Result,
)
from custom_components.somfy_ble.protocol import Command, CommandPacket, JogDirection, encode_pin, make_command, somfy_uuid
from custom_components.somfy_ble.safety import Controller


ADDRESS = "aa:bb:cc:dd:ee:ff"
PIN = "012345"


class FakeCharacteristic:
    def __init__(self, uuid: str, properties: list[str] | None = None) -> None:
        self.uuid = uuid
        self.properties = properties or ["write"]


class FakeServices:
    def __init__(self, service: object | None, auth: object | None, command: object | None) -> None:
        self.service = service
        self.auth = auth
        self.command = command

    def get_service(self, uuid: str) -> object | None:
        return self.service if uuid == somfy_uuid(0) else None

    def get_characteristic(self, uuid: str) -> object | None:
        if uuid == somfy_uuid(0x0000000B):
            return self.auth
        return self.command


class FakeClient:
    def __init__(self, services: FakeServices) -> None:
        self.services = services
        self.writes: list[tuple[object, bytes, bool]] = []
        self.disconnect_count = 0
        self.events: list[str] = []
        self.write_error: BaseException | None = None
        self.disconnect_error: BaseException | None = None

    async def write_gatt_char(self, characteristic: object, data: bytes, response: bool = True) -> None:
        self.events.append("write")
        if self.write_error and self.writes:
            raise self.write_error
        self.writes.append((characteristic, data, response))

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        self.events.append("disconnect")
        if self.disconnect_error:
            raise self.disconnect_error


@dataclass
class FakeDiscovery:
    device: object | None
    source: str | None
    calls: int = 0

    async def __call__(self) -> BleDiscovery | None:
        self.calls += 1
        if self.device is None:
            return None
        return BleDiscovery(self.device, self.source, {"source": self.source})


@dataclass
class FakeConnector:
    client: FakeClient
    calls: list[tuple[object, str, dict]]

    async def __call__(self, client_class: object, device: object, name: str, **kwargs: object) -> FakeClient:
        self.calls.append((device, name, kwargs))
        return self.client


def packet() -> CommandPacket:
    return make_command(Command.IDENTIFY)


_MISSING = object()


def ready_transport(
    *,
    client: FakeClient | None = None,
    device: object | None = _MISSING,
    source: str = "proxy-a",
    display_name: str = "Living Room",
    motor_id: str = "motor-1",
    sleeper: Callable[[float], Awaitable[None]] | None = None,
) -> tuple[BleTransport, FakeDiscovery, FakeConnector, FakeClient]:
    service = object()
    auth = FakeCharacteristic(somfy_uuid(0x0000000B))
    command = FakeCharacteristic(packet().characteristic_uuid)
    fake_client = client or FakeClient(FakeServices(service, auth, command))
    discovery = FakeDiscovery(object() if device is _MISSING else device, source)
    connector = FakeConnector(fake_client, [])
    transport = BleTransport(
        hass=object(),
        address=ADDRESS,
        display_name=display_name,
        motor_id=motor_id,
        discovery=discovery,
        connector=connector,
        sleeper=sleeper or (lambda _: asyncio.sleep(0)),
    )
    return transport, discovery, connector, fake_client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        Command.JOG,
        Command.RANGE_START,
        Command.SET_DIRECTION_CLOCKWISE,
        Command.SET_DIRECTION_COUNTERCLOCKWISE,
        Command.SET_UPPER_LIMIT,
        Command.SET_LOWER_LIMIT,
    ],
)
async def test_calibration_characteristic_is_resolved_outside_auth_service(command) -> None:
    command_packet = make_command(command)
    auth = FakeCharacteristic(somfy_uuid(0x0000000B))
    calibration = FakeCharacteristic(command_packet.characteristic_uuid)
    # Real motors put auth in service 00000000 and calibration in 00010000.
    auth_service = SimpleNamespace(
        get_characteristic=lambda uuid: auth if uuid == auth.uuid else None
    )
    client = FakeClient(FakeServices(auth_service, auth, calibration))
    transport, _, _, _ = ready_transport(client=client)

    result = await transport.async_execute(command_packet, PIN, lambda: True)

    assert result is Result.SUCCESS
    assert client.writes == [
        (auth, encode_pin(PIN), True),
        (calibration, command_packet.payload, True),
    ]


@pytest.mark.asyncio
async def test_interrogate_discovers_complete_gatt_shape_without_auth_read_or_write() -> None:
    descriptor = SimpleNamespace(uuid="descriptor-uuid", handle=7)
    characteristic = SimpleNamespace(
        uuid="characteristic-uuid",
        handle=6,
        properties=["read", "write-without-response", "notify"],
        descriptors=[descriptor],
    )
    service = SimpleNamespace(
        uuid="service-uuid",
        handle=5,
        characteristics=[characteristic],
    )
    client = FakeClient([service])  # type: ignore[arg-type]
    transport, _, _, _ = ready_transport(client=client)

    result, report = await transport.async_interrogate()

    assert result is Result.SUCCESS
    assert report == (
        {
            "uuid": "service-uuid",
            "characteristics": (
                {
                    "uuid": "characteristic-uuid",
                    "properties": ("notify", "read", "write-without-response"),
                    "descriptors": ("descriptor-uuid",),
                },
            ),
        },
    )
    assert client.writes == []
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_prefers_one_discovered_source_and_reuses_device_for_transaction() -> None:
    transport, discovery, connector, client = ready_transport()
    selected = discovery.device

    assert await transport.async_execute(packet(), PIN, lambda: True) is Result.SUCCESS
    assert discovery.calls == 1
    assert connector.calls[0][0] is selected
    assert transport.selected_device is selected
    assert transport.selected_source == "proxy-a"
    assert not hasattr(transport, "selected_metadata")
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_home_assistant_discovery_prefers_returned_device_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = SimpleNamespace(details={"source": "device-source"})
    service_info = SimpleNamespace(source="advertisement-source")

    from homeassistant import components
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bluetooth)
    monkeypatch.setattr(components, "bluetooth", bluetooth, raising=False)

    monkeypatch.setattr(
        bluetooth,
        "async_last_service_info",
        lambda *args, **kwargs: service_info,
        raising=False,
    )
    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: device,
        raising=False,
    )
    transport = BleTransport(object(), ADDRESS)

    discovered = await transport._async_discover_from_home_assistant()

    assert discovered is not None
    assert discovered.device is device
    assert discovered.source == "device-source"


@pytest.mark.asyncio
async def test_home_assistant_discovery_uppercases_config_flow_address_for_case_sensitive_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Look up lowercase config-flow addresses using HA's uppercase cache key."""
    device = SimpleNamespace(details={"source": "proxy-source"})
    service_info = SimpleNamespace(source="proxy-source")
    observed_addresses: list[str] = []

    from homeassistant import components
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bluetooth)
    monkeypatch.setattr(components, "bluetooth", bluetooth, raising=False)

    def last_service_info(hass: object, address: str, *, connectable: bool) -> object | None:
        observed_addresses.append(address)
        return service_info if address == ADDRESS.upper() and connectable else None

    def device_from_address(hass: object, address: str, *, connectable: bool) -> object | None:
        observed_addresses.append(address)
        return device if address == ADDRESS.upper() and connectable else None

    monkeypatch.setattr(bluetooth, "async_last_service_info", last_service_info, raising=False)
    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        device_from_address,
        raising=False,
    )
    transport = BleTransport(object(), ADDRESS.lower())

    discovered = await transport._async_discover_from_home_assistant()

    assert transport.address == ADDRESS.lower()
    assert discovered is not None
    assert discovered.device is device
    assert discovered.source == "proxy-source"
    assert observed_addresses == [ADDRESS.upper(), ADDRESS.upper()]


@pytest.mark.asyncio
async def test_home_assistant_discovery_falls_back_to_service_source_when_device_source_is_unsafe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    device = SimpleNamespace(details={"source": object()})
    service_info = SimpleNamespace(source="advertisement-source")

    from homeassistant import components
    bluetooth = types.ModuleType("homeassistant.components.bluetooth")
    monkeypatch.setitem(sys.modules, "homeassistant.components.bluetooth", bluetooth)
    monkeypatch.setattr(components, "bluetooth", bluetooth, raising=False)

    monkeypatch.setattr(
        bluetooth,
        "async_last_service_info",
        lambda *args, **kwargs: service_info,
        raising=False,
    )
    monkeypatch.setattr(
        bluetooth,
        "async_ble_device_from_address",
        lambda *args, **kwargs: device,
        raising=False,
    )
    transport = BleTransport(object(), ADDRESS)

    discovered = await transport._async_discover_from_home_assistant()

    assert discovered is not None
    assert discovered.source == "advertisement-source"


@pytest.mark.asyncio
async def test_connector_is_called_once_with_cache_and_no_pairing() -> None:
    transport, _, connector, _ = ready_transport()

    await transport.async_execute(packet(), PIN, lambda: True)

    assert len(connector.calls) == 1
    _, name, kwargs = connector.calls[0]
    assert "Somfy Bluetooth" in name
    assert ADDRESS not in name
    assert PIN not in name
    assert kwargs == {"max_attempts": 3, "use_services_cache": True, "pair": False}


@pytest.mark.asyncio
async def test_connector_label_redacts_address_and_pin_from_display_name() -> None:
    transport, _, connector, _ = ready_transport(
        display_name=f"Office {ADDRESS} {PIN}",
    )

    await transport.async_execute(packet(), PIN, lambda: True)

    name = connector.calls[0][1]
    assert "Office" not in name
    assert ADDRESS not in name
    assert PIN not in name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("address", "pin", "display_name", "motor_id"),
    [
        ("aa:bb:cc:dd:ee:ff", "012345", "Office aa:bb:cc:dd:ee:ff 012345", "motor-1"),
        ("aa-bb-cc-dd-ee-ff", "012345", "Office aa-bb-cc-dd-ee-ff 012345", "motor-1"),
        ("aa.bb.cc.dd.ee.ff", "012345", "Office aa.bb.cc.dd.ee.ff 012345", "motor-1"),
        ("aabbccddeeff", "012345", "Office aabbccddeeff 012345", "motor-1"),
        ("aa bb cc dd ee ff", "012345", "Office aa bb cc dd ee ff 01 23 45", "motor-1"),
        ("aa:bb:cc:dd:ee:ff", "012345", "Office", "012345"),
    ],
)
async def test_connector_label_is_opaque_for_all_identity_and_pin_forms(
    address: str,
    pin: str,
    display_name: str,
    motor_id: str,
) -> None:
    service = object()
    auth = FakeCharacteristic(somfy_uuid(0x0000000B))
    command = FakeCharacteristic(packet().characteristic_uuid)
    client = FakeClient(FakeServices(service, auth, command))
    discovery = FakeDiscovery(object(), "proxy-a")
    connector = FakeConnector(client, [])
    transport = BleTransport(
        hass=object(),
        address=address,
        display_name=display_name,
        motor_id=motor_id,
        discovery=discovery,
        connector=connector,
    )

    assert await transport.async_execute(packet(), pin, lambda: True) is Result.SUCCESS
    name = connector.calls[0][1]

    assert name == f"Somfy Bluetooth {transport._safe_connector_label()}"
    assert len(name) <= 64
    assert name.replace(" ", "").replace("-", "").replace(".", "").isalnum()
    for sensitive in (address, pin, display_name, motor_id):
        assert sensitive not in name
    repeat = BleTransport(object(), address, display_name=display_name, motor_id=motor_id)
    assert transport._safe_connector_label() == repeat._safe_connector_label()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary_result", (None, False, 1, "approved", object()))
async def test_only_literal_true_boundary_allows_command_write(boundary_result: object) -> None:
    transport, _, _, client = ready_transport()

    result = await transport.async_execute(packet(), PIN, lambda: boundary_result)  # type: ignore[return-value]

    assert result is Result.STALE_CALLBACK
    assert len(client.writes) == 1
    assert client.writes[0][1] == encode_pin(PIN)


@pytest.mark.asyncio
async def test_boundary_exception_fails_closed_without_command_write() -> None:
    transport, _, _, client = ready_transport()

    def boundary() -> bool:
        raise RuntimeError("stale callback")

    assert await transport.async_execute(packet(), PIN, boundary) is Result.ERROR
    assert len(client.writes) == 1
    assert client.writes[0][1] == encode_pin(PIN)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        ("not_found", Result.NOT_FOUND),
        ("slot", Result.BUSY),
        ("connect", Result.ERROR),
        ("missing_service", Result.NOT_FOUND),
        ("missing_auth", Result.NOT_FOUND),
        ("missing_command", Result.NOT_FOUND),
    ],
)
async def test_prewrite_failures_never_invoke_boundary_or_command(failure: str, expected: Result) -> None:
    if failure == "not_found":
        transport, _, connector, client = ready_transport(device=None)
        connector.client = client
    else:
        service = None if failure == "missing_service" else object()
        auth = None if failure == "missing_auth" else FakeCharacteristic(somfy_uuid(0x0000000B))
        command = None if failure == "missing_command" else FakeCharacteristic(packet().characteristic_uuid)
        client = FakeClient(FakeServices(service, auth, command))
        transport, _, connector, _ = ready_transport(client=client)
        if failure == "slot":
            import bleak_retry_connector
            connector.client = client
            connector.__call__ = None  # type: ignore[method-assign]
            async def slot(*args: object, **kwargs: object) -> FakeClient:
                raise bleak_retry_connector.BleakOutOfConnectionSlotsError("slot")
            transport._connector = slot  # type: ignore[attr-defined]
        elif failure == "connect":
            async def connect(*args: object, **kwargs: object) -> FakeClient:
                raise RuntimeError("connect failed")
            transport._connector = connect  # type: ignore[attr-defined]

    called = False
    def boundary() -> bool:
        nonlocal called
        called = True
        return True

    result = await transport.async_execute(packet(), PIN, boundary)
    assert result is expected
    assert not called
    assert not client.writes
    assert client.disconnect_count <= 1


@pytest.mark.asyncio
async def test_auth_settles_before_acknowledged_command_without_read() -> None:
    transport, _, _, client = ready_transport()
    command = make_command(Command.JOG, direction=JogDirection.DOWN, step=321)
    boundary_events: list[str] = []

    result = await transport.async_execute(command, PIN, lambda: boundary_events.append("boundary") or True)

    assert result is Result.SUCCESS
    assert client.writes[0][0].uuid == somfy_uuid(0x0000000B)
    assert client.writes[0][1] == encode_pin(PIN)
    assert client.writes[0][2] is True
    assert client.writes[1] == (client.writes[1][0], b"A\x01", True)
    assert boundary_events == ["boundary"]
    assert client.events == ["write", "write", "disconnect"]


@pytest.mark.asyncio
async def test_auth_then_identify_use_acknowledged_writes() -> None:
    service = object()
    auth = FakeCharacteristic(
        somfy_uuid(0x0000000B), ["write", "write-without-response"]
    )
    identify = FakeCharacteristic(
        packet().characteristic_uuid, ["write", "write-without-response"]
    )
    client = FakeClient(FakeServices(service, auth, identify))
    transport, _, _, _ = ready_transport(client=client)

    result = await transport.async_execute(packet(), PIN, lambda: True)

    assert result is Result.SUCCESS
    assert client.writes == [
        (auth, encode_pin(PIN), True),
        (identify, b"\x01", True),
    ]
    assert client.events == ["write", "write", "disconnect"]


@pytest.mark.asyncio
async def test_retained_calibration_session_reuses_connection_and_authentication() -> None:
    transport, discovery, connector, client = ready_transport()
    jog_down = make_command(Command.JOG, direction=JogDirection.DOWN, step=25)
    jog_up = make_command(Command.JOG, direction=JogDirection.UP, step=10)

    first = await transport.async_execute(
        jog_down, PIN, lambda: True, retain_connection=True
    )
    second = await transport.async_execute(
        jog_up, PIN, lambda: True, retain_connection=True
    )

    assert first is Result.SUCCESS
    assert second is Result.SUCCESS
    assert discovery.calls == 1
    assert len(connector.calls) == 1
    assert [payload for _, payload, _ in client.writes] == [
        encode_pin(PIN),
        jog_down.payload,
        jog_up.payload,
    ]
    assert client.disconnect_count == 0

    assert await transport.async_close_session() is True
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_boundary_callback_is_immediately_before_command_write() -> None:
    events: list[str] = []

    async def sleeper(delay: float) -> None:
        assert delay == 2.0
        events.append("auth-settle")

    transport, _, _, client = ready_transport(sleeper=sleeper)
    original_write = client.write_gatt_char

    async def write(characteristic: object, data: bytes, response: bool = True) -> None:
        events.append("command-write" if data != encode_pin(PIN) else "auth-write")
        await original_write(characteristic, data, response)

    client.write_gatt_char = write  # type: ignore[method-assign]
    assert (
        await transport.async_execute(packet(), PIN, lambda: events.append("mark") or True)
        is Result.SUCCESS
    )
    assert events == ["auth-write", "auth-settle", "mark", "command-write"]


@pytest.mark.asyncio
async def test_command_error_after_boundary_is_unknown_and_disconnects_once() -> None:
    transport, _, _, client = ready_transport()
    client.write_error = RuntimeError("write failed")
    calls = 0

    def boundary() -> bool:
        nonlocal calls
        calls += 1
        return True

    result = await transport.async_execute(packet(), PIN, boundary)
    assert result is Result.ERROR
    assert calls == 1
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_disconnect_error_after_boundary_is_reported_as_transport_error() -> None:
    transport, _, _, client = ready_transport()
    client.disconnect_error = RuntimeError("disconnect failed")

    result = await transport.async_execute(packet(), PIN, lambda: True)

    assert result is Result.ERROR
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_disconnect_error_does_not_replace_prewrite_failure() -> None:
    transport, _, _, client = ready_transport()
    client.disconnect_error = RuntimeError("disconnect failed")
    original_write = client.write_gatt_char

    async def fail_auth_once(
        characteristic: object, data: bytes, response: bool = True
    ) -> None:
        if not client.writes:
            raise RuntimeError("authentication failed")
        await original_write(characteristic, data, response)

    client.write_gatt_char = fail_auth_once  # type: ignore[method-assign]

    result = await transport.async_execute(packet(), PIN, lambda: True)

    assert result is Result.AUTH_FAILED
    assert len(client.writes) == 0
    assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_stubborn_disconnect_timeout_is_reported_as_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ble_module, "BLE_DISCONNECT_TIMEOUT", 0.01)
    transport, _, _, client = ready_transport()
    disconnect_started = asyncio.Event()
    disconnect_finished = asyncio.Event()
    release_disconnect = asyncio.Event()

    async def stubborn_disconnect() -> None:
        client.disconnect_count += 1
        disconnect_started.set()
        try:
            await release_disconnect.wait()
        except asyncio.CancelledError:
            await release_disconnect.wait()
        disconnect_finished.set()

    client.disconnect = stubborn_disconnect  # type: ignore[method-assign]

    result = await asyncio.wait_for(
        transport.async_execute(packet(), PIN, lambda: True), 1.0
    )

    assert result is Result.ERROR
    assert disconnect_started.is_set()
    assert client.disconnect_count == 1
    release_disconnect.set()
    await asyncio.wait_for(disconnect_finished.wait(), 1.0)


@pytest.mark.asyncio
async def test_cancellation_before_boundary_and_after_boundary_cleanup_once() -> None:
    for after_boundary in (False, True):
        transport, _, _, client = ready_transport()
        entered = asyncio.Event()
        original = client.write_gatt_char

        async def write(characteristic: object, data: bytes, response: bool = True) -> None:
            if data != encode_pin(PIN) and after_boundary:
                entered.set()
                await asyncio.sleep(60)
            await original(characteristic, data, response)

        client.write_gatt_char = write  # type: ignore[method-assign]
        task = asyncio.create_task(
            transport.async_execute(packet(), PIN, lambda: True if after_boundary else (_ for _ in ()).throw(asyncio.CancelledError()))
        )
        if after_boundary:
            await entered.wait()
            task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert client.disconnect_count == 1


@pytest.mark.asyncio
async def test_disconnect_cleanup_has_hard_deadline_and_preserves_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ble_module, "BLE_DISCONNECT_TIMEOUT", 0.01)
    transport, _, _, client = ready_transport()
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    disconnect_finished = asyncio.Event()

    async def stubborn_disconnect() -> None:
        client.disconnect_count += 1
        disconnect_started.set()
        try:
            await release_disconnect.wait()
        except asyncio.CancelledError:
            await release_disconnect.wait()
        disconnect_finished.set()

    client.disconnect = stubborn_disconnect  # type: ignore[method-assign]

    def cancelled_boundary() -> bool:
        raise asyncio.CancelledError

    task = asyncio.create_task(transport.async_execute(packet(), PIN, cancelled_boundary))
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1.0)
    await disconnect_started.wait()
    assert client.disconnect_count == 1

    release_disconnect.set()
    await asyncio.wait_for(disconnect_finished.wait(), 1.0)


@pytest.mark.asyncio
async def test_coordinator_owns_controller_boundary_and_stale_ids_do_not_mutate_new_work() -> None:
    clock = lambda: 1.0
    controller = Controller(clock)
    controller.set_configured_motors(("living",))
    transport, _, _, _ = ready_transport()
    coordinator = Coordinator(controller)
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    assert queued.result is Result.SUCCESS

    result = await coordinator.async_execute_next()
    assert result.result is Result.SUCCESS
    assert result.command_id == queued.command_id
    assert not controller.has_active()

    queued2 = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    assert queued2.result is Result.SUCCESS
    began = controller.begin_next()
    assert controller.complete(began.command_id + 100, Result.ERROR).result is Result.STALE_CALLBACK
    assert controller.has_active()


@pytest.mark.asyncio
async def test_expired_boundary_rejects_once_without_write_and_preserves_command_result() -> None:
    class SteppingClock:
        def __init__(self) -> None:
            self.values = iter((0.0, 0.0, COMMAND_TTL_SECONDS))
            self.calls = 0

        def __call__(self) -> float:
            self.calls += 1
            return next(self.values)

    class BoundaryTransport:
        def __init__(self) -> None:
            self.command_writes = 0

        async def async_execute(
            self,
            packet: CommandPacket,
            pin: str,
            before_write: object,
            *,
            retain_connection: bool = False,
        ) -> Result:
            try:
                allowed = before_write()
            except TransportError as error:
                return error.result
            if allowed is not True:
                return Result.STALE_CALLBACK
            self.command_writes += 1
            return Result.SUCCESS

    clock = SteppingClock()
    controller = Controller(clock)
    coordinator = Coordinator(controller)
    transport = BoundaryTransport()
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))

    result = await coordinator.async_execute_next()

    assert result.result is Result.STALE_CALLBACK
    assert result.command_id == queued.command_id
    assert clock.calls == 3
    assert transport.command_writes == 0
    assert not controller.has_active()


def test_command_ttl_exceeds_the_full_bounded_prewrite_budget() -> None:
    prewrite_budget = (
        BLE_DISCOVERY_TIMEOUT
        + BLE_CONNECT_TIMEOUT
        + BLE_AUTH_TIMEOUT
        + BLE_AUTH_SETTLE_DELAY
    )

    assert COMMAND_TTL_SECONDS > prewrite_budget


@pytest.mark.asyncio
async def test_coordinator_maps_prewrite_timeout_and_postwrite_error() -> None:
    clock = lambda: 1.0
    controller = Controller(clock)
    controller.set_configured_motors(("living",))

    class Prewrite:
        async def async_execute(self, packet: CommandPacket, pin: str, before_write: object, *, retain_connection: bool = False) -> Result:
            raise TransportError(Result.TIMEOUT_BEFORE_WRITE)

    coordinator = Coordinator(controller)
    coordinator.register_motor("living", Prewrite(), ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    result = await coordinator.async_execute_next()
    assert result.result is Result.TIMEOUT_BEFORE_WRITE
    assert queued.command_id == result.command_id
    assert not controller.has_active()


@pytest.mark.asyncio
async def test_name_only_motor_loads_but_execution_is_not_found_without_credentials() -> None:
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    coordinator.register_motor("kitchen", object(), None, None, "Kitchen")  # type: ignore[arg-type]
    queued = controller.enqueue(CommandRequest("kitchen", Command.IDENTIFY))

    result = await coordinator.async_execute_next()
    assert result.result is Result.NOT_FOUND
    assert result.command_id == queued.command_id
    assert not controller.has_active()


@pytest.mark.asyncio
@pytest.mark.parametrize("transport_result", (Result.ERROR, Result.TIMEOUT_BEFORE_WRITE))
async def test_coordinator_maps_postwrite_failure_to_unknown_without_replay(
    transport_result: Result,
) -> None:
    clock = lambda: 1.0
    controller = Controller(clock)
    controller.set_configured_motors(("living",))
    calls = 0

    class Postwrite:
        async def async_execute(self, packet: CommandPacket, pin: str, before_write: object, *, retain_connection: bool = False) -> Result:
            nonlocal calls
            calls += 1
            assert before_write() is True
            return transport_result

    coordinator = Coordinator(controller)
    coordinator.register_motor("living", Postwrite(), ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))

    result = await coordinator.async_execute_next()
    assert result.result is Result.UNKNOWN_AFTER_WRITE
    assert result.command_id == queued.command_id
    assert calls == 1
    assert not controller.has_active()
    assert (await coordinator.async_execute_next()).result is Result.NOT_FOUND


@pytest.mark.asyncio
async def test_coordinator_maps_disconnect_failure_to_unknown_with_same_command_id() -> None:
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    transport, _, _, client = ready_transport()
    client.disconnect_error = RuntimeError("disconnect failed")
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))

    result = await coordinator.async_execute_next()

    assert result == coordinator_module.CompletionResult(
        Result.UNKNOWN_AFTER_WRITE, queued.command_id
    )
    assert controller.drain_deferred_results() == ()
    assert not controller.has_active()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("after_boundary", "expected"),
    ((False, Result.ERROR), (True, Result.UNKNOWN_AFTER_WRITE)),
)
async def test_coordinator_cancellation_retains_one_deferred_terminal_result(
    after_boundary: bool,
    expected: Result,
) -> None:
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    entered = asyncio.Event()
    release = asyncio.Event()

    class CancellableTransport:
        async def async_execute(
            self,
            packet: CommandPacket,
            pin: str,
            before_write: object,
            *,
            retain_connection: bool = False,
        ) -> Result:
            if after_boundary:
                assert before_write() is True
            entered.set()
            await release.wait()
            return Result.SUCCESS

    coordinator.register_motor(
        "living", CancellableTransport(), ADDRESS, PIN, "Living Room"
    )  # type: ignore[arg-type]
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    running = asyncio.create_task(coordinator.async_execute_next())
    await entered.wait()

    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running

    assert controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.BUSY
    assert controller.drain_deferred_results() == (
        coordinator_module.CompletionResult(expected, queued.command_id),
    )
    assert controller.drain_deferred_results() == ()
    assert not controller.has_active()
    assert controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.SUCCESS
    release.set()


@pytest.mark.asyncio
async def test_shared_coordinator_rejects_simultaneous_transactions_globally() -> None:
    clock = lambda: 1.0
    controller = Controller(clock)
    coordinator = Coordinator(controller)
    entered = asyncio.Event()
    release = asyncio.Event()

    class BlockingTransport:
        async def async_execute(self, packet: CommandPacket, pin: str, before_write: object, *, retain_connection: bool = False) -> Result:
            assert before_write() is True
            entered.set()
            await release.wait()
            return Result.SUCCESS

    coordinator.register_motor("living", BlockingTransport(), ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    coordinator.register_motor("bedroom", BlockingTransport(), ADDRESS, PIN, "Bedroom")  # type: ignore[arg-type]
    first = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    second = controller.enqueue(CommandRequest("bedroom", Command.IDENTIFY))
    assert first.result is Result.SUCCESS
    assert second.result is Result.SUCCESS

    running = asyncio.create_task(coordinator.async_execute_next())
    await entered.wait()
    assert (await coordinator.async_execute_next()).result is Result.BUSY
    release.set()
    assert (await running).result is Result.SUCCESS


@pytest.mark.asyncio
async def test_unregister_cancels_global_active_work_and_keeps_remaining_motor_usable() -> None:
    runtime = SharedRuntime()
    entered = asyncio.Event()

    class BlockingTransport:
        async def async_execute(self, packet: CommandPacket, pin: str, before_write: object, *, retain_connection: bool = False) -> Result:
            assert before_write() is True
            entered.set()
            await asyncio.sleep(60)
            return Result.SUCCESS

    runtime.register_motor("living", BlockingTransport(), ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    runtime.register_motor("bedroom", object(), ADDRESS, PIN, "Bedroom")  # type: ignore[arg-type]
    runtime.controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    running = asyncio.create_task(runtime.coordinator.async_execute_next())
    await entered.wait()

    assert await runtime.async_unregister_motor("bedroom") is False
    assert runtime.coordinator.motor_ids == ("living",)
    assert runtime.controller.is_configured("living")
    assert not runtime.controller.is_configured("bedroom")
    assert not runtime.controller.has_active()
    assert runtime.controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.BUSY
    assert runtime.controller.drain_deferred_results() == (
        coordinator_module.CompletionResult(Result.UNKNOWN_AFTER_WRITE, 1),
    )
    assert runtime.controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.SUCCESS
    with pytest.raises(asyncio.CancelledError):
        await running
    await runtime.coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_unregister_is_bounded_and_keeps_global_busy_until_stubborn_work_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "EXECUTION_CANCEL_TIMEOUT", 0.01)
    runtime = SharedRuntime()
    entered = asyncio.Event()
    release = asyncio.Event()
    callbacks: list[object] = []
    calls = 0

    class StubbornTransport:
        address = ADDRESS

        async def async_execute(self, packet: CommandPacket, pin: str, before_write: object, *, retain_connection: bool = False) -> Result:
            nonlocal calls
            calls += 1
            callbacks.append(before_write)
            assert before_write() is True
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return Result.SUCCESS

    runtime.register_motor("living", StubbornTransport(), ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    runtime.register_motor("bedroom", object(), ADDRESS, PIN, "Bedroom")  # type: ignore[arg-type]
    queued = runtime.controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    running = asyncio.create_task(runtime.coordinator.async_execute_next())
    await entered.wait()

    started = asyncio.get_running_loop().time()
    assert await runtime.async_unregister_motor("bedroom") is False
    assert asyncio.get_running_loop().time() - started < 0.5
    assert runtime.coordinator.motor_ids == ("living",)
    assert runtime.controller.is_configured("living")
    assert not runtime.controller.is_configured("bedroom")
    assert not runtime.controller.has_active()
    assert (await runtime.coordinator.async_execute_next()).result is Result.BUSY
    with pytest.raises(TransportError):
        callbacks[0]()  # type: ignore[operator]

    release.set()
    finished = await running
    assert finished.command_id == queued.command_id
    assert calls == 1
    assert runtime.controller.drain_deferred_results() == (
        coordinator_module.CompletionResult(Result.UNKNOWN_AFTER_WRITE, queued.command_id),
    )
    assert runtime.controller.drain_deferred_results() == ()
    assert runtime.controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.SUCCESS
    assert (await runtime.coordinator.async_execute_next()).result is Result.SUCCESS
    await runtime.coordinator.async_shutdown()


@pytest.mark.asyncio
async def test_shutdown_is_bounded_and_revokes_controller_until_stubborn_work_exits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coordinator_module, "EXECUTION_CANCEL_TIMEOUT", 0.01)
    runtime = SharedRuntime()
    entered = asyncio.Event()
    release = asyncio.Event()

    class StubbornTransport:
        address = ADDRESS

        async def async_execute(self, packet: CommandPacket, pin: str, before_write: object, *, retain_connection: bool = False) -> Result:
            assert before_write() is True
            entered.set()
            while not release.is_set():
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    continue
            return Result.SUCCESS

    runtime.register_motor("living", StubbornTransport(), ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    runtime.controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    running = asyncio.create_task(runtime.coordinator.async_execute_next())
    await entered.wait()

    started = asyncio.get_running_loop().time()
    await runtime.coordinator.async_shutdown()
    assert asyncio.get_running_loop().time() - started < 0.5
    assert not runtime.controller.is_configured("living")
    assert (await runtime.coordinator.async_execute_next()).result is Result.BUSY

    release.set()
    await running
    assert (await runtime.coordinator.async_execute_next()).result is Result.NOT_FOUND


@pytest.mark.asyncio
async def test_stop_priority_and_active_state_are_controller_owned() -> None:
    clock = lambda: 1.0
    controller = Controller(clock)
    controller.set_configured_motors(("living",))
    transport, _, _, _ = ready_transport()
    coordinator = Coordinator(controller)
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    assert controller.enqueue(CommandRequest("living", Command.JOG)).result is Result.SUCCESS
    assert controller.enqueue(CommandRequest("living", Command.STOP)).result is Result.SUCCESS
    result = await coordinator.async_execute_next()
    assert result.result is Result.SUCCESS
    assert not controller.has_active()


def test_transport_diagnostics_do_not_expose_pin() -> None:
    transport, _, _, _ = ready_transport()
    for value in (repr(transport), str(transport), repr(TransportError(Result.AUTH_FAILED))):
        assert PIN not in value


@pytest.mark.asyncio
async def test_phase_deadlines_use_owned_tasks_and_are_hard(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ble_module, "BLE_CONNECT_TIMEOUT", 0.01)
    transport, discovery, _, client = ready_transport()
    release = asyncio.Event()

    async def stubborn_connector(*args: object, **kwargs: object) -> FakeClient:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return client

    transport._connector = stubborn_connector  # type: ignore[attr-defined]
    started = asyncio.get_running_loop().time()
    result = await transport.async_execute(packet(), PIN, lambda: True)

    assert result is Result.TIMEOUT_BEFORE_WRITE
    assert asyncio.get_running_loop().time() - started < 0.2
    assert discovery.calls == 1
    assert not client.writes
    assert transport.has_lingering_operations
    release.set()
    await transport.async_wait_quiescent()
    assert client.disconnect_count == 1
    assert not transport.has_lingering_operations


@pytest.mark.asyncio
async def test_auth_cancellation_does_not_continue_to_command_after_release() -> None:
    transport, _, _, client = ready_transport()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_write = client.write_gatt_char

    async def stubborn_auth(
        characteristic: object, data: bytes, response: bool = True
    ) -> None:
        if data == encode_pin(PIN):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        await original_write(characteristic, data, response)

    client.write_gatt_char = stubborn_auth  # type: ignore[method-assign]
    running = asyncio.create_task(transport.async_execute(packet(), PIN, lambda: True))
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 0.2)
    release.set()
    await transport.async_wait_quiescent()
    assert len(client.writes) == 1
    assert client.writes[0][1] == encode_pin(PIN)


@pytest.mark.asyncio
async def test_command_cancellation_is_unknown_and_busy_until_write_quiesces() -> None:
    transport, _, _, client = ready_transport()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_write = client.write_gatt_char

    async def stubborn_command(
        characteristic: object, data: bytes, response: bool = True
    ) -> None:
        if data != encode_pin(PIN):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        await original_write(characteristic, data, response)

    client.write_gatt_char = stubborn_command  # type: ignore[method-assign]
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    running = asyncio.create_task(coordinator.async_execute_next())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 0.2)
    assert controller.drain_deferred_results() == (
        coordinator_module.CompletionResult(Result.UNKNOWN_AFTER_WRITE, queued.command_id),
    )
    assert (await coordinator.async_execute_next()).result is Result.BUSY
    release.set()
    await transport.async_wait_quiescent()


@pytest.mark.asyncio
async def test_coordinator_tracks_lingering_connector_after_outer_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ble_module, "BLE_CONNECT_TIMEOUT", 0.01)
    transport, _, _, client = ready_transport()
    release = asyncio.Event()

    async def stubborn_connector(*args: object, **kwargs: object) -> FakeClient:
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
        return client

    transport._connector = stubborn_connector  # type: ignore[attr-defined]
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))

    result = await coordinator.async_execute_next()
    assert result == coordinator_module.CompletionResult(
        Result.TIMEOUT_BEFORE_WRITE, queued.command_id
    )
    assert (await coordinator.async_execute_next()).result is Result.BUSY
    release.set()
    await transport.async_wait_quiescent()
    assert controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.SUCCESS
    assert (await coordinator.async_execute_next()).result is Result.SUCCESS


@pytest.mark.asyncio
async def test_coordinator_auth_cancellation_records_prewrite_error() -> None:
    transport, _, _, client = ready_transport()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_write = client.write_gatt_char

    async def stubborn_auth(
        characteristic: object, data: bytes, response: bool = True
    ) -> None:
        if data == encode_pin(PIN):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
        await original_write(characteristic, data, response)

    client.write_gatt_char = stubborn_auth  # type: ignore[method-assign]
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))
    running = asyncio.create_task(coordinator.async_execute_next())
    await entered.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(running, 0.2)
    assert controller.drain_deferred_results() == (
        coordinator_module.CompletionResult(Result.ERROR, queued.command_id),
    )
    assert (await coordinator.async_execute_next()).result is Result.BUSY
    release.set()
    await transport.async_wait_quiescent()
    assert len(client.writes) == 1


@pytest.mark.asyncio
async def test_stubborn_disconnect_keeps_coordinator_busy_until_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ble_module, "BLE_DISCONNECT_TIMEOUT", 0.01)
    transport, _, _, client = ready_transport()
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()

    async def stubborn_disconnect() -> None:
        client.disconnect_count += 1
        disconnect_started.set()
        try:
            await release_disconnect.wait()
        except asyncio.CancelledError:
            await release_disconnect.wait()

    client.disconnect = stubborn_disconnect  # type: ignore[method-assign]
    controller = Controller(lambda: 1.0)
    coordinator = Coordinator(controller)
    coordinator.register_motor("living", transport, ADDRESS, PIN, "Living Room")
    queued = controller.enqueue(CommandRequest("living", Command.IDENTIFY))

    result = await coordinator.async_execute_next()
    assert result == coordinator_module.CompletionResult(
        Result.UNKNOWN_AFTER_WRITE, queued.command_id
    )
    await disconnect_started.wait()
    cleanup_task = transport._disconnect_task
    assert cleanup_task is not None
    assert not cleanup_task.done()
    assert cleanup_task.cancelling() == 0
    assert (await coordinator.async_execute_next()).result is Result.BUSY
    assert controller.drain_deferred_results() == ()
    release_disconnect.set()
    await transport.async_wait_quiescent()
    assert controller.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.SUCCESS
    assert (await coordinator.async_execute_next()).result is Result.SUCCESS


@pytest.mark.asyncio
async def test_connector_done_before_parent_cancellation_orphans_client_once() -> None:
    transport, _, _, client = ready_transport()
    parent = asyncio.current_task()
    assert parent is not None

    async def connector(*args: object, **kwargs: object) -> FakeClient:
        asyncio.get_running_loop().call_soon(parent.cancel)
        return client

    transport._connector = connector  # type: ignore[attr-defined]

    with pytest.raises(asyncio.CancelledError):
        await transport.async_execute(packet(), PIN, lambda: True)

    assert not client.writes
    assert transport.has_lingering_operations
    await transport.async_wait_quiescent()
    assert client.disconnect_count == 1
    assert not client.writes


@pytest.mark.asyncio
async def test_cancellation_during_disconnect_deadline_leaves_cleanup_alive() -> None:
    transport, _, _, client = ready_transport()
    parent = asyncio.current_task()
    assert parent is not None
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()

    async def stubborn_disconnect() -> None:
        client.disconnect_count += 1
        disconnect_started.set()
        parent.cancel()
        try:
            await release_disconnect.wait()
        except asyncio.CancelledError:
            await release_disconnect.wait()

    client.disconnect = stubborn_disconnect  # type: ignore[method-assign]

    with pytest.raises(asyncio.CancelledError):
        await transport.async_execute(packet(), PIN, lambda: True)

    await disconnect_started.wait()
    cleanup_task = transport._disconnect_task
    assert cleanup_task is not None
    assert not cleanup_task.done()
    assert cleanup_task.cancelling() == 0
    release_disconnect.set()
    await transport.async_wait_quiescent()
    assert client.disconnect_count == 1
