# Desktop Manipulation Research Platform Roadmap

## Purpose and current baseline

This roadmap extends Workbench-1 into a reproducible desktop manipulation
research platform while preserving the existing semantic boundary:

```text
Task -> Planner -> Controller or RL Policy -> Simulator or Robot
     -> Observation -> WorldState -> Verifier -> VerificationResult
                                      |
                                      +-> event log -> replay -> evaluation
```

The repository already has two layers that must remain distinct:

- `services/agent_runtime/` plans typed semantic actions. It must not emit joint
  position, velocity, torque, or emergency-stop commands.
- `services/world_model/`, the JSON Schemas under `interfaces/`, and matching
  Pydantic models under `libs/contracts/` define state and verification meaning.
  Motion, simulation, and RL producers must use those contracts rather than
  inventing parallel `WorldState`, `TaskGraph`, or `VerificationResult` models.
- `robot/control/workbench_motion/` contains the UR5e and Robotiq 2F-85 motion
  package. The arm has six controlled revolute joints. The gripper has one
  controlled driver joint and five mimic followers.
- `sim/scenarios/` and the scripted runner provide deterministic pipeline
  fixtures. They are labelled `SCRIPTED_FIXTURE` and are never Gazebo, MuJoCo,
  RL, or hardware evidence.

The motion baseline includes MoveIt reachability, vendor and hardware-override
joint limits, trajectory preflight, ros2_control configuration, and a Gazebo
Harmonic `FollowJointTrajectory` adapter. `RobotState`, `RobotCommand`, and
`Controller` form a ROS-free control boundary. `AcceptedTrajectoryExecutor`
checks immutable trajectory, context, and state hashes before dispatch.
`phase2_probe` can record real Gazebo controller, TF, collision, convergence,
and gripper evidence when all runtime endpoints are present. A missing runtime
is `NOT_EXECUTED`; mocked tests only prove deterministic logic.

## Dependency rules

Dependencies flow downward through explicit ports:

```text
Task contracts
    -> MotionPlanner / Policy interfaces
        -> RobotCommand / Controller
            -> SimulatorBackend or hardware adapter

Simulator and robot observations
    -> WorldState producer
        -> task Verifier
            -> VerificationResult
```

Gazebo and MuJoCo APIs stay inside their backend adapters. An RL policy depends
only on versioned `Observation` and `Action` values. It cannot call simulator
APIs or bypass controller limits. MoveIt remains one replaceable
`MotionPlanner`; it is not embedded in `Controller`. Cartesian motion follows
`Cartesian target -> IK -> Joint target -> Trajectory -> Controller -> RobotState`.

Any change to `interfaces/` requires the matching Pydantic update, producer and
consumer review, three human approvals, and `make contract`. Each implementation
phase must use a bounded Task Packet and preserve event logs, replay,
deterministic scenarios, and fail-closed behavior.

## Phase plan

### Phase 1: Existing code analysis

Inputs are the repository, schemas, Pydantic models, tests, CI, task packets,
and current evaluation artifacts. The output is an evidence-based architecture
audit with paths and line references. Completion requires explicit separation
of real runtime capability, deterministic fixture behavior, and missing
capability. No repository files change during the audit.

### Phase 2: Unified RobotState, RobotCommand, and Controller interface

Keep the interface in `robot/control/workbench_motion/workbench_motion/` and
independent of ROS, Gazebo, and MuJoCo. It owns finite typed feedback,
position/velocity/trajectory commands, lifecycle modes, cancel, stop, hold, and
reset. The trajectory port accepts only immutable preflighted trajectories and
records state/context hashes and whether dispatch was attempted.

Completion requires ROS-free tests for invalid data, stale feedback, lifecycle
transitions, zero dispatch on gate rejection, backend rejection receipts, and
safe-stop failure. Gazebo tests may mock ROS message transport but may not count
as simulation evidence.

### Phase 3: Complete Joint Controller

