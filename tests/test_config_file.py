"""Motor configuration file: CBOR, framing, write gate, and the BLE exchange."""

from __future__ import annotations

import asyncio

import pytest

from custom_components.somfy_ble.ble import BleDiscovery, BleTransport
from custom_components.somfy_ble.config_file import (
    READ_SEQUENCE,
    STATUS_UUID,
    TRANSFER_UUID,
    CborError,
    cbor_decode,
    cbor_encode,
    check_write,
    decode_file,
    redact,
    write_payload,
)
from custom_components.somfy_ble.coordinator import Coordinator, SharedRuntime
from custom_components.somfy_ble.models import Result
from custom_components.somfy_ble.protocol import Characteristic, encode_pin
from custom_components.somfy_ble.safety import Controller


PIN = "012345"
SPEED = {"w": True, "l": 1000, "h": 2800, "u": "1/100rpm", "i": 2000}
CONFIG = {
    "NominalSpeed": [2000, SPEED],
    "StartUpRamp": [1000, {"w": True, "l": 500, "h": 1000}],
    "Application": ["Roller", {"w": True, "e": ["Roller", "Sheer", "Zebra"]}],
    "LiftRange": [7936, {"w": False}],
}


def test_write_payload_matches_the_app_encoding() -> None:
    assert write_payload("NominalSpeed", 1).hex() == "02a16c4e6f6d696e616c53706565648101"
    assert write_payload("TiltSpeed", 1).hex() == "02a16954696c7453706565648101"


def test_cbor_round_trips_and_rejects_trailing_bytes() -> None:
    value = {"a": [1, -5, 300, 70000], "b": True, "c": None, "d": "text", "e": 1.5}
    assert cbor_decode(cbor_encode(value)) == value
    with pytest.raises(CborError):
        cbor_decode(cbor_encode(1) + b"\x00")
    with pytest.raises(CborError):
        cbor_decode(b"\xa1\x61")  # truncated


def test_cbor_decodes_indefinite_maps_and_bytes() -> None:
    assert cbor_decode(bytes.fromhex("bf6161420102ff")) == {"a": "0102"}


def test_decode_file_skips_a_short_header() -> None:
    data = b"\x03\x00" + cbor_encode(CONFIG)
    assert decode_file(data) == (CONFIG, 2)
    assert decode_file(b"\x01\x02") == (None, -1)


@pytest.mark.parametrize(
    ("config", "key", "value", "reason"),
    [
        (CONFIG, "ResetToFactory", 1, "not a writable"),
        (CONFIG, "NominalSpeed", 1.5, "value must"),
        (CONFIG, "NominalSpeed", [1], "value must"),
        (None, "NominalSpeed", 1, "read the motor config first"),
        (CONFIG, "TiltSpeed", 1, "not in this motor"),
        (CONFIG, "NominalSpeed", 2900, "between 1000 and 2800"),
        (CONFIG, "NominalSpeed", True, "needs a int"),
        (CONFIG, "DisabledSoftRamps", True, "not in this motor"),
    ],
)
def test_write_gate_refuses(config, key, value, reason) -> None:
    assert reason in check_write(config, key, value)


def test_write_gate_allows_a_key_the_read_reported() -> None:
    assert check_write(CONFIG, "NominalSpeed", 2800) is None
    assert check_write(CONFIG, "StartUpRamp", 500) is None


def test_redact_strips_addresses() -> None:
    assert redact("failed for AA:BB:CC:DD:EE:FF") == "failed for <address>"


class Char:
    def __init__(self, uuid: str) -> None:
        self.uuid = uuid


class Services:
    def __init__(self) -> None:
        self.chars = {uuid: Char(uuid) for uuid in (Characteristic.AUTH.uuid, TRANSFER_UUID, STATUS_UUID)}

    def get_characteristic(self, uuid: str) -> Char | None:
        return self.chars.get(uuid)


