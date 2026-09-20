# Migrating from the tabletop fixture to Revision D

Issue #327 adds Revision D as a **separate** model. It does not replace, include
or modify `robot/description/workbench.urdf.xacro`, and it does not change any
existing scenario.

---

## Two models, two product configurations

| | `workbench.urdf.xacro` | `revision_d/revision_d.urdf.xacro` |
|---|---|---|
| What it is | static tabletop fixture | Revision D mobile bimanual robot |
| Contains | table, tray, module, camera post | base, four steer-drive modules, lift, torso, head, tool dock, two seven-axis arms |
| The arm | composed from a vendor package at launch | modelled, 14 named revolute axes |
| Root | `world` | `map` |
| Status | frozen, in use | new, `CONCEPT_PHYSICAL_VALIDATION_REQUIRED` |

Neither model claims the physical robot exists. Both are simulation and
integration boundaries.

---

## What did not change

- `workbench.urdf.xacro` is byte-for-byte unchanged. `#327` must not alter it,
  and `tests/unit/test_revision_d_description.py` fails if it does.
- No frozen scenario, golden set or registry entry changed.
- `robot/control/` is untouched. The Revision D `ros2_control` declaration lives
  beside the description, because `robot/control/` is a different owner's module
  and the composed UR5e interface there already owns the tabletop arm.
- No shared contract, event schema or Pydantic model changed.

---

## What a caller must do differently

1. **Root frame.** Tabletop scenarios are expressed under `world`; Revision D
   scenarios are expressed under `map`. A pose copied between them keeps its
   numbers and changes its meaning.
2. **Unit of angle.** Both use radians in the URDF. `design-spec.json` is in
   degrees and millimetres.
3. **Base axis convention.** `design-spec.json`'s `robot_base` is x right, y
   rear; the ROS `base_link` is x forward, y left. The description converts;
   anything reading the spec directly must too. See `FRAMES.md`.
4. **The arm is in the model.** The tabletop arm is composed at launch; Revision
   D arms are links and joints in this file, so a consumer must not also compose
   one on top.
5. **Launch.** Use `revision_d/launch/revision_d.launch.py`. It spawns the model
   and the controllers and exposes no unguarded trajectory path.

---

## Migration order

1. Point the caller at `robot/description/revision_d/revision_d.urdf.xacro`.
2. Re-express poses under `map` and confirm the axis convention above.
3. Run `make description-check` and `python -m pytest tests/ -q`.
4. Keep using the tabletop fixture for its existing scenarios. Migration is per
   caller, not global, and there is no plan to delete the tabletop model.

---

## What is explicitly not migrated

- **Navigation.** `#152` owns the bounded Nav2 contract. Revision D models the
  mechanical interface; it does not claim localization, mapping or obstacle
  avoidance.
- **Motion planning bridge.** `#260` owns the plan-only MoveIt boundary.
- **Camera collision.** `#261` added the tabletop `camera_body` collision; the
  Revision D cameras follow the same rule but are separate geometry.
- **Physical evidence.** Hardware and HIL remain `NOT_EXECUTED`.
- **Stability.** `stability_gates.analytical_screen_only` is `true`. The
  modelled stabilizers are a mechanism and a deploy geometry, not a validated
  stability result, and `drive_tip_angle_is_not_manipulation_release` still
  holds.
