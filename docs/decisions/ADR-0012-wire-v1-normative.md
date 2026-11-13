# ADR-0012: Wire V1 is the normative Classic CAN encoding

Status: **accepted**
Date: 2026-11-13

## Context

`docs/architecture/mcu-wire-v1.md` documents two mutually incompatible Classic CAN
encodings on the same arbitration space, and until this record neither was
normative:

| | **Wire V1** (firmware codec) | **`runtime_can`** (vendored runtime) |
|---|---|---|
| Source | `firmware/mcu/core/frame_codec.c` | `integrations/embodied_agent_runtime/ros2_ws/src/runtime_can/include/runtime_can/protocol.hpp` |
| Version byte | `0x10` (`MCU_WIRE_VERSION_V1`) | `0x01` (`kProtocolVersion`) |
| STOP opcode | `5` (`MCU_WIRE_OPCODE_STOP`) | `0xFF` (`kStopOpcode`) |
| STOP frame id | `0x080` (`MCU_CAN_ID_STOP`) | `0x100` (`kCommandFrameId`) |
| STOP ack id | `0x081` (`MCU_CAN_ID_STOP_ACK`) | — |

Both sides refuse the other's frames, which is the fail-closed behaviour the
document describes and which this ADR keeps. The practical effect of the
disagreement: the firmware that will run on `MCU-BASE` and the runtime the ROS 2
execution plane is built from cannot talk to each other, so whichever side is
flashed first, the robot's bus does not work. The `ros2_control` SocketCAN
interface (#541) cannot land without this decision.

## Decision

**Wire V1 is normative.** The `runtime_can` codec changes to match it; the firmware
codec does not change.

### Why this side

The decision is made on evidence, not on seniority, and the evidence is
asymmetric:

- **Wire V1 has committed golden vectors.** `firmware/mcu/tests/frame_codec_tests.c`
  carries **8 golden vectors** — each a full frame, its arbitration id and its
  eight data bytes — driven through **33 `check()` calls** covering encode, decode
  and rejection. `docs/architecture/mcu-wire-v1.md` is a reviewed binary contract
  with field offsets, not a header comment, and the `MCU_WIRE_*` constants are the
  ones both the QEMU and the host target build against.
- **`runtime_can` has one test file.** `runtime_can/test/test_protocol.cpp` exists
  and is not nothing, but it does not carry golden vectors, and its constants are
  local to one vendored package rather than shared with a contract document.
- **Wire V1 is the fail-closed side.** It has a dedicated STOP ack id (`0x081`)
  and a `MCU_WIRE_FAULT_STOP_REJECTED` fault code, so a rejected stop is a
  distinguishable, recordable outcome. `runtime_can` has no ack id and no
  equivalent fault path, so a stop that failed to land is not observable from its
  frames.

Making the side with golden vectors, a reviewed contract and an explicit
stop-acknowledgement path normative is the choice that keeps the verification
story that already exists. The alternative would invalidate eight committed
vectors and 33 checks to preserve one test file.

## Consequences

**What changes.** `runtime_can`'s constants and frame layout move to Wire V1:
version byte `0x10`, STOP opcode `5`, STOP id `0x080`, STOP ack id `0x081`. Its
`test_protocol.cpp` gains golden vectors copied from the firmware's, so the two
cannot drift back apart.

**What is invalidated.** `runtime_can`'s existing byte-level expectations for the
version byte, the STOP opcode and the STOP id. The number of affected assertions is
whatever `test_protocol.cpp` currently pins; the implementing issue must enumerate
them rather than assert that none exist.

**What is NOT invalidated.** The eight golden vectors and 33 checks in
`firmware/mcu/tests/frame_codec_tests.c`, the `mcu_protocol.schema.json` logical
contract (this is a transport layout question, not a logical one), and the
QEMU/host targets.

**Migration order**, because a bus with one side updated must still fail closed:

1. `runtime_can` gains Wire V1 encode/decode **alongside** its current codec, and
   both are tested. Nothing on the wire changes yet.
2. The ROS 2 execution plane switches to the Wire V1 path. The old path stays
   compiled for one release.
3. The old codec is deleted in a follow-up, after the bench has carried traffic.

**A bus with one side not yet updated** must keep the existing fail-closed
rejection: the updated side refuses a foreign version byte rather than
interpreting it. This is the answer the issue requires, and it is not "undefined
behaviour" — `docs/architecture/mcu-wire-v1.md` already states that both sides
refuse each other's frames, and that behaviour is preserved by step 1 keeping the
old path compiled rather than by making the parser lenient.

**The follow-up implementation issue** is the one that changes `runtime_can`; this
record changes no codec.

## What this ADR does not decide

- **The logical protocol.** `interfaces/json_schema/mcu_protocol.schema.json` is
  unchanged: this is about how bytes reach the wire, not what the message means.
- **FDCAN vs Classic CAN at the transceiver.** Both parts selected in ADR-0011
  are FDCAN-capable; this record is about the Classic CAN frame layout both codecs
  currently speak.
- **The `ros2_control` interface's shape.** #541 is separate.

## Evidence this ADR relies on

- `firmware/mcu/core/frame_codec.h` — the `MCU_WIRE_*` constants.
- `firmware/mcu/tests/frame_codec_tests.c` — eight golden vectors, 33 checks.
- `integrations/embodied_agent_runtime/ros2_ws/src/runtime_can/include/runtime_can/protocol.hpp`
  — `kCommandFrameId = 0x100`, `kProtocolVersion = 1`, `kStopOpcode = 0xFF`.
- `docs/architecture/mcu-wire-v1.md` — the reviewed binary contract.
- `tests/unit/test_can_transport.py::test_rejects_a_foreign_classic_can_encoding`
  — the existing rejection this ADR preserves unchanged.
