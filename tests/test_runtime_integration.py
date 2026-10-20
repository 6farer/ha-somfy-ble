"""Focused Task5/Task6 runtime seam tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.somfy_ble import (
    DOMAIN,
    PLATFORMS,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.somfy_ble import coordinator as coordinator_module
from custom_components.somfy_ble.coordinator import SharedRuntime
from custom_components.somfy_ble.models import CommandRequest, Result
from custom_components.somfy_ble.protocol import Command, CommandPacket


ADDRESS = "aa:bb:cc:dd:ee:ff"
PIN = "012345"


@dataclass
class FakeTransport:
    """A transport-shaped fake that crosses the real coordinator boundary."""

    address: str = ADDRESS
    selected_source: str | None = "esphable1"
    result: Result = Result.SUCCESS
    calls: int = 0
    packets: list[CommandPacket] = field(default_factory=list)
    retained: list[bool] = field(default_factory=list)
    close_calls: int = 0
    has_session: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event | None = None

    async def async_execute(
        self,
        packet: CommandPacket,
        pin: str,
        before_write: object,
        *,
        retain_connection: bool = False,
    ) -> Result:
        assert pin == PIN
        self.calls += 1
        self.packets.append(packet)
        self.retained.append(retain_connection)
        self.has_session = retain_connection and self.result is Result.SUCCESS
        self.entered.set()
        assert before_write() is True
        if self.release is not None:
            await self.release.wait()
        return self.result

    async def async_close_session(self) -> bool:
        if not self.has_session:
            return True
        self.close_calls += 1
        self.has_session = False
        return True


async def _runtime_with_motor(
    transport: FakeTransport | None = None,
    motor_id: str = "living",
) -> tuple[SharedRuntime, FakeTransport]:
    runtime = SharedRuntime()
    transport = transport or FakeTransport()
    runtime.register_motor(motor_id, transport, ADDRESS, PIN, "Living Room")  # type: ignore[arg-type]
    return runtime, transport


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    Command.JOG, Command.RANGE_START, Command.SET_DIRECTION_CLOCKWISE,
    Command.SET_DIRECTION_COUNTERCLOCKWISE, Command.SET_UPPER_LIMIT, Command.SET_LOWER_LIMIT,
])
async def test_calibration_commands_execute_without_an_enable_step(command) -> None:
    runtime, transport = await _runtime_with_motor()
    try:
        admitted = runtime.enqueue(CommandRequest("living", command, step=25))
        await runtime.async_wait_idle()
        assert admitted.result is Result.SUCCESS
        assert transport.calls == 1
        assert runtime.snapshot("living").last_result is Result.SUCCESS
    finally:
        await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_enqueue_automatically_executes_transport_and_publishes_success() -> None:
    runtime, transport = await _runtime_with_motor()

    admitted = runtime.enqueue(CommandRequest("living", Command.IDENTIFY))
    await runtime.async_wait_idle()

    assert admitted.result is Result.SUCCESS
    assert transport.calls == 1
    assert runtime.snapshot("living").last_result is Result.SUCCESS
    assert runtime.snapshot("living").reachable is True
    assert runtime.snapshot("living").selected_source == "esphable1"
    await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_jog_uses_runtime_step_setting() -> None:
    runtime, transport = await _runtime_with_motor()

    assert runtime.get_jog_step("living") == 100
    assert runtime.set_jog_step("living", 321) is Result.SUCCESS
    admitted = runtime.enqueue(CommandRequest("living", Command.JOG, step=321))
    await runtime.async_wait_idle()

    assert admitted.result is Result.SUCCESS
    assert transport.calls == 1
    assert transport.packets[0].payload == b"\x41\x01"
    await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_jogs_retain_one_calibration_session() -> None:
    runtime, transport = await _runtime_with_motor()
    try:

        runtime.enqueue(CommandRequest("living", Command.JOG, step=25))
        runtime.enqueue(CommandRequest("living", Command.JOG, step=10))
        await runtime.async_wait_idle()

        assert transport.retained == [True, True]
        assert transport.close_calls == 0
    finally:
        await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_idle_timeout_closes_connection_without_locking_out_next_jog(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "BLE_SESSION_IDLE_TIMEOUT", 0.01)
    runtime, transport = await _runtime_with_motor()
    try:
        runtime.enqueue(CommandRequest("living", Command.JOG, step=25))
        await runtime.async_wait_idle()

        await asyncio.sleep(0.02)
        await runtime.async_wait_idle()

        assert transport.close_calls == 1
        admitted = runtime.enqueue(CommandRequest("living", Command.JOG, step=10))
        await runtime.async_wait_idle()
        assert admitted.result is Result.SUCCESS
        assert transport.calls == 2
    finally:
        await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_idle_cleanup_does_not_close_an_in_progress_jog(monkeypatch) -> None:
    monkeypatch.setattr(coordinator_module, "BLE_SESSION_IDLE_TIMEOUT", 0.01)
    runtime, transport = await _runtime_with_motor()
    try:
        runtime.enqueue(CommandRequest("living", Command.JOG, step=25))
        await runtime.async_wait_idle()
        transport.entered.clear()
        transport.release = asyncio.Event()
        runtime.enqueue(CommandRequest("living", Command.JOG, step=10))
        await transport.entered.wait()
        await asyncio.sleep(0.02)
        assert transport.close_calls == 0
        transport.release.set()
        await runtime.async_wait_idle()
        assert runtime.snapshot("living").last_result is Result.SUCCESS
    finally:
        if transport.release is not None:
            transport.release.set()
        await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_limit_write_ends_retained_calibration_session() -> None:
    runtime, transport = await _runtime_with_motor()
    try:
        runtime.enqueue(CommandRequest("living", Command.JOG, step=25))
        runtime.enqueue(CommandRequest("living", Command.SET_LOWER_LIMIT))
        await runtime.async_wait_idle()

        assert transport.retained == [True, False]
        assert transport.has_session is False
    finally:
        await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_post_write_error_is_exposed_as_unknown_after_write() -> None:
    runtime, transport = await _runtime_with_motor(FakeTransport(result=Result.ERROR))

    runtime.enqueue(CommandRequest("living", Command.IDENTIFY))
    await runtime.async_wait_idle()

    state = runtime.snapshot("living")
    assert state.last_result is Result.UNKNOWN_AFTER_WRITE
    assert state.last_error == Result.UNKNOWN_AFTER_WRITE.value
    assert transport.calls == 1
    await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_two_motors_are_globally_serialized() -> None:
    first = FakeTransport(release=asyncio.Event())
    second = FakeTransport()
    runtime = SharedRuntime()
    runtime.register_motor("living", first, ADDRESS, PIN, "Living")  # type: ignore[arg-type]
    runtime.register_motor("bedroom", second, ADDRESS, PIN, "Bedroom")  # type: ignore[arg-type]

    runtime.enqueue(CommandRequest("living", Command.IDENTIFY))
    runtime.enqueue(CommandRequest("bedroom", Command.IDENTIFY))
    await first.entered.wait()
    await asyncio.sleep(0)
    assert second.calls == 0

    first.release.set()
    await runtime.async_wait_idle()
    assert first.calls == 1
    assert second.calls == 1
    await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_switching_motors_closes_the_previous_calibration_session() -> None:
    first = FakeTransport()
    second = FakeTransport()
    runtime = SharedRuntime()
    runtime.register_motor("living", first, ADDRESS, PIN, "Living")  # type: ignore[arg-type]
    runtime.register_motor("bedroom", second, ADDRESS, PIN, "Bedroom")  # type: ignore[arg-type]
    try:
        runtime.enqueue(CommandRequest("living", Command.JOG, step=25))
        await runtime.async_wait_idle()
        assert first.has_session is True

        runtime.enqueue(CommandRequest("bedroom", Command.JOG, step=25))
        await runtime.async_wait_idle()

        assert first.close_calls == 1
        assert second.has_session is True
    finally:
        await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_stop_deferred_results_are_drained_and_do_not_backpressure_forever() -> None:
    first = FakeTransport(release=asyncio.Event())
    runtime, _ = await _runtime_with_motor(first)
    first_admitted = runtime.enqueue(CommandRequest("living", Command.JOG, step=100))
    await first.entered.wait()
    pending = [
        runtime.enqueue(CommandRequest("living", Command.IDENTIFY))
        for _ in range(7)
    ]
    stop = runtime.enqueue(CommandRequest("living", Command.STOP))
    assert stop.result is Result.SUCCESS

    first.release.set()
    await runtime.async_wait_idle()

    assert first_admitted.result is Result.SUCCESS
    assert runtime.controller.deferred_count() == 0
    assert runtime.snapshot("living").last_result is Result.SUCCESS
    assert runtime.enqueue(CommandRequest("living", Command.IDENTIFY)).result is Result.SUCCESS
    await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_subscriber_exception_does_not_break_worker() -> None:
    runtime, transport = await _runtime_with_motor()
    notifications: list[str] = []

    def broken() -> None:
        raise RuntimeError("subscriber failure")

    runtime.subscribe("living", broken)
    runtime.subscribe("living", lambda: notifications.append("notified"))
    runtime.enqueue(CommandRequest("living", Command.IDENTIFY))
    await runtime.async_wait_idle()

    assert transport.calls == 1
    assert len(notifications) >= 2
    await runtime.async_shutdown()


@pytest.mark.asyncio
async def test_runtime_snapshots_and_diagnostics_never_expose_credentials() -> None:
    runtime, _ = await _runtime_with_motor()

    rendered = repr(runtime) + repr(runtime.snapshot("living")) + repr(
        runtime.diagnostics_snapshot("living")
    )

    assert ADDRESS not in rendered
    assert PIN not in rendered
    assert set(runtime.diagnostics_snapshot("living")) <= {
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
    await runtime.async_shutdown()


@pytest.fixture
def entry() -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="Living Room",
        data={"name": "Living Room", "address": ADDRESS, "pin": PIN},
    )


@pytest.mark.asyncio
async def test_setup_forwards_exactly_four_platforms_after_runtime_registration(hass, entry) -> None:
    entry.add_to_hass(hass)
    forward = AsyncMock()
    hass.config_entries.async_forward_entry_setups = forward

    assert await async_setup_entry(hass, entry)

    forward.assert_awaited_once_with(entry, PLATFORMS)
    assert tuple(platform.value for platform in PLATFORMS) == (
        "binary_sensor",
        "button",
        "number",
        "sensor",
    )
    assert entry.runtime_data.runtime is hass.data[DOMAIN]
    await async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_forward_failure_rolls_back_motor_and_runtime(hass, entry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=RuntimeError("forward"))
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    with pytest.raises(RuntimeError, match="forward"):
        await async_setup_entry(hass, entry)

    assert DOMAIN not in hass.data
    assert not hasattr(entry, "runtime_data")
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(entry, PLATFORMS)


@pytest.mark.asyncio
async def test_unload_false_preserves_runtime_and_motor(hass, entry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    assert await async_setup_entry(hass, entry)
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=False)

    assert await async_unload_entry(hass, entry) is False
    assert DOMAIN in hass.data
    assert entry.runtime_data.controller.is_configured(entry.runtime_data.motor_id)
    assert not entry.runtime_data.runtime.coordinator._shutdown
    await entry.runtime_data.runtime.async_shutdown()


@pytest.mark.asyncio
async def test_final_unload_quiesces_and_removes_domain(hass, entry) -> None:
    entry.add_to_hass(hass)
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    assert await async_setup_entry(hass, entry)
    runtime = entry.runtime_data.runtime
    hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)

    assert await async_unload_entry(hass, entry) is True

    assert hass.config_entries.async_unload_platforms.await_args.args == (entry, PLATFORMS)
    assert runtime.coordinator._shutdown is True
    assert DOMAIN not in hass.data
    assert not runtime.coordinator.motor_ids
