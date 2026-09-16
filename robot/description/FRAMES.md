# Frames and dimensions

Every number in `workbench.urdf.xacro` and why it is that number. Change a
dimension without reading this and something downstream breaks quietly.

---

## Frame tree

```
world
└── table                    slab centre
    └── table_surface        the working plane — express scenario poses here
        ├── tray_base        tray origin, on the surface
        │   ├── tray_floor
        │   ├── tray_wall_xp / xn / yp / yn
        │   └── tray_cavity  ← containment is computed against this
        ├── module_red       free body; pose comes from the scenario manifest
        └── camera_post
            └── camera_body
                └── camera_optical   REP-103: z fwd, x right, y down
```

The arm attaches at `world` and is composed at launch. It is not in this file.

---

## Why `table_surface` exists

Scenario poses are expressed relative to `table_surface`, not `table`.

`table` is the slab *centre*, so a pose relative to it depends on
`table_thick`. Change the slab from 40 mm to 30 mm and every object in every
manifest shifts by 5 mm — the manifests still validate, the scenes are all
subtly wrong, and nothing reports it.

`table_surface` sits on the working plane. Objects placed relative to it stay
put when the slab changes.

---

## Dimensions

| Property | Value | Reasoning |
|---|---|---|
| `table_x` × `table_y` | 1.20 × 0.80 m | Fits a 850 mm-reach arm's workspace with margin on all sides |
| `table_thick` | 0.04 m | Structural only. Nothing depends on it because poses go through `table_surface` |
| `table_height` | 0.75 m | Standard bench height; matches where a real arm base would mount |
| `tray_x` × `tray_y` | 0.24 × 0.18 m | Holds several 40 mm modules with clearance for approach error |
| `tray_wall` | 0.006 m | Thin enough not to dominate the interior, thick enough for stable contact |
| `tray_depth` | 0.05 m | Deeper than the module is tall, so a placed module is unambiguously inside |
| `tray_floor` | 0.004 m | Gives the cavity a real floor to rest on |
| `module_size` | 0.040 m | Inside a standard parallel gripper's stroke, with approach margin |
| `module_mass` | 0.050 kg | Light enough that hard contact flicks it away — deliberate, see below |
| `cam_height` | 0.70 m | Sees the module start region and the tray in one frame |

### Why the module is deliberately light

50 g means a badly tuned contact model throws it across the table.

That is the failure mode physics tuning exists to remove. Making the module
heavy would mask bad contact parameters — grasps would succeed for the wrong
reason, and the same parameters would fail on real hardware where the object
really is light.

---

## The tray is five parts

Floor plus four walls, not one box.

The verifier decides "is the module in the tray" by spatial containment against
`tray_cavity`. That requires an interior volume. A single box has no interior —
containment against a solid is either intersection with the solid itself
(meaningless) or nothing.

### Interior extents

Consumers need these. Derived, not hardcoded:

```
x: tray_x    - 2 * tray_wall  = 0.240 - 0.012 = 0.228 m
y: tray_y    - 2 * tray_wall  = 0.180 - 0.012 = 0.168 m
z: tray_depth -    tray_floor = 0.050 - 0.004 = 0.046 m
```

`tray_cavity`'s origin is at the centre of that volume.

**Read them from the model, not from a constant in Python.** A hardcoded
0.228 in the verifier goes stale the first time someone widens the tray here,
and the resulting failure looks like a perception problem.

---

## Containment is three-valued

| Overlap ratio | Status | Meaning |
|---|---|---|
| ≥ 0.95 | `confirmed` | fully inside |
| 0.01 – 0.95 | `insufficient_evidence` | caught on the rim — retry, don't replan |
| ≤ 0.01 | `refuted` | outside |

The middle band is the reason the cavity has to be geometrically real. A
boolean test forces "wedged on the tray edge" into success or failure, when it
actually means *placed badly, retry the action*.

Full reasoning: `docs/algorithms/world-model.md`.

