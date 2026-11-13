# ADR-0004: Arm selection — UR5e + Robotiq 2F-85

## Status

Accepted for v0.1 phase 1 (Motion). Supersedes the Motion plan's leaning toward
Panda; see "Decision" for why.

## Context

The Motion plan is not published in this repository; its Phase 1 gates are quoted
here so the decision stands on committed text. Phase 1 requires selecting an
**official vendor** arm asset — explicitly *not* a self-assembled URDF — and
composing it into the workbench world at `robot/description/workbench.urdf.xacro`.
The task must run under Ubuntu 24.04 + ROS 2 Jazzy + Gazebo Harmonic, no GPU.

The two Phase 1 gates this decision is measured against:

1. **Vendor package first.** Verify the vendor description package exists, is
   licensed for distribution, and loads, before fixing the model
   ("先验证 vendor 包再定型").
2. **≥95% reachability.** At least 95% of the sampled grasp poses must be
   kinematically reachable from the fixed base, measured position-level.

The plan leaned toward Franka Panda (7 DoF, richest assets) with UR5e (6 DoF) as a
backup, but left the model choice open pending gate 1. We verified availability and
licensing before deciding.

## Verified facts (checked 2026-08-08, ROS 2 Jazzy)

| Fact | Panda | UR5e |
|---|---|---|
| Official description pkg | `ros-jazzy-moveit-resources-panda-description` (test resource, not a maintained vendor line) | `ros-jazzy-ur-description` (maintained Universal Robots line) |
| Already installed in this env | no | **yes** (`ur_description`, `robotiq_description`) |
| MoveIt config pkg on apt | `moveit-resources-panda-moveit-config` (test-only) | `ros-jazzy-ur-moveit-config` (maintained) |
| Gripper | integrated `franka_hand` | **none** — needs an add-on |
| Gazebo sim helper | none first-party | `ros-jazzy-ur-simulation-gz` |
| URDF/xacro license | Apache-2.0 (clean) | BSD-3-Clause (clean) |
| Mesh license | BSD-ish (moveit resources) | **proprietary — "Universal Robots A/S' Terms and Conditions for Use of Graphical Documentation"** (see THIRD_PARTY_REVIEW) |

## Decision

Use **UR5e** as the phase-1 arm, with a **Robotiq 2F-85** parallel gripper
(`ros-jazzy-robotiq-description`) attached at the UR `tool0` frame.

Reasons, in priority order:

1. **Maintained official vendor line, not a test fixture.** `ur_description` /
   `ur_moveit_config` are the maintained Universal Robots ROS 2 packages. The
   Panda assets on Jazzy are `moveit-resources-*` — MoveIt's own *test* fixtures,
   not a vendor-maintained product line. The plan's intent ("官方机械臂资产,不自己
   拼 URDF") is better served by the maintained line.
2. **Already installed + apt-clean dependency graph.** `ur_description` and
   `robotiq_description` are present; the rest (`ur-moveit-config`,
   `ur-simulation-gz`, `moveit`, `trac-ik`) resolve cleanly on apt (verified with
   `apt-get install --just-print`, exit 0). No source builds, no vendoring.
3. **First-party Gazebo Harmonic support** via `ur-simulation-gz`, which the Panda
   test resources lack — this de-risks phase 2 (ros2_control) and phase 5 (Gazebo
   execution).
4. **6 DoF is sufficient** for top-down pick-and-place of a 40 mm cube into a tray
   on a fixed table. The 7th DoF of Panda buys redundancy we do not need for this
   task; the plan itself notes UR5e is a valid choice.

Trade-off accepted: UR5e ships **no gripper**, so we add Robotiq 2F-85. And the
UR **meshes are under a proprietary license** — flagged in THIRD_PARTY_REVIEW with
an exit path (primitive collision geometry / model swap), not treated as clean.

## Alternatives considered

- **Franka Panda.** Integrated gripper and 7 DoF are attractive, but only test-
  fixture assets are on Jazzy apt, no first-party Gazebo helper, and it is not
  installed here. Kept as the documented fallback if the UR mesh license blocks
  distribution (the swap is config-only — see README swap checklist).
