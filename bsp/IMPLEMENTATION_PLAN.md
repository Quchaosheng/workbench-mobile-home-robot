# BSP Implementation Plan

This is the execution register for the cost-conscious robot BSP baseline.
Completion means repository artifacts and repeatable commands exist; physical
bring-up remains a separate evidence gate.

| Stage | Deliverable | Status | Exit evidence |
|---|---|---|---|
| BSP-0 | six-domain topology, ownership and CAN contract | COMPLETE | architecture docs and manifest |
| BSP-1 | Jetson board, carrier, power and thermal closure | BLOCKED | vendor schematic, load test, approved BOM |
| BSP-2 | STM32G474RET6 base and STM32G0B1 safety closure | BLOCKED | part/revision, FDCAN/pin budget, programming and safety review; legacy CH32V307 PCB ECO |
| BSP-3 | kernel config, device tree and interface enumeration | READY_TO_START | BSP-1/BSP-2 inputs |
| BSP-4 | bootloader, rootfs, firmware bundle and recovery image | READY_TO_START | reproducible build and recovery transcript |
| BSP-5 | CAN discovery, six heartbeats, STOP and reset validation | NOT_EXECUTED | guarded hardware capture |
| BSP-6 | power, thermal, latency, bus-load and long-run validation | NOT_EXECUTED | calibrated raw measurements |

## Working rule

Do not close a blocked stage by changing its status manually. Attach the named
evidence first, then update this register and the release evidence register in
the same change.
