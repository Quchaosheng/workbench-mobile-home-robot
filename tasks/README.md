# tasks

Task definitions: one directory per task type. Each task has a goal description,
a verifier, and a set of scenarios.

```
tasks/
  pick_place/    put object A into container B (v0.1 demo task)
  kitting/       assemble a kit from a parts tray
  inspection/    check N attributes of an object (present, colour, orientation)
  assembly/      connect two parts in a defined configuration
```

Adding a task:
1. Write a verifier in `tasks/<name>/verifier.py` that takes a `WorldState` and
   returns `VerificationResult`
2. Add at least 3 frozen scenarios to `sim/scenarios/frozen/`
3. Add a task description to `interfaces/examples/`
4. Write unit tests for the verifier

The verifier is the only thing that changes per task. Everything else
(planning, execution, event store, replay) is reused.

## Scenario registration contract

A task family becomes selectable by stable `scenario_id@scenario_version`
through the Scenario Registry. The contract that registration must satisfy is
frozen before the registry is implemented:

- [ADR-0006: Multi-scenario registry contract and ownership boundaries](../docs/decisions/ADR-0006-scenario-registry-contract.md)
  states the boundary, the field ownership table, and what a manifest may never
  carry.
- `docs/architecture/scenario-contract-v1.json` is the machine-readable field
  list, diagnostic codes, bounds and forbidden field names.
- `tools/scripts/check_scenario_contract.py` validates a manifest against that
  file and exits `0 PASS`, `1 FAIL` or `2 INCOMPLETE`.

```bash
python3 tools/scripts/check_scenario_contract.py
python3 tools/scripts/check_scenario_contract.py --require-executable
```

A manifest may declare semantic actions only. Joint values, velocities,
torques, CAN frames, controller goals, trajectories and emergency-stop
authority belong to Motion, MCU and Safety, and are rejected by name before any
value is read. A manifest also never carries a second policy or verifier
implementation.

`evidence_status` distinguishes `SCRIPTED_FIXTURE`, `GAZEBO`, `PHYSICAL`,
`NOT_EXECUTED` and `BLOCKED`. The first, fourth and fifth are never release
eligible, so a scripted fixture cannot be rendered as a physical validation.

Until Issue #301 migrates the existing families, the `TASK_PROFILES` selection
path in `tools/scripts/scenario_tools.py` remains authoritative for the twelve
frozen and six expanded-per-variant regression scenarios.
