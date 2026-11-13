# ADR-0003: Safety MCU targets RISC-V, verified in QEMU

Status: **superseded by [ADR-0011](ADR-0011-stm32-mcu-selection.md)**
Date: 2026-08-05

> Superseded 2026-11-13. The board carries `STM32G474RET6` and `STM32G0B1`
> (both FDCAN), and QEMU models neither part's CAN peripheral, so the
> verification-cost argument below no longer favours RISC-V. The two-layer
> `core/` + `hal/` split this record established survives and is carried forward.
> The rest of the record is left as written.

## Context

`firmware/virtual_mcu/` is a Python state machine. It is a useful contract stub
— Motion can build against it before any hardware exists — but it proves
nothing about firmware. A Python class asserting that it entered `safe_stop`
does not show that a cross-compiled binary does the same under a real timer
interrupt.

No MCU part had been chosen. Choosing one late means the whole firmware effort
lands in the last month, next to real-hardware bring-up, which is where
schedule risk is already highest.

## Decision

The safety MCU targets **RISC-V (rv32imac)**. The reference part is the
**CH32V307** (on-chip CAN, ~¥10, single-purpose enough for a safety element).
Firmware is verified in **QEMU in CI**, then on the board in P3.

Firmware splits into two layers:

```
firmware/mcu/
  core/           platform-independent C: state machine, frame codec,
                  watchdog, dedup, ring buffers.
                  No peripheral registers. No vendor headers.
  hal/qemu/       QEMU target, CTU CAN FD over PCI
  hal/ch32v307/   real board, CH32V307 CAN peripheral
  hal/host/       x86_64 build for fast logic tests
```

`core/` compiles to three targets from one source. Swapping the board means
writing a new `hal/`, not touching `core/`. That mirrors the project-wide rule
that implementations are replaceable and contracts are not.

## The QEMU limit, stated plainly

QEMU models **SJA1000** and **CTU CAN FD**, both over PCI. It does **not**
model the CH32V307 CAN peripheral. MCU-specific CAN models are added to QEMU
one part at a time (the STM32 bxCAN work is a patch series, not upstream).

So the split is:

| Proven in QEMU | Requires the board |
|---|---|
| State machine transitions | CH32V307 CAN register behaviour |
| Watchdog timing under a real timer interrupt | Bit timing (BRP/TSEG1/TSEG2/SJW) |
| Dedup across sequence wraparound | Error frames, bus-off recovery |
| Frame codec, ID partition enforcement | Electrical behaviour, EMI |
| Absence of malloc and FP instructions | Brownout, power-on reset |

This does not widen what we claim. The README already lists CAN electrical
behaviour and bus timing as not proven in simulation.

## Why RISC-V

- Royalty-free ISA, fully open toolchain — reproducibility is a project goal
- Mature upstream QEMU support for rv32
- Cheap real parts available now (CH32V307, ESP32-C6, GD32VF103)
- `core/` carries no vendor headers, so changing part means a new `hal/` only

CH32V307 over ESP32-C6 specifically: this is a safety element doing e-stop and
watchdog. WiFi and Bluetooth are attack surface on a component that must stay
independent of the network.

## Alternatives rejected

**Keep the Python model, test only on real hardware.** Pushes all firmware
risk into P3 alongside arm bring-up. Fault coverage would be unproven until
the last month.

**Write a QEMU device model for the CH32V307 CAN peripheral.** A project in
itself. Not worth it for v0.1.

**ARM + STM32.** The QEMU bxCAN support is a patch series, not upstream-
guaranteed. No advantage over RISC-V here, and it gives up the open-toolchain
argument.

## Cost

The MCU track grows from roughly one week (Python state machine) to about
2.5 weeks in P1: toolchain, startup code, QEMU harness, HAL split. P1 G-track
milestones move out by about a week.

CI gains a `mcu-qemu` job. It is cheap compared to Gazebo — no GPU, no display,
seconds per run.

## Consequences

- `firmware/virtual_mcu/` (Python) stays through P1 as the contract Motion
  builds against, and as the reference model for differential testing against
  the C core. It is **retired at the end of P1**, once `core/` passes the same
  fault suite. Two implementations of one state machine must not outlive P1.
- The `mcu_protocol` schema does not change. Same wire format, real
  implementation underneath.
- P3 payoff: the same `core/` binary passes the same fault suite in QEMU and on
  the board, with only `hal/` differing.

## Toolchain

Upstream GCC works. WCH's private instructions (`mcpy`) are mandatory only on
CH584/585, not on CH32V307, so MounRiver is not required.

| Toolchain | Triplet | Use |
|---|---|---|
| Ubuntu `gcc-riscv64-unknown-elf` | `riscv64-unknown-elf-` | CI, if its rv32 multilib works |
| [xpack `riscv-none-elf-gcc`](https://github.com/xpack-dev-tools/riscv-none-elf-gcc-xpack) | `riscv-none-elf-` | fallback and local dev; newer, reliable rv32 |
| WCH MounRiver | `riscv-none-elf-` | not needed for '307 |

Flashing uses [`wlink`](https://github.com/ch32-rs/wlink), not OpenOCD:

```
wlink mode-switch --rv     # put WCH-LinkE into RV mode once
wlink flash build/board/mcu.elf
```

`wchisp flash` over USB ISP (BOOT0 + RESET) works with no adapter at all.
OpenOCD is only needed for GDB, and requires the WCH fork built with
`--enable-wlinke`.

## Hardware

**CH32V307 has a CAN controller but no CAN transceiver.** The transceiver is
external and is not on the EVT board.

| Item | Part | Qty | Why |
|---|---|---:|---|
| Board | CH32V307V-EVT-R1 | 2 | On-board WCH-Link, no separate debugger needed |
| **CAN transceiver** | SN65HVD230 module (3.3V) | 2 | **Required. Not on the board.** |
| USB-CAN bridge | CANable 2.0 or PCAN-USB | 1 | Lets the host see real frames with `candump` |
| Logic analyser | any 8-channel | 1 | FW22 watchdog timing |
| Twisted pair + 2×120Ω | — | — | Terminate both ends of the bus |

Two boards, not one: CAN is a bus protocol. Arbitration, error frames and
bus-off recovery (FW19) cannot be exercised with a single node.

3.3V transceiver specifically: CH32V307 I/O is 3.3V. A 5V TJA1050 would need
level shifting, which is one more thing to get wrong.

Buy at the end of P2 (task H1). FW1-FW16 are all QEMU; the board is first
needed at FW17. One exception worth the ¥150: buy a single board early and run
the FW3 state machine on it once, to test the assumption that QEMU-passing code
also passes on hardware. Finding that out in P1 beats finding it out in P3.

## References

- QEMU CAN emulation: https://www.qemu.org/docs/master/system/devices/can.html
- SocketCAN vcan: https://www.kernel.org/doc/html/latest/networking/can.html
- CH32V307 SDK and datasheet: https://github.com/openwch/ch32v307
- Open-source CH32V toolchain guide: https://github.com/cjacker/opensource-toolchain-ch32v
- Zephyr board page (pinout): https://docs.zephyrproject.org/latest/boards/wch/ch32v307v_evt_r1/doc/index.html
