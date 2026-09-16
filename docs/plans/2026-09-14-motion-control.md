# Motion control development plan

Baseline: `feat/motion-quintic-point-to-point`, `ee24d61`. The human selected Gazebo-first development with a future hardware port and keyboard Cartesian jogging before gamepad support. Only M1 is authorized for this implementation.

## Stages

1. M1: connect the existing rest-to-rest quintic generator to immutable preflight, a ROS-free Controller, real Gazebo execution, feedback monitoring, bounded braking, hold/reset and reproducible motion evaluation.
2. M2: replaceable MotionPlanner using existing MoveIt IK, 5 mm / 1 degree discrete Cartesian jogging, single input owner, workspace and sampled-path collision gates.
3. M3: Gazebo F/T observation with frame/point transforms, bias and known-tool gravity compensation; bounded continuous references before three-axis fixed-orientation admittance.
4. M4: Pinocchio nominal dynamics and a separate effort experiment mode for PD versus PD plus gravity. Exclude position ownership and implicit joint hold from effort evidence.
5. M5: payload/gravity/friction identification, excitation checks, held-out trajectories, and collision residuals scored against independent contact truth before enabling protection.
6. M6: joint torque impedance using the established model and effort safety boundary. Cartesian impedance, MuJoCo robot backend and Reach RL require later stages.

## M1 implementation contract

Preserve the existing RobotState/RobotCommand/Controller draft names, AcceptedTrajectory and preflight APIs. Correct the draft's tuple-versus-mapping limit access. InMemoryController remains admission-only test support.

The execution boundary admits complete q/v/a quintic trajectories only after existing preflight and continuous interpolation extrema checks. It binds scene/configuration/trajectory evidence and checks a fresh starting state using position and velocity tolerances, not equality of successive state hashes. Unsupported velocity mode is rejected. Ordinary requests do not preempt; protective stop/hold preserves the accepted reference prefix and absolute simulation epoch, then joins bounded braking at a future C2 reference knot. Measured feedback remains an independent tracking gate, never the stale replacement anchor. Normal goals reserve delivery time with a validated stationary prefix. Late dispatch/acceptance, including time spent journaling, fails closed. Missing feedback or failed stopping leaves device state unconfirmed and latches a fault. Reset requires fresh stationary feedback and readiness; it never resets the physical world or clears evidence.

Run the existing container image with a current-source colcon overlay. Add optional GUI arguments to the existing launch and use the existing X11 profile for server, Gazebo GUI and RViz in one container. Keep default headless behavior. Apply the pinned controller_manager 4.45.2 simulation-clock overlay before launch; the adapter refuses the unpatched runtime. Hardware clock behavior is unchanged. Python is a supervisory controller, not a hard-real-time control loop.

Motion logs record source/image/model/config identity, seed, measured and desired state, estimated acceleration/jerk, RMS/max tracking error, settling time and receipts. Persist only valid action_result events through the existing MotionEvidenceAdapter. No command receipt or convergence result creates WorldState facts or verified task success. Research journals are referenced artifacts, not a replacement event store.

## Verification

Add safety, lifecycle, adapter, metrics and launch tests. Preserve package tests plus `make test`, `make contract`, `make scenario-check`, `make context-check`, lint and colcon tests. Validate seeds 0, 7, 42 with repeated real Gazebo runs. Mathematical output is deterministic; physics reproducibility is measured rather than assumed bitwise.

Nominal goals use the existing 0.02 rad goal tolerance and 0.5 s goal-time allowance. Stopping requires all measured joint velocities <= 0.01 rad/s for at least 0.2 s with fresh samples. Test malformed, excessive, duplicate and stale requests with zero dispatch, backend rejection/timeout, feedback loss, moving stop/hold and guarded reset. Missing runtime is NOT_EXECUTED. Report current-run evidence separately from historical phase-2 reports.

## Compatibility and delivery

The untracked DEVELOPMENT_ROADMAP is an earlier draft and is not authoritative for completed capabilities. Do not rewrite it or unrelated WIP. Public schemas, Pydantic models, TaskGraph, WorldState, VerificationResult, deterministic fixtures and the event/replay architecture retain their meaning. Raw run data, image archives and local configuration stay out of Git. Deliver the M1 change set, commands, tests, evidence, limitations and M2 recommendation; do not implement M2 or publish changes without further instruction.

## M1 validation update (2026-09-14)

The [smoothness report](../evaluation/motion-m1-smoothness.md) records the causal diagnosis and final real-Gazebo run: 49 trials, 110 measured metric groups including whole-trial transitions, no observed acceleration/jerk exceedance at the unchanged 0.5 rad/s² and 2 rad/s³ limits. This validates the tested small shoulder-pan operating envelope, not a continuous physical guarantee or other arm/payload regimes. M2 still requires separate authorization.
