# Architecture documents

The written boundaries this repository is built on. Each one exists because a
previous change drifted across a line that was never written down.

## Scenario registry

| Document | What it freezes |
| --- | --- |
| `docs/decisions/ADR-0006-scenario-registry-contract.md` | The boundary, the field ownership table, and what a manifest may never carry. |
| `docs/architecture/scenario-contract-v1.json` | The machine-readable field list, diagnostic codes, bounds and forbidden field names. |
| `docs/architecture/scenario-capability-matrix-v1.json` | What each scenario can do, which team owns each half, and how strong its evidence is. |
| `docs/architecture/scenario-capability-matrix.md` | The human-readable table, generated from the matrix above. |

The contract and the matrix are enforced by a gate, not by review alone. A
scenario that is registered but absent from the matrix fails
`tools/scripts/check_scenario_capabilities.py`, and so does a matrix row that
claims more evidence than its manifest carries.

```bash
python3 tools/scripts/check_scenario_contract.py
python3 tools/scripts/check_scenario_capabilities.py --check-generated
```

## Runtime and hardware boundaries

- `architecture/system.md` -- the service map and the trusted runtime boundary.
- `architecture/host-can-transport-v1.md`, `architecture/mcu-can-hal-boundary-v1.md` -- the
  host/MCU split and who owns a frame.
- `architecture/ros2-device-runtime-bridge-v1.md` -- the adapter boundary below orchestration.
- `architecture/observability-contract-v1.md` -- what a health snapshot may claim.
- `architecture/linux-drivers.md` -- the driver framework and its tests.
- `architecture/robot-bsp-*.md` -- the board support package manifests and selection rules.
- `architecture/semantic-action-correlation-ledger.md` -- how an action result is tied to evidence.

## How these documents relate

A scenario declares semantic actions and adapters. Motion, perception, the MCU
and navigation implement them below the orchestration boundary and return the
shared `ActionResult`. The World Model decides verification. Release decides
eligibility. No document in this directory moves that stop authority upward, and
no scenario manifest or capability row may carry joint values, trajectories,
controller goals or emergency-stop authority.
