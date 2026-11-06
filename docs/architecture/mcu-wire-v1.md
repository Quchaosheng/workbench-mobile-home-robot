# MCU CAN Wire V1

Status: **firmware-owned binary contract** for Issue #54, connected to the raw
HAL envelope by Issue #180.

This document maps the frozen logical MCU protocol v1.0 into one Classic CAN
data frame. The logical contract remains normative for frame meaning. Wire V1
defines only transport encoding and does not change
`interfaces/json_schema/mcu_protocol.schema.json` or the exported Pydantic
models.

## Transport boundary

- Classic CAN 2.0 data frames with standard 11-bit identifiers.
- DLC is exactly 8 for every frame kind. Remote, extended-ID and CAN FD frames
  are outside this codec.
- Multi-byte integers use network byte order (big-endian).
- Byte 0 is the compact protocol version. Logical version `"1.0"` is `0x10`.
- CAN's frame CRC is the transport integrity check; Wire V1 adds no payload
  checksum.

The raw controller boundary and rejection order are defined in
`docs/architecture/mcu-can-hal-boundary-v1.md`. In particular,
`hal_can_frame.arbitration_id` is the 11-bit value in the table below, while
the logical 16-bit `command_id` remains inside payload bytes 1..2.

The logical `frame_id`, `sent_at_us` and `clock_id` fields are adapter/evidence
metadata and are not transmitted in the eight-byte CAN payload. A bridge owns
their local generation and retention. They are never reconstructed as remote
timestamps or used as cross-device freshness evidence.

## Not the only classic-CAN encoding in the ecosystem

A companion runtime (`embodied-agent-runtime`, `ros2_ws/src/runtime_can/`) speaks a
**different** classic-CAN layout on the same arbitration IDs. This is recorded here so
nobody discovers it by diffing two headers:

| | Wire V1 (here) | `runtime_can` |
| --- | --- | --- |
| byte 0 version | `0x10` | `0x01` |
| byte 4 | `retry_count` | `argument` bits 31..24 |
| bytes 5..7 | reserved `0x00` | `argument` bits 23..0 |
| ordinary command / STOP | IDs `0x100` / `0x080` | both on `0x100` |
| STOP opcode | `5` | `0xFF` |
| STOP payload | ID `>= 0x8000` | ID `>= 0x8000`, original ID in `argument` |
| ACK | `0x101` | `0x101` |
| telemetry | `0x180` | not implemented |

Both codecs refuse the other's frames, which is the intended fail-closed behaviour:
feeding a `runtime_can` command frame (`01 00 01 01 ...` on `0x100`) to
`decode_can_frame` raises `unsupported CAN Wire version`, and that rejection is pinned by
`tests/unit/test_can_transport.py::test_rejects_a_foreign_classic_can_encoding`.

The two encodings are therefore **not** interoperable, and nothing in this repository may
claim they are. `integrations/ros2_runtime/` consumes the runtime's `TaskEvent` over a
captured JSONL file; it is a software-evidence boundary and does not imply bus
compatibility. Making them interoperable — adopting one layout, or adding a bridge —
requires an owner decision recorded under `docs/decisions/` before any codec changes.

## Arbitration identifiers

| CAN ID | Frame kind | Direction | Priority rationale |
| --- | --- | --- | --- |
| `0x080` | `stop` | host to MCU | Highest protocol priority. |
| `0x081` | `stop_ack` | MCU to host | Correlated safety response. |
| `0x100` | `command` | host to MCU | Ordinary command traffic. |
| `0x101` | `ack` | MCU to host | Ordinary correlated response. |
| `0x180` | `telemetry` | MCU to host | Lowest protocol priority. |

Lower identifiers win CAN arbitration. The ID selects exactly one frame kind;
all other standard identifiers are rejected by the codec.

## Payload layouts

All offsets are zero-based and every reserved byte must be `0x00`.

### Command and STOP

| Byte | Field |
| --- | --- |
| 0 | version (`0x10`) |
| 1..2 | `command_id`, unsigned 16-bit big-endian |
| 3 | opcode |
| 4 | `retry_count` |
| 5..7 | reserved zero |

`command` accepts IDs `0x0000..0x7fff` and ordinary opcodes only. `stop`
accepts IDs `0x8000..0xffff` and opcode `stop` only.

### ACK and STOP_ACK

| Byte | Field |
| --- | --- |
| 0 | version (`0x10`) |
| 1..2 | `command_id`, unsigned 16-bit big-endian |
| 3 | opcode |
| 4 | echoed `retry_count` |
| 5 | `result_code` |
| 6 | `fault_code` |
| 7 | `device_mode` |

`ack` accepts the ordinary ID/opcode partition. `stop_ack` accepts the STOP
partition and opcode. Result, fault and mode combinations must satisfy the
frozen logical protocol; encoding a numeric enum value is not sufficient.

### Telemetry

| Byte | Field |
| --- | --- |
| 0 | version (`0x10`) |
| 1..4 | `sequence_no`, unsigned 32-bit big-endian |
| 5 | `fault_code` |
| 6 | `device_mode` |
| 7 | reserved zero |

Telemetry has no command ID, opcode, retry count or result code and never
confirms a command.

## Numeric registries

| Opcode | Value |
| --- | --- |
| reserved | `0x00` |
| `move` | `0x01` |
| `grip_open` | `0x02` |
| `grip_close` | `0x03` |
| `hold` | `0x04` |
| `stop` | `0x05` |
| `heartbeat` | `0x06` |

| Result | Value |
| --- | --- |
| accepted | `0x00` |
| rejected | `0x01` |

| Fault | Value | Valid MCU frame |
| --- | --- | --- |
| `none` | `0x00` | ACK, STOP_ACK, telemetry as constrained by result/mode |
| `ack_timeout` | `0x01` | never; host-only diagnostic |
| `stop_timeout` | `0x02` | never; host-only diagnostic |
| `stop_rejected` | `0x03` | failed STOP_ACK only |
| `link_lost` | `0x04` | fault telemetry only |
| `duplicate_frame` | `0x05` | failed ordinary ACK only |
| `watchdog_expired` | `0x06` | fault telemetry only |
| `malformed_frame` | `0x07` | failed ordinary ACK only |

| Device mode | Value |
| --- | --- |
| `idle` | `0x00` |
| `moving` | `0x01` |
| `holding` | `0x02` |
| `stopped` | `0x03` |
| `faulted` | `0x04` |

All unlisted enum values are reserved and invalid.

## Canonical golden vector

The committed `interfaces/examples/mcu-frame-stop-ack.json` describes a
successful STOP acknowledgement with command ID 32769 (`0x8001`), zero retry,
no fault and stopped mode. Its Wire V1 representation is:

```text
CAN ID: 0x081
DLC:    8
DATA:   10 80 01 05 00 00 00 03
```

The JSON `frame_id`, `sent_at_us` and `clock_id` remain adapter metadata as
defined above. The shared Host/QEMU C test corpus pins this byte vector.

## Fail-closed behavior and limits

The decoder rejects the complete frame before publishing output when the ID,
DLC, version, reserved bytes, enum values, ID partition or cross-field result
semantics are invalid. The encoder validates the complete logical wire object
and destination capacity before writing any output byte.

Wire V1 does not implement command deduplication, watchdog scheduling, host
transport dispatch, CAN controller registers, bus-off recovery, multi-node
addressing or electrical validation. The Issue #180 bridge validates the raw
envelope and routes decoded frames, but real target drivers and physical
evidence remain separate owner-gated work.
