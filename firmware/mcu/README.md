# Safety MCU firmware

Legacy target: RISC-V rv32imac on CH32V307. The current BSP `MCU-BASE`
candidate is STM32G474RET6 for FDCAN and requires a separate HAL/PCB ECO.
Decision and rationale: `docs/decisions/ADR-0003-mcu-riscv-qemu.md`.

## Layout

```
core/           platform-independent C. State machine, frame codec,
                HAL/Wire bridge, watchdog timing, dedup, ring buffers.
                No peripheral registers. No vendor headers.
hal/qemu/       QEMU target, CTU CAN FD over PCI. Used by CI.
hal/ch32v307/   legacy real board, CH32V307 classic CAN peripheral. P3.
hal/host/       x86_64 build, for fast logic tests.
tests/          shared test suite, runs against all three targets.
```

`core/` compiles to three targets from one source. Changing the board means
writing a new `hal/`, not touching `core/`.

## The one rule

`core/` must not include a vendor or platform header. Any register access
belongs in `hal/`. An `#ifdef CH32V307` inside `core/` means the boundary has
been violated.

## Authority boundary

The C safety state machine under `core/` and its shared Host/QEMU transition
suite are authoritative for MCU safety behavior. The allocation-free Wire V1
codec under `core/frame_codec.[ch]`, its binary contract in
`docs/architecture/mcu-wire-v1.md`, and the shared Host/QEMU golden vectors are
authoritative for the Classic CAN payload encoding. `firmware/virtual_mcu/` is
retired as a safety reference and parity oracle. It remains only as a legacy
compatibility stub for earlier Python consumers and is not evidence of C
protocol, firmware, or physical safety behavior. Changes to that model require
a separate issue and must not silently be treated as C parity work.

## What QEMU proves and doesn't

QEMU models SJA1000 and CTU CAN FD, not the CH32V307 CAN peripheral.

| Proven in QEMU | Requires the board |
|---|---|
| State machine transitions | CH32V307 CAN register behaviour |
| Watchdog timing under a real timer interrupt | Bit timing (BRP/TSEG1/TSEG2/SJW) |
| Dedup across sequence wraparound | Error frames, bus-off recovery |
| Frame codec, ID partition enforcement | Electrical behaviour, EMI |
| Raw-envelope rejection and STOP-first bridge logic | Controller FIFO/IRQ and wire arbitration |
| Absence of malloc and FP instructions | Brownout, power-on reset |

## Build

```bash
make host        # x86_64 library for fast tests
make qemu        # rv32imac ELF for QEMU
make board       # rv32imac ELF to flash (P3)

make test-host   # logic tests, seconds
make test-host-sanitize  # Host corpus under ASan and UBSan
make test-qemu   # fault suite in QEMU, what CI runs
```

## Status

The platform-independent C safety state machine is implemented by Issue #53.
Issue #54 adds the strict Classic CAN Wire V1 codec and shared Host/QEMU golden
vectors. Issue #60 adds the allocation-free heartbeat watchdog, bounded STOP
acknowledgement timing, fake-clock tests and QEMU machine-timer/watchdog
evidence. Issue #61 adds the fixed-memory ordinary-command replay window,
trusted startup-session gate and shared Host/QEMU wraparound corpus. Issue #180
adds the strict raw HAL/Wire V1 bridge and bounded Host fake CAN transport. The
QEMU and physical target CAN drivers, six-domain arbitration and physical CAN
validation remain separate owner-gated follow-up work and are `NOT_EXECUTED`.

## CAN HAL/Wire boundary

`core/can_bridge.[ch]` is the only raw-envelope mapping. The HAL exposes an
11-bit `arbitration_id`, DLC, explicit extended/RTR/error/FD flags and eight
data bytes. The logical 16-bit `command_id` remains in Wire V1 payload bytes
1..2. Only a flag-free, DLC-8 standard frame with a known Wire V1 ID can be
decoded; malformed or wrong-direction traffic produces no state-machine event.

MCU ingress checks STOP before ordinary command routing. A valid STOP bypasses
the ordinary startup-session gate and dedup window, and its ACK is confirmed
only after `hal_can_send()` accepts the transport handoff. The complete
contract, fake evidence and physical/multi-node limits are documented in
`docs/architecture/mcu-can-hal-boundary-v1.md`.

## Timing safety path

`core/watchdog.[ch]` owns the timing state but not timer or watchdog registers.
Only a complete, valid and serially new ordinary frame may refresh the
software link watchdog. Malformed, retry, duplicate, stale and STOP traffic do
not extend the execution deadline. A missed deadline enters the existing
latched `FAULT/watchdog_expired` state and emits one telemetry record.

A valid STOP immediately transitions the state machine to `SAFE_STOP` and
creates a correlated `STOP_ACK` handoff record. The transport must confirm the
handoff before the controlled deadline; otherwise the core emits one local
`STOP_TIMEOUT` outcome and never claims stopped confirmation. The exact
constants and clock-wrap rules are documented in
`docs/architecture/mcu-watchdog-v1.md`.

While the STOP handoff is pending, an equal `retry_count` is an exact
link-level replay and a strictly greater count is a protocol-level retry.
Decreasing or wrapped retry counts are stale and rejected without changing the
pending ACK correlation.

## Ordinary command replay protection

`core/command_dedup.[ch]` is the platform-independent entry for decoded
ordinary commands. It keeps eight cached semantic ACK records, applies the
frozen 15-bit half-range comparison, clears pre-wrap records before accepting a
new serial epoch, and never allocates. Exact duplicates and increasing
protocol retries replay the original result without dispatching another safety
event or refreshing the watchdog. Conflicting, decreasing, stale and evicted
attempts fail closed with `duplicate_frame`.

Boot starts with ordinary command dispatch closed. The transport must drain
queued pre-session traffic before opening the trusted session gate; an ordinary
safety reset does not erase replay history. STOP remains on the independent
watchdog path and cannot be consumed by a full or closed ordinary window. The
exact algorithm and evidence limits are documented in
`docs/architecture/mcu-command-dedup-v1.md`.
