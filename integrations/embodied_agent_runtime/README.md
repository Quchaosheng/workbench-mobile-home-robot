# Embodied Agent Runtime

The ROS 2 execution layer of Workbench: the code that turns an approved task
into BehaviorTree.CPP orchestration, nested ROS 2 Actions, SocketCAN device
commands, and a terminal `TaskEvent`.

This directory was previously the standalone `embodied-agent-runtime`
repository. It is now vendored here so one repository carries the whole
evidence chain, from the product-side goal to the device command and back to
the verifier. The Python adapter that reads its output lives next door in
[`../ros2_runtime/`](../ros2_runtime/README.md).

## Where this fits

Workbench has two execution halves that were separate repositories:

| Half | Language | What it owns | Where |
| --- | --- | --- | --- |
| Evidence plane | Python | Event store, World Model, verifier, replay, dashboard, evaluation | `services/`, `libs/`, `tools/`, `apps/` |
| Execution plane | C++ / ROS 2 | Bounded task orchestration, device commands, runtime diagnostics, task history | this directory |

They meet at exactly one artefact: the terminal `TaskEvent` on
`/runtime/task_events`.

```text
goal -> bounded planner -> TaskGraph          (evidence plane, Python)
                |
                v
     ExecuteWorkflow / ExecuteTask / ExecuteDeviceCommand   (this directory)
                |
                v
        fixed BehaviorTree.CPP -> Device Bridge -> SocketCAN -> device
                |
                v
          TaskEvent  --->  integrations/ros2_runtime  --->  action_result event
                |                                              |
          SQLite history                          World Model verifier + replay
```

The split matters because the two halves make different claims. A `TaskEvent`
proves an Action terminal status and a protocol outcome. It never proves that an
actuator moved — that stays with the World Model verifier and observation
evidence. Keeping the runtime in the same repository makes that boundary
reviewable in one diff instead of across two, but it does not merge the two
claims.

## What is vendored

```text
embodied_agent_runtime/
├── ros2_ws/src/           11 ROS 2 packages (see below)
├── scripts/               vcan setup, arm64 checks, e2e runners, doc checks
├── docs/                  runtime design notes and history
├── Dockerfile             ros:jazzy-ros-base build used by scripts and CI
├── Makefile               make demo-local, the containerised end-to-end run
└── README.runtime.md      the original runtime README, kept for its detail
```

The ROS 2 packages:

| Package | Role |
| --- | --- |
| `robot_task_interfaces` | `TaskEvent`, `DeviceState` and the Action definitions |
| `task_orchestrator` | `ExecuteWorkflow` Action server, fixed BehaviorTree.CPP tree |
| `task_executor` | `ExecuteTask` Action server, target validation, deadline |
| `device_bridge` | `ExecuteDeviceCommand` Action server, the SocketCAN boundary |
| `runtime_can` | CAN framing and transport |
| `runtime_gateway` | gRPC gateway and durable idempotency |
| `runtime_history` | SQLite task history |
| `runtime_monitor` | Runtime diagnostics |
| `ai_task_adapter` | Rule and model text adapters |
| `perception_task_adapter` | ArUco image and camera adapters |
| `virtual_can_device` | Virtual device for bench testing without hardware |

## Running it

The runtime needs ROS 2 Jazzy and a Linux Docker host, because `vcan` is Linux
networking. Nothing here is required for the Python evidence plane, which runs
offline.

```bash
cd integrations/embodied_agent_runtime
make demo            # builds the image and runs the end-to-end scenario
```

The demo drives the fixed tree through a virtual CAN device, so it proves the
plumbing without an actuator. Native X5/ARM64 execution, a physical USB camera,
ArUco detection and bidirectional SocketCAN bench traffic are verified
separately; see `README.runtime.md` for the current status table and
`docs/PORTABILITY.md` for the arm64 profile.

## CI

`ros2-ci.yml` runs from the repository root and is scoped to this directory, so
a Python-only change does not pay for a ROS 2 build. It keeps the two jobs the
standalone repository had: PowerShell syntax and WSL tool behaviour on Windows,
and a colcon build plus test on Ubuntu 24.04.

## History

The vendoring commit is the record of the move: `integrations/embodied_agent_runtime/`
is the previous repository at its final commit, with no source changes. Later
work on the runtime happens here. `docs/history/` keeps the runtime's own
decision log from before the move, because those notes explain why the tree is
fixed and why cancel and device-ACK are non-bypassable.
