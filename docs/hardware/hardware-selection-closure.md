# Hardware selection closure

Status: `ORDER_RELEASE_BLOCKED`

This is the purchase gate for one prototype robot. It consolidates the
existing BOM, connector map, BSP selection and motor-driver constraints. A
candidate class is not an approved part: every row below needs an exact MPN,
datasheet, supplier evidence and the listed acceptance test before ordering.

## Hard blockers

| Area | Current constraint | Required closure |
|---|---|---|
| Traction power | Controller `J2` is limited to 12 V / 120 W / 10 A aggregate; the documented dual-motor candidate reaches about 11 A at stall | Freeze both motor MPNs and driver current limits; either prove the complete 12 V branch stays within 10 A including inrush/regeneration, or redesign the traction bus as a protected 48 V branch |
| MCU/CAN mode | PCB U5 is `CH32V307VCT6` (classic CAN 2.0B), while the system contract requires CAN-FD | Use `STM32G474RET6` for `MCU-BASE` and release a PCB ECO, or explicitly downgrade every CAN node and the host contract to classic CAN; do not claim CAN-FD with CH32V307 |
| Battery and protection | 48 V pack is only a planning allocation; pack, BMS, charger, contactors, precharge, fuse and PDU are unspecified | Freeze pack nominal/max voltage, continuous/peak current, BMS cutoffs, contactor coil voltage, precharge resistor/power, fuse interrupt rating and UN 38.3 evidence |
| Main isolated converter | U2 is still a 36-60 V to 12 V / 240 W class placeholder with no approved MPN or land pattern | Freeze an exact converter with 60 V endpoint coverage, isolation/creepage, thermal interface and vendor application evidence; regenerate the PCB footprint and routing after selection |
| CAN isolation power | U7 candidate data does not yet close the required isolation working voltage/creepage for the PCB barrier | Freeze the isolated 5 V module and insulation system, verify loaded regulation and hipot/creepage evidence, then rerun layout and safety review |
| Safety | `J11` is single-channel diagnostic only and is not compatible with childboard `J_SAFE` | Freeze dual-channel E-stop, K1/K2 force-guided relays, reset/discrepancy behavior and independent inhibit measurements; never wire `J11` to `J_SAFE` |
| CAN | Bitrate, transceiver/isolated supply approval, termination and shield/drain policy remain TBD | Freeze CAN-FD bitrate, two 120 ohm bus-end terminations, isolated power/creepage and harness pinout; pass loopback, fault and eye/EMC checks |
| Compute and camera | Jetson carrier power input, JetPack/kernel, D435 USB3 mode, mount and calibration are unresolved | Freeze exact Jetson carrier/dev-kit revision, active cooling, JetPack image, D435 SKU/serial, USB3 bandwidth mode, mount and physical calibration |

## Purchase checklist

| Item | Baseline candidate | Missing before PO | Acceptance evidence | Owner |
|---|---|---|---|---|
| Linux compute | Jetson Orin Nano Super Developer Kit 8GB + 512GB NVMe | carrier, SKU lifecycle, power and cooling | sustained workload thermal/power capture | BSP |
| RGB-D camera | Intel RealSense D435 | exact SKU, USB3 cable/mount, serial, ROS2/librealsense compatibility | stream bandwidth and robot-frame calibration | Perception |
| Main MCU | STM32G474RET6 | package, FDCAN/timer/ADC budget, programming fixture and PCB ECO | interface loopback and reset capture | Electrical/Firmware |
| Safety MCU | STM32G0B1 | watchdog, dual-channel I/O, reset and safety review | E-stop truth table and independent inhibit capture | Safety |
| Input protection | LTC4368 family + back-to-back 150 V MOSFETs | approved MPNs, SOA, fuse and thermal design | UV/OV, reverse, inrush and fault trip tests | Electrical |
| 48 V to 12 V | isolated 240 W converter class | exact MPN, 60 V endpoint coverage, pinout, creepage, thermal model | loaded voltage, isolation and 30 min thermal test | Electrical |
| Jetson branch | TPS26633RGER candidate, 12 V / 5 A controlled branch | prove Jetson peak load fits the 5 A branch or replace U3 with an approved >=6 A part; confirm thermal fit | load-trip and brownout test | Electrical |
| Logic rail | 12 V to 3.3 V, 5 A class | exact MPN, inductor/layout and transient limits | ripple, efficiency and thermal test | Electrical |
| Isolated CAN | ISO1042 class + isolated 5 V module | exact MPNs, creepage/clearance and termination | CAN-FD loopback, bus fault and isolation test | Electrical/Safety |
| Traction motors | M1/M2 not selected | voltage, continuous/stall current, speed/torque, gearbox and encoder interface | loaded current, thermal and regeneration test | Motion |
| Motor drivers | childboard driver class not selected | driver MPN, MOSFET/SOA, current sense, clamp/brake path, nFAULT behavior | four-channel current limit and fail-closed safety test | Motion/Electrical |
| Encoders | left/right not selected | incremental/absolute type, voltage level, CPR, connector and shield | direction, index/timestamp and noise test | Motion |
| Arms/tools | supplier controllers | exact controller/tool MPN, protocol, power, reset and safety interface | supplier protocol and reset evidence | Motion |
| Harness/PCB/mechanics | controlled classes only | released drawings, AVL, quotes, DFM and lot traceability | continuity/pull, AOI/ICT, fit and impact tests | Manufacturing |

## Release rule

The existing `hardware/procurement/bom.csv` remains the commercial source of
truth and must stay `ORDER_BLOCKED` until every critical row has an approved
MPN, dated supplier evidence, certificates where applicable, and owner signoff.
Passing repository validators proves table consistency only; it does not prove
physical safety, thermal performance or production readiness.
