# Revision D frames and dimensions

Every number in `revision_d.urdf.xacro` and which
`hardware/mechanical/design-spec.json` value it comes from. The authority is the
mechanical spec; this file and `generated/revision-d-frames.json` are the
machine-checkable bridge to it.

Change a dimension here without reading this and `make description-check` should
fail. If it passes anyway, an anchor is decorative and that is the bug.

---

## Frame tree

```
map
└── odom
    └── base_footprint
        └── base_link                     chassis enclosure, origin at its bottom
            ├── bumper_front
            ├── base_camera_body
            │   └── base_camera_optical
            ├── drive_module_{front,rear}_{left,right}_steer_mount
            │   └── ..._steer             steering joint (continuous, Z)
            │       └── ..._wheel         drive joint (Y)
            ├── stabilizer_{front,rear}_{left,right}_arm
            │   └── ..._pad               prismatic deploy joint (local X)
            ├── lift_column
            └── lift_joint (prismatic, Z)
                └── lift_carriage
                    ├── torso
                    │   ├── parcel_bay
                    │   ├── tool_dock
                    │   │   └── tool_dock_slot_{1,2,3}
                    │   └── neck_pedestal
                    │       └── neck_yaw          neck_pan_joint (Z)
                    │           └── head_display  neck_tilt_joint (Y)
                    │               ├── head_window (visual only)
                    │               └── head_camera_body
                    │                   └── head_camera_optical
                    ├── arm_{left,right}_shoulder_yoke
                    │   └── arm_*_joint_1..7     seven revolute axes
                    │       └── arm_*_tool0
                    └── (each forearm also carries arm_*_wrist_camera_body
                         └── arm_*_wrist_camera_optical)
```

The head hangs off the torso through the neck chain and never off an arm. The
yokes bolt into the upper torso side walls and into the lift-supported spine.

---

## Conventions

| Convention | Value | Why |
|---|---|---|
| World units | metres, radians, kilograms | `design-spec.json` is millimetres; xacro divides by 1000 exactly once per anchor |
| Root frame | `map` | one root, `validate_description.py` asserts it |
| `base_link` origin | bottom of the base enclosure, on the ground-clearance plane | the usual ROS convention; `odom` chains assume it. The enclosure top lands on `coordinate_system.base_top_z_mm` |
| Base axes | x forward, y left, z up | REP-103. Note `design-spec.json`'s `robot_base` uses **x right, y rear** |
| Optical frames | z forward, x right, y down | REP-103. Publishing detections from a body frame gives a silent 90° error that still renders |
| Drive angle | continuous | `chassis.steering_range_deg` is `continuous_360_with_through_bore_slip_ring` |
| Wheel spin axis | link local Y | so the wheel cylinder's own Z is rotated onto Y by the joint origin |

### The x-axis trap

`design-spec.json` (`stability_analysis.coordinate_convention`) defines
`robot_base` as **x = robot right, y = robot rear**. ROS base frames are
**x forward, y left**. Both are legitimate; mixing them is not.

This is why the stabilizer corners are selected by sign in the description
(`sx`, `sy`) and the deploy direction is *derived* as
`atan2(sy·reach_y, sx·reach_x)` rather than written as four hand-picked yaw
angles. A hand-picked yaw is where the sign error lives: during development two
of the four pads were assigned to each other's corners and all four collapsed
onto one axis before the derived form replaced them.

---

## Dimensions and their anchors

| Property | Value | `design-spec.json` pointer |
|---|---|---|
| `chassis_x` × `chassis_y` | 0.540 × 0.520 m | `/chassis/width`, `/chassis/depth` |
| `base_housing_z` | 0.112 m | derived: `/coordinate_system/base_top_z_mm` − `/chassis/ground_clearance` |
| `ground_clearance` | 0.028 m | `/chassis/ground_clearance` |
| `wheelbase` × `track` | 0.390 × 0.460 m | `/chassis/wheelbase`, `/chassis/track` |
| `wheel_radius` | 0.070 m | half of `/chassis/wheel_diameter_mm` |
| `wheel_width` | 0.042 m | `/chassis/wheel_width_mm` |
| `module_mount_z` | 0.095 m | derived so the wheel centre sits at `wheel_radius` |
| `stabilized_half` | 0.410 m | `/stability_analysis/support_polygons/stabilized/vertices_mm` |
| `stabilizer_stroke` | 0.280 m | derived: `hypot(reach_x, reach_y)` |
| `lift_travel` | 0.350 m | `/lifting_platform/travel` |
| `shoulder_min_z` | 0.780 m | `/lifting_platform/minimum_shoulder_height` |
| `shoulder_max_z` | 1.130 m | `/lifting_platform/maximum_shoulder_height` |
| `torso_x/y/z` | 0.420 × 0.330 × 0.430 m | `/torso/width`, `/depth`, `/height` |
| `torso_bottom_z` | 0.500 m | `/coordinate_system/torso_bottom_stowed_z_mm` |
| `head_x/y/z` | 0.260 × 0.105 × 0.128 m | `/head/width`, `/depth`, `/height` |
| `head_top_stowed_z` | 1.100 m | `/coordinate_system/head_top_stowed_z_mm` |
| `shoulder_spacing` | 0.352 m | `/shoulder_mounts/center_spacing_mm` |
| `upper_arm_length` | 0.275 m | `/manipulator/links_mm/upper_arm` |
| `forearm_length` | 0.245 m | `/manipulator/links_mm/forearm` |
| `wrist_stack` | 0.115 m | `/manipulator/links_mm/wrist_stack` |
| `arm_reach` | 0.720 m | `/manipulator/reach_mm` |
| `tool_dock_slots` | 3 | `/torso/tool_dock_slots` |

