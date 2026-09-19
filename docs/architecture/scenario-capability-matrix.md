# Scenario capability matrix

One authoritative view of what each scenario can do, which boundary owns each
half, and how strong its current evidence is. Issue #311 requires this to be
machine-readable where practical, so the table below is **generated** from
`docs/architecture/scenario-capability-matrix-v1.json`.

Regenerate and verify with:

```bash
python3 tools/scripts/check_scenario_capabilities.py --check-generated
```

## How to read a row

- **Status** is the registration state: `REGISTERED`, `PLANNED`, `BLOCKED` or `NOT_REGISTERED`.
- **Environment** is how the scenario was actually exercised. It may restate or
  weaken the manifest `evidence_status`; a row that claims a stronger class than
  its manifest is refused with `MATRIX_STATUS_EXCEEDS_EVIDENCE`.
- **Missing capabilities** is `not_available` or `blocked`. A passing fixture never
  implies support.
- **Release eligible** is `yes` only for a registered row whose environment is
  `GAZEBO` or `PHYSICAL`. Scripted fixtures are never release eligible.

Ownership columns are the ADR-0006 ownership table projected per scenario:
scenario rules, adapter implementation, evidence and the release decision.

## Boundaries this matrix may not cross

A row may name semantic actions and adapter capabilities only. It must not carry
joint trajectories, controller goals, torque or velocity limits, CAN frames or
emergency-stop authority. Motion and the MCU keep controller and stop authority;
a scenario can request a semantic action, never implement one.

## Matrix

<!-- Generated from docs/architecture/scenario-capability-matrix-v1.json. Do not edit by hand. -->

| Scenario | Status | Environment | Semantic actions | Adapters | Missing capabilities | Evidence status | Release eligible | Rules owner | Adapter owner | Evidence owner | Release owner |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `assemble-two-parts@0.3` | BLOCKED | BLOCKED | observe, grasp, place | motion, perception | blocked | BLOCKED | no | Task / Simulation | Motion | World Model | Release / QA |
| `clean-and-inspect-workspace@0.1` | NOT_REGISTERED | NOT_EXECUTED | observe, navigate, clean_workspace | motion, navigation, perception, simulation | not_available, blocked | NOT_EXECUTED | no | Scenario / Simulation | Motion / Navigation | World Model | Release / QA |
| `clear-workspace@0.2` | PLANNED | SCRIPTED_FIXTURE | observe, navigate, grasp, place | motion, navigation, perception, simulation | not_available | SCRIPTED_FIXTURE | no | Task / Simulation | Motion / Navigation | World Model | Release / QA |
| `inspect-workpieces@0.2` | PLANNED | SCRIPTED_FIXTURE | observe | perception, simulation | not_available | SCRIPTED_FIXTURE | no | Task / Simulation | Perception | World Model | Release / QA |
| `kit-three-parts@0.2` | PLANNED | SCRIPTED_FIXTURE | observe, grasp, place | motion, perception, simulation | not_available | SCRIPTED_FIXTURE | no | Task / Simulation | Motion / Perception | World Model | Release / QA |
| `pick-place-red-block@1.0` | REGISTERED | SCRIPTED_FIXTURE | observe, grasp, place | motion, perception, simulation | - | SCRIPTED_FIXTURE | no | Task / Simulation | Motion / Perception | World Model | Release / QA |
| `sort-parcels@0.2` | PLANNED | SCRIPTED_FIXTURE | observe, grasp, place | motion, perception, simulation | not_available | SCRIPTED_FIXTURE | no | Task / Simulation | Motion / Perception | World Model | Release / QA |