class FileClient:
    """Answers each file command on the transfer characteristic."""

    def __init__(self, *, file: bytes = b"", notify_error: Exception | None = None,
                 fail_on: bytes | None = None, ready: bool = True,
                 hang_on: bytes | None = None, size_extra: int = 0) -> None:
        self.services = Services()
        self.mtu_size = 23
        self.file = file
        self.notify_error = notify_error
        self.fail_on = fail_on
        self.writes: list[tuple[str, bytes]] = []
        self.reply = b""
        self.chunks: list[bytes] = []
        self.ready = ready
        self.hang_on = hang_on
        self.size_extra = size_extra
        self.reads: list[str] = []
        self.status = b"\x00\x00"
        self.callback = None
        self.disconnects = 0

    async def start_notify(self, char, callback) -> None:
        if self.notify_error:
            raise self.notify_error
        self.callback = callback

    async def write_gatt_char(self, char, data: bytes, response: bool = True) -> None:
        self.writes.append((char.uuid, bytes(data)))
        if self.hang_on is not None and data == self.hang_on:
            await asyncio.sleep(3600)
        if char.uuid != TRANSFER_UUID:
            return
        if data[0] == 0x00 and self.ready:
            self.status = b"\x05\x64"
            if self.callback:
                self.callback(char, bytearray(self.status))
        if self.fail_on is not None and data == self.fail_on:
            raise RuntimeError("GATT error=14 for AA:BB:CC:DD:EE:FF")
        if data[0] == 0x00:
            pass
        elif data[0] == 0x01:
            self.reply = (len(self.file) + self.size_extra).to_bytes(2, "little")
        elif data[0] == 0x03:
            self.chunks = [self.file[i:i + 20] for i in range(0, len(self.file), 20)]
            self.reply = b""
        else:
            self.reply = b"\x00"

    async def read_gatt_char(self, char) -> bytes:
        self.reads.append(char.uuid)
        if char.uuid == STATUS_UUID:
            return self.status
        if self.chunks:
            return self.chunks.pop(0)
        reply, self.reply = self.reply, b""
        return reply

    async def disconnect(self) -> None:
        self.disconnects += 1


def transport_for(client: FileClient) -> BleTransport:
    async def discovery():
        return BleDiscovery(object(), "proxy", None)

    async def connector(*_args, **_kwargs):
        return client

    async def no_sleep(_):
        await asyncio.sleep(0)

    return BleTransport(object(), "aa:bb:cc:dd:ee:ff", discovery=discovery, connector=connector, sleeper=no_sleep)


async def test_read_authenticates_then_opens_reads_and_closes() -> None:
    client = FileClient(file=cbor_encode(CONFIG))
    result, trace = await transport_for(client).async_config_exchange(PIN, READ_SEQUENCE)
    assert result is Result.SUCCESS
    sent = [(uuid, data.hex()) for uuid, data in client.writes]
    assert sent[0] == (Characteristic.AUTH.uuid, encode_pin(PIN).hex())
    assert [data for _, data in sent[1:]] == ["00c40000", "01", "03", "04"]
    # 00040002 is the app's firmware-update characteristic; never write to it.
    assert {uuid for uuid, _ in sent} == {Characteristic.AUTH.uuid, TRANSFER_UUID}
    read_step = trace["steps"][2]
    assert bytes.fromhex("".join(read_step["reads"])) == cbor_encode(CONFIG)
    assert trace["steps"][0]["status"] == "0564"
    assert trace["size"] == len(cbor_encode(CONFIG))
    assert trace["mtu"] == 23
    assert client.disconnects == 1


async def test_open_ack_error_is_judged_by_file_status() -> None:
    # The real motor answered OPEN with ATT 0x0E, then reported 05 64 (ready).
    client = FileClient(file=cbor_encode(CONFIG), fail_on=READ_SEQUENCE[0])
    result, trace = await transport_for(client).async_config_exchange(PIN, READ_SEQUENCE)
    assert result is Result.SUCCESS
    assert "<address>" in trace["steps"][0]["write_error"]
    assert "AA:BB" not in str(trace)


async def test_file_never_ready_fails_but_still_closes() -> None:
    client = FileClient(file=cbor_encode(CONFIG), ready=False)
    result, trace = await transport_for(client).async_config_exchange(PIN, READ_SEQUENCE)
    assert result is Result.ERROR
    assert trace["steps"][0]["error"] == "file not ready: 0000"
    assert [data for _, data in client.writes[1:]] == [READ_SEQUENCE[0], b"\x04"]
    assert client.disconnects == 1


