# BSP Bring-up Closure Checklist

This checklist is the release gate for moving the BSP from candidate selection
to a physically reproducible prototype image. A checked item must link to raw
evidence; a simulation result or CI pass is not physical evidence.

`bsp/readiness.yaml` may only mark a gate `PASS` through a committed YAML record
that `bsp/validation/check_readiness.py` parses. The record carries the fields
`bringup-plan.yaml` declares, binds them to the gate, board model and source
revision, and names a repository-relative raw capture whose sha256 recomputes:

```yaml
gate: BSP-GATE-CAN
board_model: NVIDIA_JETSON_ORIN_NANO_SUPER_DEVELOPER_KIT_8GB
board_revision: P3449-A02
kernel_version: 5.15.148-tegra
firmware_versions: {MCU-BASE: 1.2.0, MCU-SAFETY: 1.0.3}
timestamp_utc: 2026-10-20T04:05:06Z
result: PASS
source_revision: <40-character commit>
raw_capture: bsp/validation/captures/can-six-domain.log
raw_capture_hash: <sha256 of that file>
```

Blank, unresolved (`TBD_*`, `NOT_BUILT`) or placeholder values, a record bound to
a different gate, and a digest that no longer recomputes all keep the gate
non-PASS, so a generic documentation file can never close a physical gate.

## 1. Hardware identity

- [ ] Carrier-board MPN, revision, schematic and pinmux are recorded.
- [ ] Jetson module SKU and board revision are recorded.
- [ ] CAN transceiver, connector, harness, termination and measured bitrate are recorded.
- [ ] Camera serial, USB topology, stream modes and calibration artifact are recorded.
- [ ] Arm and tool controller MPN, firmware, protocol and safety I/O are recorded.
- [ ] U2 exact MPN, input range, isolation rating, derating curve, footprint and thermal review are approved.

## 2. Reproducible software

- [ ] JetPack/L4T release, vendor package URLs and SHA256 values are locked.
- [ ] Kernel source, compiler, merged `.config`, Image/modules and DTB hashes are locked.
- [ ] Rootfs package lock, firmware bundle, recovery image and rollback procedure are attached.
- [ ] ROS 2 distribution and third-party package versions are pinned per deployment image.

## 3. Safety and performance evidence

- [ ] Power-on, brownout, thermal and sustained-load logs pass the acceptance limits.
- [ ] CAN loss, watchdog, emergency-stop and brake tests pass with measured trip times.
- [ ] Tip-stability tests cover every declared pose, payload and floor condition.
- [ ] Fault injection and recovery transcripts are archived with board and firmware identity.

## 4. Release decision

- [ ] Safety owner signs the hazard-analysis and external-inhibit review.
- [ ] Legal owner signs third-party license, NOTICE and redistribution review.
- [ ] Release owner verifies every manifest hash and evidence link.

Until all applicable boxes are checked, keep `bsp/readiness.yaml` at
`REPOSITORY_BASELINE_READY_PHYSICAL_BRINGUP_BLOCKED` and keep image inputs at
`inputs_unresolved`.
