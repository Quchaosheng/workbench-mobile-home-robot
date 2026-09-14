# MCU CAN HAL Boundary V1

Status: **platform-independent bridge implemented; physical target transport
NOT_EXECUTED** for Issue #180.

This boundary connects the raw CAN controller envelope in
`firmware/mcu/core/hal.h` to the frozen MCU CAN Wire V1 codec. It removes the
former ambiguity between a CAN arbitration identifier and a logical command
identifier without changing any Wire V1 number or payload byte.

## Identifier boundary

The two identifiers have different widths and authorities:

| Name | Width | Location | Meaning |
| --- | ---: | --- | --- |
| `arbitration_id` | 11 bits | CAN envelope | Selects one Wire V1 frame kind and arbitration priority. |
| `command_id` | 16 bits | payload bytes 1..2 | Correlates an ordinary command or STOP and selects its disjoint ID partition. |

A STOP therefore uses arbitration ID `0x080` while its payload command ID is
`0x8000..0xffff`. A target HAL must never write the logical command ID into a
CAN arbitration register.

`hal_can_frame` contains:

- `arbitration_id`, which must be at most `0x7ff`;
- `dlc`;
- an explicit flags byte for extended-ID, RTR, error and CAN FD metadata; and
- eight Classic CAN data bytes.

The flags are not C bit-fields. Every target must translate its controller
status into the declared masks explicitly, avoiding compiler-specific layout.

## One encoding authority

`mcu_can_bridge_encode()` calls `mcu_frame_encode()` and then constructs a raw
standard Classic CAN data envelope. `mcu_can_bridge_decode()` validates the raw
envelope before calling `mcu_frame_decode()`. There is no second frame-kind or
payload mapping in the HAL.

The decoder publishes no logical frame unless all of these conditions pass:

1. flags are exactly `HAL_CAN_FRAME_FLAG_NONE`;
2. `arbitration_id <= 0x7ff`;
3. DLC is exactly eight;
4. the arbitration ID is one of the five frozen Wire V1 IDs; and
5. version, reserved bytes, enum values, ID partitions and cross-field
   semantics all pass the existing codec.

Extended, RTR, error, CAN FD, unknown-flag, out-of-range-ID, unknown-ID,
wrong-DLC and malformed Wire V1 inputs produce a bounded bridge rejection
record. They do not dispatch a state-machine event, create an ACK, enter replay
history or refresh the software watchdog. If execution is already active,
malformed traffic cannot keep it alive; only a valid serially new ordinary
command may refresh the existing deadline.

## MCU ingress direction and routing

The MCU ingress accepts only `STOP` and ordinary `COMMAND` kinds:

```text
raw HAL envelope
  -> strict envelope validation
  -> mcu_frame_decode()
  -> STOP: mcu_watchdog_receive_stop()
  -> COMMAND: mcu_command_dedup_receive()
  -> response: mcu_frame_encode() -> hal_can_send()
```

The STOP branch is tested before ordinary-command handling. It bypasses both
the trusted ordinary-session gate and the fixed replay window; a missing or
corrupt ordinary dedup object cannot suppress this path. ACK, STOP_ACK and
telemetry frames are valid MCU-originated encodings, but are rejected as
wrong-direction traffic if received by this MCU ingress and never reach safety
state.

`mcu_can_bridge_poll()` consumes at most one HAL frame per call. The target is
the single writer of the bridge, state machine, watchdog and dedup objects; it
must serialize ISR/task ownership and configure controller filters/FIFO policy
so STOP priority is preserved. The core does not create a hidden or unbounded
receive queue.

## Response handoff

`hal_can_send() == true` means only that the complete encoded frame was handed
to the target transport. It does not prove arbitration, wire delivery, remote
receipt, motor state or physical stopping.

For an accepted STOP, a successful HAL handoff confirms the existing bounded
STOP_ACK slot through `mcu_watchdog_confirm_stop_ack()`. If the HAL rejects the
send, the ACK remains pending and the existing STOP timeout/retry policy stays
active. Ordinary ACK results remain in the dedup cache, so a host retry can
request the same semantic response without executing the command again.

## Priority evidence

Wire V1 retains the strict numeric order:

```text
STOP 0x080 < STOP_ACK 0x081 < COMMAND 0x100 < ACK 0x101 < TELEMETRY 0x180
```

The bounded Host fake accepts a set of pending frames and exposes the lowest
arbitration ID first, retaining insertion order for equal IDs. A regression
queues an ordinary command before STOP and proves STOP is still dispatched and
handed off first while the ordinary session is closed. This is deterministic
logic evidence only; it is not bus timing, controller FIFO, ISR latency or
electrical arbitration evidence.

## Six-domain BSP compatibility gate

BSP V0.1 assigns logical node IDs to `MCU-BASE`, both arm controllers, both
tool controllers and `MCU-SAFETY`, but its concrete multi-node arbitration
bit allocation is still an implementation gate. Wire V1 currently assigns all
11 arbitration bits to five exact frame-kind IDs and carries no node field in
the eight-byte payload. Multiple independently responding domains therefore
cannot be placed on one shared bus by silently OR-ing a node ID into these
values: that would change the frozen contract and can create colliding ACKs.

This bridge remains a single logical Wire V1 endpoint until the Protocol,
Firmware, Linux and Electrical owners approve one of the following in a
separate versioned decision:

- a new arbitration layout with explicit source/destination ownership;
- physically separated buses that preserve the existing IDs; or
- a new payload/transport version with bounded node addressing.

No option is selected or inferred by Issue #180's platform-independent slice.

## Target evidence matrix

| Target | Bridge/codec logic | CAN transport | Evidence boundary |
| --- | --- | --- | --- |
| Host fake | `PASS` | `PASS` for bounded fake queues | No controller, wire or physical timing. |
| RISC-V QEMU | `PASS` | `NOT_EXECUTED` | `hal_can_init/send/recv` remain false-returning stubs. |
| Legacy CH32V307 target | build source absent | `NOT_EXECUTED` | No board HAL, linker/startup package or board run. |
| BSP STM32G474RET6 / STM32G0B1 | target absent | `NOT_EXECUTED` | No approved clock, pin, FDCAN, transceiver, filter, IRQ, linker or vendor HAL inputs. |

CH32V307 remains a legacy PCB/firmware target only. Its CAN peripheral is
classic CAN 2.0B, so it must not be used as the `MCU-BASE` endpoint on the
CAN-FD backbone without an explicit architecture change and bus downgrade.

The Host/QEMU tests prove envelope validation, codec reuse, direction gates,
STOP-first dispatch and transport-handoff state semantics. They do not prove a
real STM32 peripheral, six-domain arbitration, bitrate, bus-off recovery,
physical CAN, actuators, E-stop behavior or hard-real-time deadlines.
