# Public Candidate Selection Package

Status: candidate baseline researched from public vendor and upstream sources;
not an approved AVL, safety certification, or physical bring-up result.

## Candidate matrix

| Area | Public candidate | Integration decision | Evidence and remaining closure |
|---|---|---|---|
| Linux board and carrier | NVIDIA Jetson Orin Nano Super Developer Kit 8GB with its official carrier | Use the vendor carrier for prototype bring-up; defer a custom carrier | [Jetson Linux](https://developer.nvidia.com/embedded/jetson-linux-r3640). Confirm purchased board revision, power input, pinmux and IRQ map from the board manual before DTS freeze. |
| Prototype Linux power | Vendor-recommended regulated DC input for the official carrier | Keep Linux power separately fused from motion power | Board manual, measured sustained load and thermal evidence are still required; no power rating is promoted to production. |
| CAN host interface | PEAK PCAN-USB FD, isolated USB CAN-FD adapter | Use SocketCAN through the vendor Linux driver; keep `can0` as the logical bus | Confirm exact SKU, driver version, isolation rating, bitrate, termination and harness before purchase. |
| Base motion MCU | ST STM32G474RET6 with FDCAN | Use as the `MCU-BASE` candidate for traction, encoders and lift; replace the legacy CH32V307 PCB U5 before CAN-FD bring-up | Confirm exact package, pin/timer/ADC budget, debugger path, firmware target and PCB ECO. |
| JetPack/L4T | JetPack 6.2.1 / Jetson Linux 36.4.4 | Pin this as the prototype software candidate | NVIDIA release page confirms the mapping; download hash, module revision and kernel config hash remain required. |
| Kernel | NVIDIA L4T 36.4.4 vendor kernel baseline | Merge `bsp/linux/robot_bsp.config` only after the exact source package is pinned | Kernel source archive, toolchain digest, resulting `.config`, Image/modules and DTB hashes remain required. |
| Host rootfs | NVIDIA Jetson Linux rootfs for L4T 36.4.4 | Boot motion-inhibited with the checked-in systemd service set | Package lock, firmware bundle, recovery image and rollback transcript remain required. |
| ROS 2 | ROS 2 Humble on the JetPack Ubuntu 22.04 host; retain Jazzy for Ubuntu 24.04 simulation/container jobs | Avoid claiming native Jazzy binaries on the Jetson host until a supported image is proven | Freeze the ROS distribution per deployment image and run a compatibility test against `xarm_ros2`. |
| Seven-axis arm | UFACTORY xArm 7 with the vendor controller and `xarm_ros2` | Candidate for both arm domains; use the vendor controller as the module boundary | Exact xArm 7 SKU, firmware, power, safety I/O, network mode and supplier quotation remain open. Upstream ROS 2 package is BSD-3-Clause. |
| End effector | Robotiq 2F-85 description and vendor controller | Keep tool control behind `TOOL-L-CTRL`/`TOOL-R-CTRL` | Confirm gripper SKU, mounting, power, control protocol and safe-stop behavior with the supplier. |
| PCB U2 | Mean Well RSD-300-12 as a 300 W-class candidate | Do not replace the schematic placeholder until the exact input range, isolation, footprint, creepage, thermal and EMI reviews pass | [Public datasheet](https://www.meanwell.com/Upload/PDF/RSD-300/RSD-300-SPEC.PDF). The system bus voltage must be matched to the exact RSD-300 variant; input range, derating, mounting, protection and AVL approval remain open. |
| Safety hardware | Existing independent STM32G0B1 `MCU-SAFETY` boundary plus dual-channel external inhibit chain | Preserve hardware authority outside Linux and ROS | Safety-owner review, hazard analysis, certified components, wiring and measured trip-time evidence remain required. |

The existing CH32V307 PCB/firmware target is retained only as a legacy EVT
artifact. Its CAN peripheral is classic CAN 2.0B, so it is not an acceptable
`MCU-BASE` for the CAN-FD system baseline.

## Third-party status

- NVIDIA JetPack/L4T is a vendor distribution and must be reviewed under its
  applicable redistribution terms before an image is published.
- `xArm-Developer/xarm_ros2` is BSD-3-Clause at the public `humble` branch:
  <https://github.com/xArm-Developer/xarm_ros2>.
- RLSOK is Apache-2.0 at the public repository:
  <https://github.com/realitywarden/rlsok>.
- RLSOK's documented official integration is UR5e on Ubuntu 24.04/Jazzy;
  xArm 7 support is a generic protocol integration and is not vendor-certified.
- MonoSim has no verified public repository or license in the current evidence
  set; it remains an invited external integration until its maintainers provide
  a link and written terms.

## Closure rule

These candidates make the next engineering actions concrete, but they do not
close the existing release gates. Do not change `REVIEW_REQUIRED`, `BLOCKED`,
or `NOT_EXECUTED` statuses to `PASS` until the named evidence is attached.

The step-by-step evidence gate is maintained in
[`validation/bringup-closure-checklist.md`](validation/bringup-closure-checklist.md).
