# Runtime Monitor Runbook

**English** | [简体中文](README.zh-CN.md)

`runtime_monitor` aggregates the health reported by Device Bridge and Task
Executor. It publishes a system diagnostic and an aggregate readiness bit. The
monitoring path is observation-only: it does not send an Action goal, access
SocketCAN, stop a device, restart a process, or change the state of a monitored
node.

## Architecture and interfaces

```text
Virtual CAN device <-> vcan0 <-> Device Bridge <-> Task Executor <-> AI adapter
                                  |                  |
                                  +-- /diagnostics --+
                                           |
                                   Runtime Monitor
                                      |         |
                              /diagnostics  /runtime/ready
```

The stable diagnostic names are:

- `runtime/device_bridge`
- `runtime/task_executor`
- `runtime/system`

`runtime/device_bridge` reports `ready`, `state`, `active_command_id`,
`retry_count`, `ack_timeout_count`, `device_fault_count`,
`stop_failure_count`, and `last_error_code`. Its `ready` field means that the
SocketCAN socket is open. A latched command error can therefore produce
`level=ERROR` while this local transport field remains `true`.

`runtime/task_executor` reports `ready`, `state`, `active_task_id`,
`bridge_ready`, `last_outcome`, and `last_error_code`.

`runtime/system` reports `ready`, `device_bridge_level`,
`device_bridge_age_ms`, `task_executor_level`, and
`task_executor_age_ms`. `/runtime/ready` is the authoritative aggregate Ready
signal.

The standard diagnostic levels are `OK=0`, `WARN=1`, `ERROR=2`, and
`STALE=3`. Runtime Monitor publishes every 500 ms and treats a core diagnostic
as stale after 2000 ms by default. Aggregate Ready is true only after both core
sources have been seen, both are fresh, and neither is `ERROR` nor `STALE`.
`WARN` describes degraded but usable operation and therefore keeps aggregate
Ready true.

Current error health is recoverable: one later successful task clears the
latched component error. The cumulative Bridge counters remain available after
recovery. They live only in the Device Bridge process and reset when that
process restarts; this package does not persist counters across process
restarts.

## Build and common setup

Use a clean ROS environment for the build:

```bash
unset CONDA_EXE CONDA_PREFIX CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset CONDA_PYTHON_EXE CONDA_DEFAULT_ENV PYTHONHOME PYTHONPATH
export PATH=/usr/bin:/bin:$PATH
set +u
source /opt/ros/jazzy/setup.bash
set -u
cd "${HOME}/robot-runtime-ws"
colcon build --packages-up-to \
  runtime_monitor device_bridge task_executor ai_task_adapter virtual_can_device
set +u
source install/setup.bash
set -u
```

Create `vcan0` once if it does not already exist:

```bash
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
ip -brief link show vcan0
```

Use an isolated domain and resolve the installed executables. Direct execution
makes each saved background PID the node's real PID.

```bash
export ROS_DOMAIN_ID=77
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
MONITOR_EXE="$(ros2 pkg prefix runtime_monitor)/lib/runtime_monitor/runtime_monitor_node"
BRIDGE_EXE="$(ros2 pkg prefix device_bridge)/lib/device_bridge/device_bridge_node"
EXECUTOR_EXE="$(ros2 pkg prefix task_executor)/lib/task_executor/task_executor_node"
VIRTUAL_EXE="$(ros2 pkg prefix virtual_can_device)/lib/virtual_can_device/virtual_can_device_node"
ADAPTER_EXE="$(ros2 pkg prefix ai_task_adapter)/lib/ai_task_adapter/ai_task_adapter_node"
```

In a new diagnostics observer terminal, run this complete block:

```bash
unset CONDA_EXE CONDA_PREFIX CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset CONDA_PYTHON_EXE CONDA_DEFAULT_ENV PYTHONHOME PYTHONPATH
export PATH=/usr/bin:/bin:$PATH
set +u
source /opt/ros/jazzy/setup.bash
source "${HOME}/robot-runtime-ws/install/setup.bash"
set -u
export ROS_DOMAIN_ID=77 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
timeout 120s ros2 topic echo \
  /diagnostics diagnostic_msgs/msg/DiagnosticArray
```

