# ADR-0011: Safety MCU targets STM32G474RET6 / STM32G0B1, not RISC-V

Status: **accepted**
Date: 2026-11-13
Supersedes: [ADR-0003](ADR-0003-mcu-riscv-qemu.md)

## Context

ADR-0003 selected **RISC-V rv32imac** and named the **CH32V307** as the safety MCU
and the reference part. Two things have since changed, and neither is a matter of
taste.

**The committed selection disagrees with the record.** `bsp/board-manifest.yaml`
names `MCU-BASE: STM32G474RET6` (FDCAN) and `MCU-SAFETY: STM32G0B1`, and lists
`controller_pcb_mcu_eco_from_legacy_ch32v307` as an unresolved item. The change came
from a PCB engineering change order away from the legacy CH32V307. An accepted ADR
that names a part the board does not carry is not a decision record any more; it is
a stale claim sitting on the critical path of the firmware effort.

**QEMU cannot verify the selected part's CAN peripheral.** ADR-0003's verification
story rests on QEMU modelling the CAN controller. QEMU models the SJA1000 and the
CTU CAN FD controller. It models **neither** the CH32V307 CAN peripheral **nor**
the STM32 FDCAN peripheral. So `make qemu` proves the shared `core/` on an
architecture the selected part does not use, and the firmware's verification story
currently covers no part that is going to be built.

That second point is the one that decides this ADR. ADR-0003 chose RISC-V partly
because QEMU made it cheap to verify; if QEMU cannot model the peripheral either
way, the verification argument no longer favours RISC-V over the part the board
actually carries.

## Decision

The safety MCU target is **STM32**, in two parts:

| Role | Part | CAN | Notes |
|---|---|---|---|
| `MCU-BASE` | **STM32G474RET6** | FDCAN | the base controller; drives the four steer-drive modules and the arm bus |
| `MCU-SAFETY` | **STM32G0B1** | FDCAN | the safety element; **independent reset domain** from `MCU-BASE` |

Both parts are FDCAN. The safety part's reset domain is independent, so a fault
that latches `MCU-BASE` does not clear the element that is supposed to stop the
robot, and a reset of the safety part is not a side effect of resetting the base
controller.

Firmware keeps the two-layer split ADR-0003 established, which is the part of that
record that survives:

```
firmware/mcu/
  core/           platform-independent C: state machine, frame codec,
                  watchdog, dedup, ring buffers.
                  No peripheral registers. No vendor headers.
  hal/            one directory per part. <-- stm32g4/ and stm32g0/ are the
                  targets; hal/ch32v307/ is no longer the board target.
```

`firmware/mcu/hal/ch32v307/` is an empty placeholder. It is not the board target
and this ADR does not populate it.

## What QEMU can still prove, and what it cannot

This is stated plainly because the whole reason ADR-0003 picked RISC-V was a
verification-cost argument, and that argument has to be re-made rather than
inherited.

**QEMU still proves, on `core/`, on the architecture the QEMU target builds for:**

- the state machine's transitions and its refusals;
- watchdog timing;
- command de-duplication;
- the frame codec's encode/decode round trip;
- the absence of `malloc` and of floating point in `core/`.

**QEMU cannot prove, and needs Renode or the bench:**

- FDCAN bit timing and the resulting bus behaviour;
- bus-off entry and recovery;
- error frames and the error-counter behaviour that drives them;
- any electrical behaviour: transceiver timing, termination, ground offsets,
  supply droop;
- the independent reset domain of `MCU-SAFETY`, which is a property of the board,
  not of a model.

`make qemu` therefore still runs and still proves the shared `core/` logic, but its
result must not be quoted as evidence about the selected part's CAN behaviour. The
replacement verification path is **Renode** (which has STM32 FDCAN models) or a
hardware-in-the-loop bench, and choosing between them is its own issue.

## Consequences

- `bsp/board-manifest.yaml` keeps
  `controller_pcb_mcu_eco_from_legacy_ch32v307` **unresolved**. This ADR records
  which parts the board carries; it does not close the eco, because the HAL that
  would make the eco resolvable does not exist yet.
- `firmware/mcu/README.md` stops describing `hal/ch32v307/` as the board target and
  names the STM32 HAL directories a follow-up issue will populate.
- The QEMU target remains in CI as a `core/`-logic check with a narrower claim than
  ADR-0003 made for it.
- No C code changes in this issue. The HAL is a follow-up.
- Every evidence table keeps the hardware and HIL columns `NOT_EXECUTED`. Selecting
  a part is not building it, and this record authorises no physical claim.

## What this ADR does not decide

- **Which of Renode or a bench replaces QEMU's CAN coverage.** That is a separate
  decision with its own cost argument.
- **Whether the two parts share a CAN transceiver or have one each.** That is a
  schematic question.
- **The HAL's shape.** The `core/hal.h` boundary is unchanged by this record.
