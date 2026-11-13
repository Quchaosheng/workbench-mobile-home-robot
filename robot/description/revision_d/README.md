# Revision D robot description (Owner: Motion)

The mobile bimanual robot, as a separate model from the frozen tabletop fixture.
This is a simulation and integration boundary for the Revision D product
configuration described in `hardware/mechanical/design-spec.json`. It is **not**
a claim that the physical robot has been built.

## What is here

| File | Purpose |
|---|---|
| `revision_d.urdf.xacro` | the model: base, four steer-drive modules, lift, torso, head, tool dock, two seven-axis arms |
| `revision_d_ros2_control.xacro` | the `ros2_control` interfaces, opt-in behind `sim_gz:=true` |
| `revision_d_controllers.yaml` | the named controllers; no whole-body joint group |
| `launch/revision_d.launch.py` | spawn in Gazebo and start the controllers |
| `tools/validate_description.py` | 300+ assertions against `design-spec.json` |
| `tools/generate_frames.py` | regenerates `generated/revision-d-frames.json` |
| `tools/gazebo_acceptance.py` | the Gazebo cases, reporting `NOT_EXECUTED` without a simulator |
| `FRAMES.md` | every dimension and the spec pointer it comes from |
| `MIGRATION.md` | the boundary against the tabletop fixture |

## Quick start

```bash
# Validate the model against the mechanical spec (no ROS needed)
python3 robot/description/revision_d/tools/validate_description.py

# Regenerate the frame artifact and check it is current
python3 robot/description/revision_d/tools/generate_frames.py --check

# The gates
make description-check

# Expand it by hand
xacro robot/description/revision_d/revision_d.urdf.xacro > /tmp/revision_d.urdf
```

## Gazebo sensors

The model declares an IMU and a bumper contact sensor unconditionally, and the
three camera streams only when the expansion is asked for them:

```bash
# no camera sensors; the IMU and bumper are still declared
xacro revision_d.urdf.xacro > /tmp/rd.urdf

# camera, depth camera and the two wrist streams included
xacro revision_d.urdf.xacro sensors:=true > /tmp/rd-sensors.urdf
```

`launch/revision_d.launch.py` passes `sensors:=true`, so a Gazebo spawn has the
streams a consumer subscribes to, while a bare `xacro` expansion in CI does not
gain topics just by existing.

The plugins are opt-in because the opposite default changes what an expansion
declares: a check that runs `xacro` on this file should test the model, not a
rendering backend.

**What a `<sensor>` element proves:** that the model is *able* to produce data.
It does not prove a frame was published in a run, that an image was inspected, or
that a camera is calibrated. Calibration is a separate path (`#554`), and every
evidence table keeps the Gazebo and physical columns `NOT_EXECUTED` until a real
run and real bench evidence exist.

The sensor values are fixture choices, not datasheet numbers: the horizontal field
of view is not pinned by `design-spec.json`, and the IMU covariances are nominal.
The file says so at each value, and a test asserts the wording stays.

## What this model does not claim

- **No physical evidence.** Hardware and HIL status are `NOT_EXECUTED`.
  `design-spec.json` says `CONCEPT_PHYSICAL_VALIDATION_REQUIRED`.
- **No stability result.** `stability_gates.analytical_screen_only` is `true`.
  The stabilizers are a modelled mechanism with a derived deploy geometry, and
  `drive_tip_angle_is_not_manipulation_release` still holds. The required
  physical tests (`pull_test`, `5_deg_slope`, `emergency_stop`, `brake_hold`,
  `stabilizer_deploy`) have not been run.
- **No navigation capability.** `#152` owns the bounded Nav2 contract. The
  chassis has a holonomic mechanical interface; localization, mapping and
  obstacle avoidance are not asserted here.
- **No safety function.** Collision geometry is a planning obstacle. It is not
  continuous collision safety, and it authorizes no execution. STOP and
  safe-stop authority stay with Motion and the safety controller.
- **No validated arm torque.** The `effort` values are the spec's peak torques,
  which is a saturation the controller must respect, not a validated profile.

## Known modelling boundary

`self_collide` is `false`. The base enclosure is a box and the wheel bogies sit
inside its footprint; a box cannot express a wheel well, so those volumes
intersect. Enabling self-collision would turn that artefact into simulated
internal contact. The forbidden-volume and contact tests own the exclusion set.
See `FRAMES.md` — this is a documented limitation, **not** a safety claim.

## Integration boundaries

| Concern | Owner |
|---|---|
| tabletop fixture, tray, module | remains `robot/description/workbench.urdf.xacro`, unchanged |
| arm composition and MoveIt bridge | `#260`, `robot/control/` |
| mobile navigation contract | `#152` |
| camera collision rule | `#261` (tabletop); applied here to the Revision D cameras |
| device runtime | `#230` |

`robot/control/` and `firmware/` are outside this model's write scope.