Add position and velocity implementations, quintic or jerk-limited trajectory
generation, tracking, and limits for joint position, velocity, acceleration,
and jerk. Record position error, velocity error, acceleration, jerk, RMS/max
tracking error, smoothness, and settling time. Preserve a seam for future
impedance/admittance control with `K`, `D`, `M`, position error, velocity error,
and external force.

Completion requires deterministic unit tests across seeds and motion validation
for limits, stop, hold, reset, convergence, and failure paths.

#### Trajectory Algorithm Sequence

Trajectory work stays in the ROS-free joint-space core until an explicit
integration Task Packet permits a controller API change. The post-quintic order
is driven by real-robot smoothness and preserves the path from Cartesian target
through IK, joint target, trajectory, controller, and feedback.

1. **Rest-to-rest jerk-limited S-curve point-to-point.** Implement a minimum-
   time, fixed-seven-slot profile under explicit position, velocity,
   acceleration, and jerk bounds. Short moves retain all seven slots but set
   unavailable plateaus to zero duration, giving 7, 6, 5, or 4 active segments.
   Synchronize joints to the slowest axis by temporal scaling. This is the first
   next algorithm and remains independent of `JointController` initially.
2. **Trajectory-generator strategy seam.** Add an internal selectable generator
   only after both quintic and S-curve primitives have unit evidence. Keep
   quintic as the compatibility default while the controller API stays stable.
3. **Nonzero-boundary and multi-waypoint joint trajectories.** Add stop-free
   segment joining, waypoint continuity, blend-radius policy, and global
   position/velocity/acceleration/jerk proofs before accepting streamed paths.
4. **Path-constrained time parameterization.** Retiming follows a geometric
   joint or Cartesian path after IK, initially with TOPP-RA class velocity and
   acceleration bounds, then jerk-aware retiming. It must keep collision and
   kinematic constraints outside the trajectory primitive.
5. **Cartesian line, arc, and screw motion.** Place Cartesian geometry above
   IK and joint retiming; no Cartesian target becomes an actuator command.
6. **Online trajectory generation.** Evaluate Ruckig or an equivalent bounded
   online generator only after the offline contracts and feedback timing are
   stable. It is for replanning from measured state, not a bypass around the
   controller boundary.
7. **Interaction control.** Add impedance/admittance and, later, constrained
   MPC only after force-feedback provenance, contact handling, and safety limits
   are present. These algorithms require `K`, `D`, `M`, external force, and
   measured position/velocity at a separate controller boundary.

### Phase 4: Gazebo basic closed loop

Place Gazebo-specific code in `robot/control/workbench_motion/` or a dedicated
backend under `sim/`. Implement `JointState -> Controller -> Gazebo -> JointState`
feedback through a shared `SimulatorBackend` contract: `reset`, `step`,
`get_observation`, `get_robot_state`, `get_contacts`, `get_time`, and `is_done`.
Add sampled path collision checks, runtime limit monitoring, zero dispatch for
rejected commands, stopping evidence, and event emission.

Completion requires the project Docker image to launch Gazebo and RViz2 with
host display forwarding, three active controllers, a complete TF chain, legal
trajectory convergence from fresh feedback, collision evidence, and explicit
classification of raw over-limit behavior. Runtime evidence must record image
identity, commit, seed, configuration hashes, and commands.

### Phase 5: MuJoCo Environment

Implement a MuJoCo backend behind the same `SimulatorBackend`. Add a versioned
model, reset/step loop, observation and action conversion, reward components,
termination reasons, contacts, simulation time, and rendering. MuJoCo objects
remain private to the adapter.

Completion requires seeded reset reproducibility, controller-limit enforcement,
contact and termination tests, model validation, and metrics for real-time
factor and FPS. Generated samples are labelled MuJoCo evidence only after the
real engine executes.

### Phase 6: Reach RL

