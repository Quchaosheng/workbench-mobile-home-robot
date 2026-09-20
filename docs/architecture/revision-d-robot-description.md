# Revision D robot description

The Revision D mobile bimanual manipulator has its own robot description under
`robot/description/revision_d/`. It is separate from the frozen tabletop fixture
in `robot/description/workbench.urdf.xacro`, which keeps serving its existing
scenarios unchanged.

This page states what the model claims, where each number comes from, and what it
deliberately does not claim. The authoritative per-dimension breakdown lives in
`robot/description/revision_d/FRAMES.md`, and the migration boundary in
`robot/description/revision_d/MIGRATION.md`.

## What it is

A Gazebo/ROS description for the product configuration described in
`hardware/mechanical/design-spec.json`: a 540 x 520 mm holonomic base with four
independent steer-drive modules, a braked 350 mm lift, torso, head/neck, tool
dock, sensors and two seven-axis arms.

It is a simulation and integration boundary. It is not evidence that the robot
has been built.

## Structure

| Group | Links and joints |
|---|---|
| Base | `base_link`, bumper, base camera |
| Drivetrain | four steer-drive modules, each a continuous steering revolute plus a wheel drive revolute |
| Stabilizers | four arms with prismatic deploy joints |
| Lift | `lift_column`, `lift_joint` (prismatic), `lift_carriage` |
| Torso | `torso`, `parcel_bay`, `tool_dock` with three slots |
| Head | `neck_pedestal`, `neck_pan_joint`, `neck_tilt_joint`, `head_display`, `head_window` |
| Arms | `arm_left` and `arm_right`, seven revolute axes each, ending at a `tool0` flange |

The 14 revolute manipulator axes are named `arm_{left,right}_joint_1..7` and take
their limits and peak torques from the mechanical spec. Wheel steering, wheel
drive, neck and lift joints are modelled separately by joint type, so they can
never be counted as manipulator axes.

## Frames

The tree has exactly one root, `map`, and reaches `odom`, `base_footprint`,
`base_link`, the lift, torso, head, both arm chains, the tool flanges and the
sensor optical frames. Camera optical frames follow the REP-103 convention (z
forward, x right, y down); publishing detections from a body frame instead gives
a silent 90-degree error that still renders plausibly.

One trap is worth stating explicitly, because it is a sign error rather than a
typo: the mechanical spec's `robot_base` uses **x = robot right, y = robot rear**,
while a ROS base frame uses **x forward, y left**. Both are legitimate
conventions. The description derives each stabilizer's deploy direction from the
corner signs and reach components rather than writing four hand-picked yaw
angles, which is where that error otherwise lives.

## Collision geometry

Every solid body carries both a visual and a collision primitive of the same
size, and exactly one collision volume. Convention frames carry none, because a
zero-size frame treated as a solid is a phantom planning obstacle.

Collision geometry here is a planning obstacle. It is not continuous collision
safety and it authorizes no execution.

`self_collide` is deliberately `false`: the base enclosure is a box and the four
wheel bogies sit inside its footprint, and a box cannot express a wheel well.
Enabling self-collision would turn that modelling artefact into simulated
internal contact. This is a documented limitation, not a safety claim.

## Control boundary

`ros2_control` is opt-in behind `sim_gz:=true` and declares named controllers
only. There is no whole-body joint group and no unguarded trajectory path. The
lift carries the shoulders, torso, head and both arms, so it is reachable only
through a named trajectory controller. STOP and safe-stop authority stay with
Motion and the safety controller; this description grants no execution authority.

## Verification

`make description-check` expands the model, checks it against the mechanical
spec, and regenerates and diffs a generated frame artifact so a stale or
hand-edited artifact fails. The validator asserts the documented end value rather
than a derived residual, so changing a residual cannot quietly move the value it
exists to preserve.

A divergence from the mechanical spec fails validation unless it is listed with a
reason. One is: the neck pedestal is taller than
`head/neck_mount/pedestal_height_mm` because the documented height chain leaves
only 42 mm between the torso top and the stowed head bottom, which fits no
pedestal. The reasoning is recorded in the divergence list and in `FRAMES.md`.

## What is not claimed

- **No physical or HIL evidence.** Status is `NOT_EXECUTED`, and the mechanical
  spec's `validation_status` remains `CONCEPT_PHYSICAL_VALIDATION_REQUIRED`.
- **No stability result.** The analytical screen is not a manipulation release,
  the required physical tests have not been run, and the stabilizers are a
  modelled mechanism with a derived deploy geometry.
- **No navigation capability.** The chassis exposes a holonomic mechanical
  interface; localization, mapping and obstacle avoidance are owned elsewhere.
- **No validated arm torque.** The `effort` values are the spec's peak torques,
  a saturation the controller must respect, not a measured profile.

The Gazebo acceptance cases required for this model report `NOT_EXECUTED` when no
simulator is present, so an unrun case is never rendered as a pass.