---

## `camera_optical` is not `camera_body`

Two frames, 90° apart, and getting them confused is the classic silent bug.

| Frame | Convention |
|---|---|
| `camera_body` | the physical box. x forward in body terms |
| `camera_optical` | REP-103. **z forward, x right, y down** |

The ROS image pipeline and every tag detector assume the optical convention.
Publish detections in the body frame and poses come out rotated 90°.

It still looks plausible in RViz. Nothing errors. The detections are simply in
the wrong place, and the first symptom is grasps missing by a consistent offset
that gets blamed on calibration.

**Verify visually once.** `check_urdf` will not catch this — it validates
structure, not whether a rotation makes physical sense.

---

## `camera_optical` has no collision, `camera_body` must

`camera_optical` is a convention frame, not a solid object, so it carries no
visual and no collision geometry. Adding a collision volume to it would make the
planner treat a zero-size frame as an obstacle.

`camera_body` is the solid housing and therefore carries **both** a visual and a
collision box of `0.05 0.05 0.03`. This was missing (#261): the housing was
visible in RViz but invisible to MoveIt, so a C3 path could be accepted that
clips it. Both elements share one `camera_body_size` property, and the two box
sizes are equal by construction rather than by two literals staying in sync.

`tests/unit/test_robot_description_camera_collision.py` fails if the collision
element is removed or if the two boxes diverge. Collision geometry here is a
planning obstacle only — it is not continuous collision safety and does not
authorize execution.

---

## Camera noise is non-zero on purpose

`stddev = 0.007`.

A noiseless camera lets perception pass with confidence thresholds that would
fail immediately on real hardware. Detection would look solved in simulation
and collapse at hardware bring-up, with no way to tell whether the regression
came from the camera, the lighting, or the detector.

The value is a placeholder until real-camera jitter is measured. Same
measurement feeds the pose quantisation step in
`docs/algorithms/world-model.md` §6.

---

## Friction values will change

| Link | `mu1` / `mu2` | Note |
|---|---|---|
| `module_red` | 0.8 | plastic on plastic, starting point |
| `tray_floor` | 0.6 | |
| `table` | 0.7 | |

These are the numbers physics tuning moves. They start plausible and are
expected to change.

**Tuning rule: one parameter at a time.** Friction and solver settings both
affect grasp success. Change both between runs and the result cannot be
attributed. This is why physics tuning is measured in weeks and cannot be
parallelised across two people working independently.

---

## What is not in this file

| Not here | Where | Why |
|---|---|---|
| The arm | official vendor package, composed at launch | Lets the world and the arm be built in parallel |
| Joint limits, controllers | `robot/control/` | Different owner, different review path |
| Scenario object poses | `sim/scenarios/frozen/*.yaml` | Seeded per run; the same seed must rebuild the same scene |
| Bit-exact camera intrinsics | calibration output, real hardware | Simulated intrinsics are not the real ones |

---

## Checks

```bash
# Expand and validate structure
xacro robot/description/workbench.urdf.xacro > /tmp/wb.urdf
check_urdf /tmp/wb.urdf

# Look at the tree
urdf_to_graphiz /tmp/wb.urdf

# Then look at it in RViz and confirm camera_optical points at the table.
# This is the one thing the tools cannot check for you.
```

`check_urdf` catches unparented links and malformed joints. It does not catch a
frame rotated the wrong way, a cavity that does not line up with its walls, or
an inertia tensor that is physically impossible.

---

## On CAD

ROS does not consume SolidWorks files. The pipeline is CAD → STL/DAE mesh →
referenced from URDF. Official arm packages already ship both URDF and meshes.

Everything here is primitives, so nothing needs machining at this stage.

If a part does need machining later, the order matters: **lock the dimensions
and frames in URDF first, then model CAD to match those numbers.** Doing it the
other way round means joint axes and frame origins drift from what the software
already assumes, and reconciling them afterwards is worse than it sounds.