Define the smallest versioned observation/action contract needed for joint or
Cartesian reach. A policy produces bounded actions for the controller boundary.
Reward records distance, action smoothness, collision, and completion
components. Episode logs include seed, environment version, policy identity,
termination, and reward terms.

Completion requires training/evaluation on held-out seeds, success and collision
rates, reward curves, generalization results, and seed reproducibility. Policy
success is provisional until execution updates `WorldState` and a verifier
returns `VerificationResult`.

### Phase 7: Pick and Place RL

Extend observation with object, gripper, and goal state. Compose reach, grasp,
transport, release, and retreat without letting the policy write WorldState.
Record grasp loss, collision, completion, insufficient evidence, and recovery.

Completion requires frozen training/evaluation splits, verified placement from
WorldState, false-success analysis, and replayable episode logs.

### Phase 8: Domain Randomization

Randomize object pose and mass, friction, sensor noise and latency, actuator
response, lighting, and camera parameters from a versioned seeded configuration.
Log every sampled value.

Completion requires deterministic materialization from seed, distribution
coverage checks, nominal versus randomized metrics, and evaluation on unseen
parameter combinations.

### Phase 9: RL to Controller to Gazebo

Connect the policy action adapter to `RobotCommand`, controller preflight, and
the Gazebo backend. Preserve the same limits and safe-stop paths used by planned
motion. Emit correlated policy, command, execution, observation, and safety
events.

Completion requires evidence that policy output cannot reach Gazebo except
through `Controller`, plus tracking, collision, completion, and recovery metrics
from real Gazebo runs.

### Phase 10: Task to RL/Controller to Verification

Extend the existing semantic flow to select a `MotionPlanner`, `RLPlanner`, or
controller operation through typed tools. Execution observations update
WorldState through the existing producer path, then the task verifier returns a
contract-valid `VerificationResult`.

Completion requires `Task -> Planner -> execution -> Observation -> WorldState
-> VerificationResult` correlation, fail-closed insufficient-evidence behavior,
and zero cases where action success alone becomes verified success.

### Phase 11: Evaluation and Replay

Unify structured motion, RL, simulation, task, and verification metrics. Motion
reports RMS/max tracking error, smoothness, settling time, acceleration, and
jerk. RL reports reward terms, success, collision, completion, and generalization
rates. Simulation reports real-time factor, FPS, and seed reproducibility. Task
reports verified success, false success, insufficient evidence, and recovery
success.

Completion requires event-log replay to reproduce WorldState and verification
decisions, provenance for every metric, schema validation, and comparisons that
never mix fixture, Gazebo, MuJoCo, and hardware result classes.

### Phase 12: Sim2Real interface

Add a hardware adapter behind the same controller and observation boundaries.
Define calibration, timestamp alignment, capability discovery, watchdogs,
emergency-stop ownership, safe startup/shutdown, and read-only shadow mode.
High-level tasks and policies still cannot issue emergency stop or raw actuator
commands.

Completion requires owner-approved hardware tests, staged shadow and low-speed
runs, simulator-to-hardware metric comparison, rollback procedures, and real
WorldState verification. Simulation evidence is never promoted to hardware
evidence.

## Reproducibility, testing, and delivery gates

Every experiment accepts a seed and records the commit, dependency and image
versions, configuration hashes, model/policy identity, structured events, raw
runner logs, and result classification. Atomic artifacts distinguish
`SCRIPTED_FIXTURE`, `GAZEBO`, `MUJOCO`, `HARDWARE`, and `NOT_EXECUTED`.

Each phase adds tests for deterministic behavior and runs the relevant runtime
validation. Repository-wide delivery keeps these checks green:

```bash
make test
make contract
make scenario-check
make context-check
```

Motion changes also run the package pytest suite, colcon build, URDF validation,
and motion validation. Simulation changes run a real backend launch and record
the resulting artifact. A PR documents commands, exact outcomes, unavailable
checks, evidence paths, known limitations, and the next bounded phase.
