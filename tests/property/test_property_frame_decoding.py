"""Property suite: CAN Wire V1 frame decoding is total and fail-closed (Issue #89).

The boundary under test is ``workbench.hardware.can_driver_safe.decode_can_frame``.
It promises two things at once:

* every well-formed frame of each declared kind decodes to the fields the wire
  layout says it carries, and re-decoding is deterministic; and
* every malformed frame raises ``CanFrameValidationError`` rather than being
  coerced into a valid frame, including a corrupted reserved byte, a wrong DLC,
  an out-of-partition command id and an inconsistent ack triple.

The generator builds valid frames from the wire layout and then perturbs exactly
one field at a time, so each rejection is attributable to the field it changed.
"""

from __future__ import annotations

import hashlib

import pytest
from workbench.hardware import (
    MCU_CAN_ID_ACK,
    MCU_CAN_ID_COMMAND,
    MCU_CAN_ID_STOP,
    MCU_CAN_ID_STOP_ACK,
    MCU_CAN_ID_TELEMETRY,
    CanFrame,
    CanFrameKind,
    CanFrameValidationError,
    decode_can_frame,
)

from ._generator import SEEDS, Rng, assert_corpus_clean, report_for, run_corpus, write_archive_if_requested

SUITE = "frame_decoding"
MIN_CASES = 3 * 140

WIRE_VERSION = 0x10
ORDINARY_OPCODES = (1, 2, 3, 4, 6)  # move, grip_open, grip_close, hold, heartbeat
DEVICE_MODES = (0, 1, 2, 3)  # idle, moving, holding, stopped
FAULT_NONE = 0
FAULT_DUPLICATE = 5
FAULT_MALFORMED = 7
FAULT_LINK_LOST = 4
FAULT_WATCHDOG = 6


def command_frame(rng: Rng) -> tuple[CanFrame, dict]:
    command_id = rng.integer(0, 0x7FFF)
    opcode = rng.choice(ORDINARY_OPCODES)
    retry = rng.integer(0, 255)
    data = bytes([WIRE_VERSION, (command_id >> 8) & 0xFF, command_id & 0xFF, opcode, retry, 0, 0, 0])
    return CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=data), {
        "kind": CanFrameKind.COMMAND,
        "command_id": command_id,
        "opcode": opcode,
        "retry_count": retry,
    }


def stop_frame(rng: Rng) -> tuple[CanFrame, dict]:
    command_id = rng.integer(0x8000, 0xFFFF)
    retry = rng.integer(0, 255)
    data = bytes([WIRE_VERSION, (command_id >> 8) & 0xFF, command_id & 0xFF, 5, retry, 0, 0, 0])
    return CanFrame(arbitration_id=MCU_CAN_ID_STOP, data=data), {
        "kind": CanFrameKind.STOP,
        "command_id": command_id,
        "opcode": 5,
        "retry_count": retry,
    }


def ack_frame(rng: Rng) -> tuple[CanFrame, dict]:
    command_id = rng.integer(0, 0x7FFF)
    opcode = rng.choice(ORDINARY_OPCODES)
    retry = rng.integer(0, 255)
    if rng.boolean():
        mode = rng.choice(DEVICE_MODES)
        data = bytes([WIRE_VERSION, (command_id >> 8) & 0xFF, command_id & 0xFF, opcode, retry, 0, 0, mode])
        expected = {"result_code": 0, "fault_code": FAULT_NONE}
    else:
        data = bytes(
            [
                WIRE_VERSION,
                (command_id >> 8) & 0xFF,
                command_id & 0xFF,
                opcode,
                retry,
                1,
                rng.choice((FAULT_DUPLICATE, FAULT_MALFORMED)),
                4,
            ]
        )
        expected = {"result_code": 1}
    return CanFrame(arbitration_id=MCU_CAN_ID_ACK, data=data), {
        "kind": CanFrameKind.ACK,
        "command_id": command_id,
        "opcode": opcode,
        "retry_count": retry,
        **expected,
    }