async def test_notify_failure_is_recorded_and_exchange_continues() -> None:
    client = FileClient(file=cbor_encode(CONFIG), notify_error=RuntimeError("not permitted"))
    result, trace = await transport_for(client).async_config_exchange(PIN, READ_SEQUENCE)
    assert result is Result.SUCCESS
    assert "not permitted" in trace["notify"]


async def runtime_with(client: FileClient) -> SharedRuntime:
    runtime = SharedRuntime()
    await runtime.async_register_motor("m1", transport_for(client), "aa:bb:cc:dd:ee:ff", PIN, "Office")
    return runtime


async def test_runtime_write_requires_a_fresh_read_then_rereads() -> None:
    client = FileClient(file=cbor_encode(CONFIG))
    runtime = await runtime_with(client)
    try:
        refused = await runtime.async_write_config("m1", "NominalSpeed", 3)
        assert refused == {"result": "invalid_payload", "error": "read the motor config first"}
        assert client.writes == []

        read = await runtime.async_read_config("m1")
        assert read["result"] == "success" and read["config"] == CONFIG

        client.file = cbor_encode({**CONFIG, "NominalSpeed": [2800, SPEED]})
        written = await runtime.async_write_config("m1", "NominalSpeed", 2800)
        assert written["result"] == "success"
        assert written["before"] == [2000, SPEED] and written["after"] == [2800, SPEED]
        assert written["confirmed"] is True
        file_ops = [data.hex() for uuid, data in client.writes if uuid == TRANSFER_UUID]
        assert file_ops[4:7] == ["00c40001", write_payload("NominalSpeed", 2800).hex(), "04"]
    finally:
        await runtime.async_shutdown()


async def test_coordinator_refuses_config_exchange_while_busy() -> None:
    coordinator = Coordinator(Controller(lambda: 0.0))
    client = FileClient()
    coordinator.register_motor("m1", transport_for(client), "aa:bb:cc:dd:ee:ff", PIN, "Office")
    coordinator._execution_task = asyncio.current_task()
    try:
        assert await coordinator.async_config_exchange("m1", READ_SEQUENCE) == (Result.BUSY, {})
    finally:
        coordinator._execution_task = None
    assert client.writes == []


async def test_timed_out_write_stops_all_further_io(monkeypatch) -> None:
    from custom_components.somfy_ble import ble as ble_module

    monkeypatch.setattr(ble_module, "BLE_COMMAND_TIMEOUT", 0.01)
    client = FileClient(file=cbor_encode(CONFIG), hang_on=b"\x01")
    transport = transport_for(client)
    result, trace = await transport.async_config_exchange(PIN, READ_SEQUENCE)
    assert result is Result.ERROR
    assert trace["steps"][-1]["error"] == "write timed out"
    # Nothing after the hung SIZE write: no reads, no CLOSE.
    assert [data for _, data in client.writes][-1] == b"\x01"
    reads_after_open = client.reads.count(TRANSFER_UUID)
    assert reads_after_open == 0


async def test_incomplete_read_is_an_error_and_never_authorizes_a_write() -> None:
    client = FileClient(file=cbor_encode(CONFIG), size_extra=10)
    runtime = await runtime_with(client)
    try:
        read = await runtime.async_read_config("m1")
        assert read["result"] == "error" and read["config"] is None
        refused = await runtime.async_write_config("m1", "NominalSpeed", 2800)
        assert refused["error"] == "read the motor config first"
    finally:
        await runtime.async_shutdown()


async def test_unacknowledged_write_is_unconfirmed_and_failed_reread_never_confirms() -> None:
    client = FileClient(file=cbor_encode(CONFIG))
    runtime = await runtime_with(client)
    try:
        assert (await runtime.async_read_config("m1"))["result"] == "success"
        client.fail_on = write_payload("NominalSpeed", 2800)
        client.size_extra = 10  # the re-read comes back incomplete
        written = await runtime.async_write_config("m1", "NominalSpeed", 2800)
        assert written["result"] == "error"
        assert "write unconfirmed" in str(written["trace"])
        assert written["reread_result"] == "error"
        assert written["confirmed"] is False
    finally:
        await runtime.async_shutdown()