In a separate new Ready observer terminal, run this complete block:

```bash
unset CONDA_EXE CONDA_PREFIX CONDA_PROMPT_MODIFIER CONDA_SHLVL
unset CONDA_PYTHON_EXE CONDA_DEFAULT_ENV PYTHONHOME PYTHONPATH
export PATH=/usr/bin:/bin:$PATH
set +u
source /opt/ros/jazzy/setup.bash
source "${HOME}/robot-runtime-ws/install/setup.bash"
set -u
export ROS_DOMAIN_ID=77 ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
timeout 120s ros2 topic echo /runtime/ready std_msgs/msg/Bool
```

The nodes publish `diagnostic_msgs/msg/DiagnosticArray` directly because
`diagnostic_updater` is not a dependency in this workspace. Direct publication
keeps the interface standard without adding that unavailable helper library.
Each array contains the status from its publisher, so a raw `/diagnostics`
observer sees interleaved Bridge, Executor, and System arrays.

## Scenario 1: missing sources and core startup

Start only Runtime Monitor and observe `runtime/system` at `STALE` with Ready
false:

```bash
"$MONITOR_EXE" --ros-args \
  --params-file src/runtime_monitor/config/runtime_monitor.yaml &
MONITOR_PID=$!
timeout 5s ros2 topic echo /runtime/ready std_msgs/msg/Bool --once
```

Then start the two core sources. These parameters are also used by scenarios
2-6, so Bridge and Executor can remain alive through scenario 5.

```bash
"$BRIDGE_EXE" --ros-args \
  --params-file src/device_bridge/config/device_bridge.yaml &
BRIDGE_PID=$!
"$EXECUTOR_EXE" --ros-args \
  --params-file src/task_executor/config/targets.yaml \
  -p ack_timeout_ms:=200 -p validation_delay_ms:=50 &
EXECUTOR_PID=$!
sleep 3
timeout 5s ros2 topic echo /runtime/ready std_msgs/msg/Bool --once
```

Expected: both component diagnostics and `runtime/system` become `OK`; Ready is
true.

## Scenario 2: delayed ACK retry and recovery

```bash
"$VIRTUAL_EXE" --ros-args \
  -p interface_name:=vcan0 -p mode:=delay_ack -p delay_ms:=300 &
VIRTUAL_PID=$!
"$ADAPTER_EXE" --ros-args \
  -p request:="go to dock_a" -p task_id:=retry_demo \
  -p allowed_duration_ms:=3000 -p cancel_after_ms:=-1
echo "adapter rc=$?"
```

Expected: Bridge briefly reports `WARN/RETRYING`, `retry_count` increments, the
System status is `WARN`, and Ready stays true. The delayed ACK completes the
task, both components return to `OK`, and `retry_count` remains nonzero.

## Scenario 3: final ACK timeout and recovery

Replace only the virtual device; keep Monitor, Bridge, and Executor running:

```bash
kill -TERM "$VIRTUAL_PID"
wait "$VIRTUAL_PID"
"$VIRTUAL_EXE" --ros-args \
  -p interface_name:=vcan0 -p mode:=drop_ack -p delay_ms:=0 &
VIRTUAL_PID=$!
"$ADAPTER_EXE" --ros-args \
  -p request:="go to dock_a" -p task_id:=timeout_demo \
  -p allowed_duration_ms:=3000 -p cancel_after_ms:=-1
echo "adapter rc=$?"
```

Expected: the Adapter exits 4 with `SAFE_STOP/201`; Bridge and Executor become
`ERROR`, `ack_timeout_count` increments, and aggregate Ready becomes false.

Recover with a normal device and a new task:

```bash
kill -TERM "$VIRTUAL_PID"
wait "$VIRTUAL_PID"
"$VIRTUAL_EXE" --ros-args \
  -p interface_name:=vcan0 -p mode:=normal -p delay_ms:=0 &
VIRTUAL_PID=$!
"$ADAPTER_EXE" --ros-args \
  -p request:="go to dock_a" -p task_id:=timeout_recovery \
  -p allowed_duration_ms:=3000 -p cancel_after_ms:=-1
```

Expected: the new task succeeds, health returns to `OK`, Ready becomes true,
and `ack_timeout_count` is retained.

## Scenario 4: device fault and recovery

```bash
kill -TERM "$VIRTUAL_PID"
wait "$VIRTUAL_PID"
"$VIRTUAL_EXE" --ros-args \
  -p interface_name:=vcan0 -p mode:=fault -p delay_ms:=0 &
VIRTUAL_PID=$!
"$ADAPTER_EXE" --ros-args \
  -p request:="go to dock_a" -p task_id:=fault_demo \
  -p allowed_duration_ms:=3000 -p cancel_after_ms:=-1
echo "adapter rc=$?"
```

Expected: the Adapter exits 4 with `DEVICE_FAULT/302`, both components are
`ERROR`, `device_fault_count` increments, and Ready is false. Repeat the normal
device and recovery-task commands from scenario 3 with a new task ID. Expected:
health returns to `OK`, Ready becomes true, and `device_fault_count` remains.

## Scenario 5: STOP acknowledgement failure and recovery

Executor is already running with `validation_delay_ms=50`; cancel the parent
task after 100 ms:

```bash
kill -TERM "$VIRTUAL_PID"
wait "$VIRTUAL_PID"
"$VIRTUAL_EXE" --ros-args \
  -p interface_name:=vcan0 -p mode:=drop_stop_ack -p delay_ms:=0 &
VIRTUAL_PID=$!
"$ADAPTER_EXE" --ros-args \
  -p request:="go to dock_a" -p task_id:=stop_failure_demo \
  -p allowed_duration_ms:=3000 -p cancel_after_ms:=100
echo "adapter rc=$?"
```

Expected: the Adapter exits 4 with `SAFE_STOP/204`, `stop_failure_count`
increments, both components become `ERROR`, and Ready is false. Repeat the
normal device and recovery-task commands from scenario 3. Expected: health
returns to `OK`, Ready becomes true, and `stop_failure_count` remains.

## Scenario 6: stale Executor and recovery

Keep Bridge and Monitor alive, stop Executor for more than the 2-second
freshness threshold, then restart it:

```bash
kill -TERM "$EXECUTOR_PID"
wait "$EXECUTOR_PID"
sleep 3
timeout 5s ros2 topic echo /runtime/ready std_msgs/msg/Bool --once

"$EXECUTOR_EXE" --ros-args \
  --params-file src/task_executor/config/targets.yaml \
  -p ack_timeout_ms:=200 -p validation_delay_ms:=50 &
EXECUTOR_PID=$!
sleep 3
timeout 5s ros2 topic echo /runtime/ready std_msgs/msg/Bool --once
```

Expected: `runtime/system` first becomes `STALE` with
`task_executor_level=3` and Ready false. After the restarted Executor publishes
its next diagnostic, System returns to `OK` and Ready true. Executor-local
diagnostic history resets because this scenario deliberately restarts that
process; Bridge counters remain because Bridge was not restarted.

## Cleanup and scope

Stop only the PIDs started by the commands above:

```bash
kill -TERM "$VIRTUAL_PID" "$EXECUTOR_PID" "$BRIDGE_PID" "$MONITOR_PID"
wait "$VIRTUAL_PID" "$EXECUTOR_PID" "$BRIDGE_PID" "$MONITOR_PID"
pgrep -af 'runtime_monitor_node|device_bridge_node|task_executor_node|virtual_can_device_node'
```

This runbook covers local standard diagnostics, freshness, recovery, and
aggregate readiness. It does not provide or claim a dashboard, Prometheus or
Grafana integration, long-term metric storage, remote alerting, automatic
restart, or automatic fault handling.