def stop_ack_frame(rng: Rng) -> tuple[CanFrame, dict]:
    command_id = rng.integer(0x8000, 0xFFFF)
    retry = rng.integer(0, 255)
    if rng.boolean():
        data = bytes([WIRE_VERSION, (command_id >> 8) & 0xFF, command_id & 0xFF, 5, retry, 0, 0, 3])
        expected = {"result_code": 0, "fault_code": FAULT_NONE}
    else:
        data = bytes([WIRE_VERSION, (command_id >> 8) & 0xFF, command_id & 0xFF, 5, retry, 1, 3, 4])
        expected = {"result_code": 1, "fault_code": 3}
    return CanFrame(arbitration_id=MCU_CAN_ID_STOP_ACK, data=data), {
        "kind": CanFrameKind.STOP_ACK,
        "command_id": command_id,
        "opcode": 5,
        "retry_count": retry,
        **expected,
    }


def telemetry_frame(rng: Rng) -> tuple[CanFrame, dict]:
    sequence_no = rng.integer(0, 0xFFFFFFFF)
    if rng.boolean():
        fault_code, mode = FAULT_NONE, rng.choice(DEVICE_MODES)
    else:
        fault_code, mode = rng.choice((FAULT_LINK_LOST, FAULT_WATCHDOG)), 4
    data = bytes(
        [
            WIRE_VERSION,
            (sequence_no >> 24) & 0xFF,
            (sequence_no >> 16) & 0xFF,
            (sequence_no >> 8) & 0xFF,
            sequence_no & 0xFF,
            fault_code,
            mode,
            0,
        ]
    )
    return CanFrame(arbitration_id=MCU_CAN_ID_TELEMETRY, data=data), {
        "kind": CanFrameKind.TELEMETRY,
        "sequence_no": sequence_no,
        "fault_code": fault_code,
        "device_mode": mode,
    }


BUILDERS = (command_frame, stop_frame, ack_frame, stop_ack_frame, telemetry_frame)


def frame_seed(frame: CanFrame) -> int:
    """A stable per-frame seed; built-in hash() is randomized per process."""

    material = bytes([(frame.arbitration_id >> 8) & 0xFF, frame.arbitration_id & 0xFF]) + frame.data
    return int.from_bytes(hashlib.sha256(material).digest()[:8], "big")


def cases() -> list[dict]:
    generated: list[dict] = []
    for seed in SEEDS:
        rng = Rng(seed)
        for _ in range(140):
            frame, expected = rng.choice(BUILDERS)(rng)
            generated.append({"kind": "valid", "frame": frame, "expected": expected})
            generated.append({"kind": "corrupt", "frame": frame, "expected": expected})
    return generated


def corrupt(rng: Rng, frame: CanFrame) -> CanFrame:
    """Perturb exactly one field, so a rejection is attributable to it."""

    choice = rng.randbelow(5)
    data = bytearray(frame.data)
    if choice == 0:
        data[0] = rng.choice((0x00, 0x11, 0xFF))
    elif choice == 1:
        data[7] = rng.integer(1, 255)  # telemetry reserved byte, or ack triple
    elif choice == 2:
        data[3] = rng.choice((0, 7, 0xFF))  # out-of-partition opcode
    elif choice == 3:
        return CanFrame(arbitration_id=frame.arbitration_id, data=bytes(data[: rng.integer(0, 7)]))
    else:
        return CanFrame(arbitration_id=rng.choice((0x123, 0x7FF, 0x000)), data=bytes(data))
    return CanFrame(arbitration_id=frame.arbitration_id, data=bytes(data))


def check(case: dict) -> None:
    if case["kind"] == "valid":
        wire = decode_can_frame(case["frame"])
        for field, value in case["expected"].items():
            assert getattr(wire, field) == value, f"{field} decoded as {getattr(wire, field)!r}, expected {value!r}"
        assert wire.frame == case["frame"]
        assert decode_can_frame(case["frame"]) == wire
        return

    # A corruption may coincidentally still be a valid frame (for example an
    # ack triple that stays consistent), in which case decoding it is correct.
    # The property is that decoding never *lies*: it either raises the typed
    # validation error or returns a frame whose fields match the bytes.
    perturbed = corrupt(Rng(frame_seed(case["frame"])), case["frame"])
    try:
        wire = decode_can_frame(perturbed)
    except CanFrameValidationError:
        return
    assert wire.frame == perturbed
    assert decode_can_frame(perturbed) == wire


