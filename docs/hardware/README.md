# docs/hardware

Hardware bring-up notes and integration guides.

- [Hardware wiring](wiring.md): controlled connector map and safe connection order.
- [Physical bring-up](bringup.md): HIL bench, staged power-up, evidence, debugging, and defects.
- [HW1 UR5e extraction](hw1-ur5e-extraction.md): source and configuration notes.
- [Hardware selection closure](hardware-selection-closure.md): purchase blockers, missing selections, owners, and acceptance evidence.

General rule: all bring-up steps are scripted where possible. If a step
requires manual action (physical wiring, screwdriver), it is documented
with a photo reference and a verifiable outcome (e.g. "voltage reading X").

## Preflight

The software-only preflight never sends a CAN, motor, or emergency-stop command:

```bash
python tools/scripts/hardware_preflight.py --output runs/hardware/preflight.json
```

It reports `not_ready` until Linux, the configured CAN interface, camera device,
and an operator-created emergency-stop marker are all present. A `not_ready`
result is an explicit block, not a request to continue with simulated hardware.
