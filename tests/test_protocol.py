from __future__ import annotations

import sys
from pathlib import Path
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from custom_components.somfy_ble.protocol import (  # noqa: E402
    Characteristic,
    Command,
    CommandPacket,
    JogDirection,
    encode_pin,
    encode_step,
    make_command,
    somfy_uuid,
)


class ProtocolTest(unittest.TestCase):
    def test_encode_pin_treats_six_digit_printed_pins_as_hex(self) -> None:
        self.assertEqual(encode_pin("012345"), bytes((0x01, 0x23, 0x45)))
        self.assertEqual(encode_pin("987654"), bytes((0x98, 0x76, 0x54)))

    def test_encode_pin_keeps_decimal_for_other_lengths(self) -> None:
        self.assertEqual(encode_pin("12345"), (12345).to_bytes(3, "little"))

    def test_encode_pin_preserves_explicit_three_byte_hex_pin(self) -> None:
        self.assertEqual(encode_pin("16777215"), bytes((0xFF, 0xFF, 0xFF)))
        self.assertEqual(encode_pin("0xA1B2C3"), bytes((0xA1, 0xB2, 0xC3)))
        self.assertEqual(encode_pin("FE8D0B"), bytes((0xFE, 0x8D, 0x0B)))

    def test_encode_pin_rejects_overlong_decimal_text_without_echoing_the_pin(self) -> None:
        overlong_pin = "000000001"
        with self.assertRaises(ValueError) as context:
            encode_pin(overlong_pin)
        self.assertNotIn(overlong_pin, str(context.exception))

    def test_encode_pin_rejects_malformed_and_out_of_range_values(self) -> None:
        for pin in ("", "1234Ga", "0x12345", "0x12345G", "16777216", "0x1234567"):
            with self.subTest(pin=pin), self.assertRaises(ValueError):
                encode_pin(pin)

    def test_encode_step_is_unsigned_16_bit_little_endian_at_protocol_boundaries(self) -> None:
        self.assertEqual(encode_step(1), bytes((0x01, 0x00)))
        self.assertEqual(encode_step(256), bytes((0x00, 0x01)))
        self.assertEqual(encode_step(500), bytes((0xF4, 0x01)))

    def test_encode_step_rejects_values_outside_protocol_range(self) -> None:
        for step in (0, 501, 65535):
            with self.subTest(step=step), self.assertRaises(ValueError):
                encode_step(step)

    def test_somfy_uuid_formats_identifier_with_authoritative_suffix(self) -> None:
        suffix = "-cad9-46c6-a2ea-2ca16d57b4a5"
        self.assertEqual(somfy_uuid(0), "00000000" + suffix)
        self.assertEqual(somfy_uuid(0x10009), "00010009" + suffix)

    def test_characteristic_holds_key_identifier_uuid_and_label(self) -> None:
        self.assertEqual(Characteristic.AUTH.identifier, 0x0000000B)
        self.assertEqual(Characteristic.AUTH.uuid, "0000000b-cad9-46c6-a2ea-2ca16d57b4a5")
        self.assertEqual(Characteristic.AUTH.label, "PIN authentication")
        self.assertEqual(len({item.identifier for item in Characteristic}), len(Characteristic))

    def test_make_command_preserves_exact_characteristic_and_payload_vectors(self) -> None:
        vectors = (
            (Command.IDENTIFY, JogDirection.UP, 100, "00000001-cad9-46c6-a2ea-2ca16d57b4a5", b"\x01"),
            (Command.STOP, JogDirection.UP, 100, "00000006-cad9-46c6-a2ea-2ca16d57b4a5", b"\x01"),
            (Command.JOG, JogDirection.UP, 500, "00010009-cad9-46c6-a2ea-2ca16d57b4a5", b"\xf4\x01"),
            (Command.JOG, JogDirection.DOWN, 256, "00010008-cad9-46c6-a2ea-2ca16d57b4a5", b"\x00\x01"),
            (Command.RANGE_START, JogDirection.UP, 100, "0001000b-cad9-46c6-a2ea-2ca16d57b4a5", b"\x00\x00"),
            (Command.SET_UPPER_LIMIT, JogDirection.UP, 100, "00010007-cad9-46c6-a2ea-2ca16d57b4a5", b"\x00"),
            (Command.SET_LOWER_LIMIT, JogDirection.UP, 100, "00010007-cad9-46c6-a2ea-2ca16d57b4a5", b"\x01"),
            (Command.SET_DIRECTION_COUNTERCLOCKWISE, JogDirection.UP, 100, "00010005-cad9-46c6-a2ea-2ca16d57b4a5", b"\x00"),
            (Command.SET_DIRECTION_CLOCKWISE, JogDirection.UP, 100, "00010005-cad9-46c6-a2ea-2ca16d57b4a5", b"\x01"),
        )
        for command, direction, step, uuid, payload in vectors:
            with self.subTest(command=command, direction=direction):
                packet = make_command(command, direction=direction, step=step)
                self.assertIsInstance(packet, CommandPacket)
                self.assertEqual(packet.characteristic_uuid, uuid)
                self.assertEqual(packet.payload, payload)

    def test_command_packet_is_immutable_without_a_dict_mutation_path(self) -> None:
        packet = make_command(Command.IDENTIFY)
        self.assertIsInstance(packet.payload, bytes)
        with self.assertRaises(AttributeError):
            packet.payload = b"\x00"  # type: ignore[misc]
        with self.assertRaises(AttributeError):
            packet.__dict__  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