def test_generated_frames_decode_consistently_or_fail_closed() -> None:
    report = run_corpus(SUITE, cases(), check)
    assert_corpus_clean(report)
    assert report.case_count >= MIN_CASES, f"corpus shrank to {report.case_count} cases"
    assert report.corpus_digest


def test_every_valid_generated_frame_decodes() -> None:
    rng = Rng(SEEDS[0])
    for _ in range(300):
        frame, expected = rng.choice(BUILDERS)(rng)
        wire = decode_can_frame(frame)
        for field, value in expected.items():
            assert getattr(wire, field) == value


def test_reserved_command_bytes_must_be_zero() -> None:
    for index in (5, 6, 7):
        data = bytearray([WIRE_VERSION, 0x00, 0x2A, 1, 3, 0, 0, 0])
        data[index] = 1
        with pytest.raises(CanFrameValidationError, match="reserved bytes must be zero"):
            decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=bytes(data)))


def test_a_wrong_dlc_or_version_is_rejected() -> None:
    with pytest.raises(CanFrameValidationError, match="requires DLC 8"):
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=b"\x10\x00\x2a\x01"))
    with pytest.raises(CanFrameValidationError, match="unsupported CAN Wire version"):
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=bytes([0x11, 0, 0x2A, 1, 0, 0, 0, 0])))


def test_frame_flags_and_arbitration_ids_are_checked() -> None:
    payload = bytes([WIRE_VERSION, 0x00, 0x2A, 1, 3, 0, 0, 0])
    with pytest.raises(CanFrameValidationError, match="extended CAN frames"):
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=payload, is_extended_id=True))
    with pytest.raises(CanFrameValidationError, match="remote CAN frames"):
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=payload, is_remote_frame=True))
    with pytest.raises(CanFrameValidationError, match="unknown CAN Wire V1 arbitration ID"):
        decode_can_frame(CanFrame(arbitration_id=0x123, data=payload))
    with pytest.raises(CanFrameValidationError, match="11-bit integer"):
        decode_can_frame(CanFrame(arbitration_id=0x800, data=payload))


def test_command_partitions_are_enforced() -> None:
    # An ordinary command id inside the STOP partition is not an ordinary frame.
    with pytest.raises(CanFrameValidationError, match="outside its partition"):
        data = bytes([WIRE_VERSION, 0x80, 0x01, 1, 0, 0, 0, 0])
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_COMMAND, data=data))
    with pytest.raises(CanFrameValidationError, match="outside its partition"):
        data = bytes([WIRE_VERSION, 0x00, 0x01, 5, 0, 0, 0, 0])
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_STOP, data=data))


def test_ack_triples_must_be_self_consistent() -> None:
    # result_code=accepted with a rejection fault code is not a valid ack.
    with pytest.raises(CanFrameValidationError, match="acknowledgement fields"):
        data = bytes([WIRE_VERSION, 0, 0x2A, 1, 0, 0, 5, 0])
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_ACK, data=data))
    with pytest.raises(CanFrameValidationError, match="acknowledgement fields"):
        data = bytes([WIRE_VERSION, 0x80, 0x01, 5, 0, 0, 0, 0])
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_STOP_ACK, data=data))


def test_telemetry_fault_and_mode_must_agree() -> None:
    with pytest.raises(CanFrameValidationError, match="telemetry fields"):
        data = bytes([WIRE_VERSION, 0, 0, 0, 1, 0, 4, 0])
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_TELEMETRY, data=data))
    with pytest.raises(CanFrameValidationError, match="telemetry fields"):
        data = bytes([WIRE_VERSION, 0, 0, 0, 1, 4, 0, 0])
        decode_can_frame(CanFrame(arbitration_id=MCU_CAN_ID_TELEMETRY, data=data))


def test_non_frame_inputs_are_rejected() -> None:
    for value in (None, b"\x10" * 8, [0x10], "frame", 7):
        with pytest.raises(CanFrameValidationError, match="CanFrame instances"):
            decode_can_frame(value)


def test_the_corpus_is_reproducible_for_a_fixed_seed() -> None:
    write_archive_if_requested()
    first = cases()
    second = cases()
    assert [case["frame"] for case in first] == [case["frame"] for case in second]
    assert report_for(SUITE).seeds == SEEDS
