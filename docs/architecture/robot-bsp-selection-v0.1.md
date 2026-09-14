# Robot BSP V0.1 Cost-Conscious Selection

Status: recommended engineering baseline for prototype build; supplier and
electrical approval are still required before purchase or safety release.

## Selected baseline

| Domain | Selection | Why this is the cost boundary |
|---|---|---|
| Linux main board | NVIDIA Jetson Orin Nano Super Developer Kit, 8 GB | GPU inference and ROS 2 headroom at substantially lower cost and power than AGX Orin; one board is enough for the high-level stack |
| Linux storage | one  NVMe SSD, 512 GB minimum | avoids removable-SD wear for logs and models; capacity can be increased without changing the BSP |
| Linux cooling | vendor active cooler plus chassis airflow | required for sustained vision workloads; no passive-only assumption |
| Linux CAN during prototype | one isolated USB-CAN-FD adapter | avoids an immediate custom carrier-board CAN spin; replaceable during bring-up |
| `MCU-BASE` | STM32G474RET6, FDCAN-capable motor-control MCU | one MCU covers traction, encoders, lift and chassis telemetry with the required FDCAN and timer/ADC peripherals |
| `MCU-SAFETY` | STM32G0B1, independent CAN-FD and safety GPIO domain | low-cost, simple safety controller with separate reset and watchdog domain |
| `ARM-L-CTRL` / `ARM-R-CTRL` | vendor arm controller supplied with each arm | do not duplicate a proprietary servo controller in the robot BSP |
| `TOOL-L-CTRL` / `TOOL-R-CTRL` | integrated vendor tool controller where available; otherwise small vendor CAN/RS-485 node | select per end-effector; keep the interface domain independent |

The selection is **one Linux board, two robot-owned MCUs, and four vendor/module
controller domains**. The four module domains are counted for addressing,
reset, health and safety analysis even when a vendor controller is physically
integrated into an arm or tool.

## Cost controls

- Do not buy an AGX Orin for the first prototype unless measured model latency
  exceeds the Orin Nano Super envelope.
- Do not design a custom Linux carrier board before USB-CAN, camera bandwidth,
  power and thermal measurements identify a real limitation.
- Keep the safety MCU electrically independent; cost reduction must not remove
  the hardwired E-stop or safe-enable path.
- Use one CAN-FD backbone for the prototype. Add a second bus only when a
  measured bandwidth, fault-containment or cable-length requirement justifies it.
- Use vendor arm/tool controllers rather than adding fourteen custom joint MCU
  boards to the project.

## Required acceptance before purchase

1. Confirm the Jetson carrier power input, sustained thermal envelope, camera
   count/bandwidth, and Linux BSP/JetPack release.
2. Confirm the STM32G474RET6 FDCAN, timer, ADC, encoder, lift and service-
   programming pin budget; reserve safe output pins for hardware inhibit and
   replace the legacy CH32V307 PCB U5 before CAN-FD bring-up.
3. Confirm the STM32G0B1 independent watchdog, dual-channel E-stop inputs,
   safe-enable outputs and reset behavior with the Safety Owner.
4. Obtain arm and tool controller protocols, power limits, CAN IDs and reset
   behavior from suppliers before assigning their final node implementations.
5. Measure prototype CPU/GPU utilization, CAN bus load, thermals and power;
   upgrade the Linux board only from recorded evidence.

Until those checks are attached to the release evidence register, this remains
`RECOMMENDED_PROTOTYPE_SELECTION`, not a production AVL or safety approval.