- **UR3e / UR10e.** Same family; UR3e reach (0.5 m) is marginal for spanning both
  the block start region and the tray, UR10e (1.3 m) is oversized for a 1.2 m
  table. UR5e (0.85 m reach) fits the workspace with margin.
- **Self-built URDF.** Explicitly rejected by the plan — tuning a home-made arm's
  physical parameters costs an extra week and defeats "官方资产".

## Consequences

- Arm-specific identifiers (planning group, `base_link`, `tool0`/EE link, gripper
  group and links, joint count, base placement pose) are isolated in
  `robot/control/workbench_motion/config/arm.yaml` and xacro args. A later swap to
  Panda touches configuration only, never adapter logic (phase 1 acceptance gate +
  README swap checklist).
- The UR mesh license is a distribution risk carried in THIRD_PARTY_REVIEW. If it
  cannot be cleared, the exit is either primitive-only collision geometry (visual
  meshes dropped) or the documented Panda swap.
- Reachability is validated against UR5e's 0.85 m reach envelope. The base was
  tuned against gate 2 (≥95% reachability): an initial back-corner placement
  `(-0.42, -0.28)` kinematically reached 100% of poses but left the tray at the
  reach extreme, where every wrist yaw collided at 3/20 tray positions (85%, fail).
  Moving the base to `(-0.30, -0.15, 0.75)`, yaw `0.36`, facing the block+tray
  centroid, shortened the tray reach (~0.66 m → ~0.52 m) and cleared it:
  **block 20/20, tray 20/20, both 100%**, stable across seeds 0/7/42
  (`docs/evaluation/phase1-reachability.json`, verified 2026-08-09 with MoveIt +
  TRAC-IK). The gate metric is position-level: a position is graspable if ≥1
  top-down yaw is collision-free and IK-valid — a parallel-jaw grasp is symmetric
  mod 180° and the planner chooses the yaw, so requiring a fixed random yaw (an
  earlier mistake) measured a capability the system never uses.
- The world attachment has exactly ONE source: the merged URDF's generated
  `base_joint` (world → base_link). The SRDF intentionally declares no
  `virtual_joint` for this — a second declaration is redundant and MoveIt warns
  on / rejects a virtual joint whose child already has a URDF parent joint. (This
  SRDF choice is validated structurally by `check_urdf`; full confirmation needs
  a `move_group` parse once MoveIt is installed.)

## Phase-1 follow-up carried into phase 3

- **`camera_body` has no collision geometry.** In
  `robot/description/workbench.urdf.xacro`, `camera_body` is a hand-written link
  with only `<visual>` — no `<collision>` (unlike `camera_post`, which uses the
  `static_box` macro that includes collision). MoveIt therefore does **not** see
  the camera body as an obstacle, so a plan passing near it could clip it. Phase 1
  is unaffected: the reachability sampling regions (block, tray) are far from the
  camera post, and the ≥95% gate passes. But before phase-3 full collision
  planning, `camera_body` needs a `<collision>`. That file is owned by
  `robot/description` (Simulation/description owner), outside Motion's write
  scope, so phase 3 must either get the owner to add it, or add a single
  PlanningScene collision object for the camera body on our side — a deliberate,
  documented exception to the "merged-URDF-is-the-only-collision-source" rule,
  justified by the missing source geometry.

## Reproduce

```bash
# vendor packages (already-installed ones omitted)
sudo apt-get install -y ros-jazzy-moveit ros-jazzy-moveit-py \
  ros-jazzy-trac-ik-kinematics-plugin ros-jazzy-ur-moveit-config \
  ros-jazzy-ur-simulation-gz ros-jazzy-gz-ros2-control \
  ros-jazzy-controller-manager ros-jazzy-joint-trajectory-controller \
  ros-jazzy-robotiq-controllers

# structural validation (after colcon build + source install/setup.bash, so
# $(find workbench_motion) resolves the vendored world)
xacro $(ros2 pkg prefix workbench_motion)/share/workbench_motion/config/arm_on_workbench.urdf.xacro > /tmp/wb_arm.urdf
check_urdf /tmp/wb_arm.urdf
```