### Derived, not copied

Several values are **not** in the mechanical spec and must not be. They are
residuals of the documented chain, derived so the documented value holds by
construction:

- `base_housing_z = base_top_z − ground_clearance`
- `module_mount_z = wheel_radius − ground_clearance + steer_drop + wheel_drop`, so
  the wheel centre lands on `wheel_radius` above the ground and the tread touches `z = 0`
- `neck_standoff = head_top_stowed_z − head_z − (torso_bottom_z + torso_z)`, so
  the stowed head top is exactly the documented value
- `stabilizer_stroke = hypot(stabilized_half − module_x, stabilized_half − module_y)`,
  so the deployed pad centre is exactly the documented polygon vertex
- `lift_column_h = torso_bottom_z − base_top_z`, so the guide column spans the
  base top to the stowed torso bottom and cannot intersect the torso it carries

Writing the derived values as literals instead is how two numbers that must
agree drift apart while everything still validates.

---

## Joint limits

All fourteen manipulator axes take their limits and peak torque from
`/manipulator/joints`.

| Joint | Axis | Limits (deg) | Peak torque (Nm) |
|---|---|---|---|
| J1 `base_yaw` | Z | −170 … 170 | 90 |
| J2 `shoulder_pitch` | Y | −90 … 120 | 130 |
| J3 `shoulder_roll` | X | −95 … 95 | 85 |
| J4 `elbow_pitch` | Y | −135 … 10 | 70 |
| J5 `forearm_roll` | X | −180 … 180 | 30 |
| J6 `wrist_pitch` | Y | −110 … 110 | 24 |
| J7 `tool_roll` | X | −180 … 180 | 16 |

`effort` is the **peak** value. The continuous rating (45/70/45/35/15/12/8 Nm)
is deliberately not the limit: a peak torque in the URDF is the saturation the
controller must respect, while the continuous rating is a thermal duty limit
that belongs in control configuration. Swapping them silently understates the
actuator and is caught by `validate_description.py`.

Neither number is a validated torque profile. Torque validation needs the
physical bench, and hardware status stays `NOT_EXECUTED`.

---

## Why `self_collide` is false

The base enclosure is a 540 × 520 box and the four wheel bogies sit inside its
footprint. A box cannot express a wheel well, so those two collision volumes
intersect. Enabling self-collision would turn that modelling artefact into
simulated internal contact and produce failures that mean nothing.

The forbidden-volume and contact tests own the exclusion set instead. This is a
documented modelling boundary, **not** a safety claim: it says the description
does not detect internal contact, so nothing may treat "self-collision disabled"
as "self-collision impossible".

---

## Collision geometry rules

1. Every solid body carries **both** a visual and a collision primitive of the
   same size. A visual-only primitive renders as a solid and reads as free space
   to the planner.
2. Every solid body carries **exactly one** collision volume, so the obstacle
   geometry cannot depend on which element a consumer happened to read.
3. Convention frames (`map`, `odom`, `base_footprint`, `parcel_bay`,
   `tool_dock_slot_*`, `*_tool0`, `*_optical`) carry **no** collision geometry.
   A zero-size frame treated as a solid is a phantom obstacle.
4. `head_window` is visual only: the head box behind it already owns the
   collision volume for that part of the shell.

Rules 1–4 are enforced for every link, not for a list of links someone
maintained.

---

## Known divergence

| Pointer | Spec | Model | Why |
|---|---|---|---|
| `/head/neck_mount/pedestal_height_mm` | 72 mm | 208 mm (`neck_standoff`) | The documented chain leaves 42 mm between the torso top and the stowed head bottom, which fits no pedestal and not the head. The neck standoff absorbs the difference; the 120 mm register/support stack is not modelled as separate links. |

The divergence is listed in `description_model.known_divergences()` and printed
into `generated/revision-d-frames.json`. A divergence that is not listed is a
validator failure, not a judgement call.

---

## Verification

```bash
# Expand and validate against design-spec.json (320 assertions)
python3 robot/description/revision_d/tools/validate_description.py

# Regenerate the frame artifact and confirm it is current
python3 robot/description/revision_d/tools/generate_frames.py --check

# The gates
make description-check
```

`check_urdf` (when a ROS install is present) catches unparented links and
malformed joints. It does not catch a frame rotated the wrong axis, a pad that
lands on the wrong polygon corner, or an inertia tensor that is physically
impossible. Those are what the validator and the unit tests are for.

**Verify the optical frames visually once.** Nothing automated catches a
convention frame that is structurally valid but points somewhere useless.
