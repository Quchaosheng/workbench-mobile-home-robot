# BSP V0.1 Review Checklist

Use this checklist before approving the prototype BSP baseline.

## Architecture

- [ ] One Linux board and six controller domains match the intended robot.
- [ ] Left/right arms and tools remain independently resettable and observable.
- [ ] `MCU-SAFETY` remains independent of Linux and ordinary motion control.
- [ ] Lift ownership in `MCU-BASE` is acceptable for the first prototype.

## Cost and supply

- [ ] Jetson Orin Nano Super satisfies measured workload before any AGX upgrade.
- [ ] NVMe, cooling and isolated CAN adapter are included in the prototype cost.
- [ ] STM32G474RET6 and STM32G0B1 lifecycle and lead time are acceptable.
- [ ] The legacy CH32V307 PCB U5 is removed or explicitly isolated from the CAN-FD base-motion path.
- [ ] Arm/tool controllers are included in supplier quotations and not double-counted.
- [ ] No custom carrier-board spin is approved without measured prototype need.

## Interfaces and safety

- [ ] Carrier power, camera bandwidth, CAN adapter and thermal limits have sources.
- [ ] Six node IDs, reset domains, heartbeats and supplier protocols are confirmed.
- [ ] E-stop and safe enable remain hardware-controlled and fail closed.
- [ ] Linux boot, service restart and update cannot clear the safety latch.

## Evidence

- [ ] Kernel/JetPack, DTB, rootfs and firmware hashes are recorded.
- [ ] Physical tests remain `NOT_EXECUTED` until raw evidence is attached.
- [ ] `python bsp/validation/validate_manifests.py` passes.
- [ ] Hardware release remains governed by `hardware/release/`.
