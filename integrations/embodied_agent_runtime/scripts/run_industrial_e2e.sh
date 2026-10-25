#!/usr/bin/env bash
set -Eeuo pipefail

WORKSPACE=${WORKSPACE:-${HOME}/robot-runtime-ws}
EVIDENCE=/tmp/runtime-industrial-evidence
ros_distro=${ROS_DISTRO:-jazzy}
ROS_DOMAIN_ID=${ROS_DOMAIN_ID:-127}
GATEWAY_PORT=${GATEWAY_PORT:-50170}
OVERALL_TIMEOUT_SECONDS=${OVERALL_TIMEOUT_SECONDS:-120}
export ROS_DOMAIN_ID ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
export RUNTIME_GATEWAY_ADDRESS="127.0.0.1:$GATEWAY_PORT"

unsafe_exit() {
  printf 'unsafe industrial E2E configuration: %s\n' "$*" >&2
  exit 64
}

[[ "$ROS_DOMAIN_ID" =~ ^[0-9]+$ ]] && ((ROS_DOMAIN_ID <= 232)) ||
  unsafe_exit "ROS_DOMAIN_ID must be in 0..232"
[[ "$GATEWAY_PORT" =~ ^[1-9][0-9]*$ ]] && ((GATEWAY_PORT <= 65535)) ||
  unsafe_exit "GATEWAY_PORT must be in 1..65535"
[[ "$OVERALL_TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] ||
  unsafe_exit "OVERALL_TIMEOUT_SECONDS must be positive"
[[ ${SETUP_VCAN:-0} == 0 || ${SETUP_VCAN:-0} == 1 ]] ||
  unsafe_exit "SETUP_VCAN must be 0 or 1"
[[ "$ros_distro" == jazzy || "$ros_distro" == humble ]] ||
  unsafe_exit "ROS_DISTRO must be jazzy or humble"
[[ "$EVIDENCE" == /tmp/runtime-industrial-evidence ]] || unsafe_exit "unexpected evidence path"
[[ ! -L "$EVIDENCE" ]] || unsafe_exit "$EVIDENCE must not be a symlink"

if [[ ${RUNTIME_INDUSTRIAL_E2E_INNER:-0} != 1 ]]; then
  export RUNTIME_INDUSTRIAL_E2E_INNER=1
  exec timeout --signal=TERM --kill-after=60s "${OVERALL_TIMEOUT_SECONDS}s" \
    /bin/bash "$(readlink -f "${BASH_SOURCE[0]}")" "$@"
fi

rm -rf "$EVIDENCE"
mkdir -p "$EVIDENCE"
printf 'scenario\tstatus\tdetail\n' > "$EVIDENCE/summary.tsv"
: > "$EVIDENCE/processes.tsv"
: > "$EVIDENCE/cleanup-waits.tsv"
: > "$EVIDENCE/cleanup-identity-failures.tsv"
: > "$EVIDENCE/assertions.log"
: > "$EVIDENCE/can.log"
printf 'scenario\tfirst_line\tlast_line\n' > "$EVIDENCE/can-windows.tsv"

declare -a PIDS=()
declare -A ACTIVE=()
declare -A LABEL=()
declare -A EXPECTED=()
declare -A CAN_START=()
declare -a NODE_EXECUTABLES=()
LAST_PID=""
VIRTUAL_PID=""
BRIDGE_PID=""
EXECUTOR_PID=""
EXECUTOR_LOG="$EVIDENCE/executor.log"
OBSERVER_PID=""

record() { printf '%s\n' "$*" | tee -a "$EVIDENCE/assertions.log"; }
fail() { record "FAIL: $*" >&2; return 1; }

process_is_running() {
  local pid=$1 state
  [[ -r "/proc/$pid/stat" ]] || return 1
  state=$(sed -n 's/^.*) \([^ ]\).*/\1/p' "/proc/$pid/stat" 2>/dev/null || true)
  [[ -n "$state" && "$state" != Z ]]
}

stop_pid() {
  local pid=$1 actual rc=0
  [[ ${ACTIVE[$pid]:-0} == 1 ]] || return 0
  if process_is_running "$pid"; then
    actual=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
    if [[ "$actual" != "${EXPECTED[$pid]}" ]]; then
      printf '%s\t%s\tTERM\t%s\t%s\n' \
        "${LABEL[$pid]}" "$pid" "${EXPECTED[$pid]}" "$actual" \
        >> "$EVIDENCE/cleanup-identity-failures.tsv"
      ACTIVE[$pid]=0
      return 1
    fi
    kill -TERM "$pid" 2>/dev/null || true
    local deadline=$((SECONDS + 3))
    while process_is_running "$pid" && ((SECONDS < deadline)); do sleep 0.05; done
    if process_is_running "$pid"; then
      actual=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
      if [[ "$actual" != "${EXPECTED[$pid]}" ]]; then
        printf '%s\t%s\tKILL\t%s\t%s\n' \
          "${LABEL[$pid]}" "$pid" "${EXPECTED[$pid]}" "$actual" \
          >> "$EVIDENCE/cleanup-identity-failures.tsv"
        ACTIVE[$pid]=0
        return 1
      fi
      kill -KILL "$pid" 2>/dev/null || true
    fi
  fi
  wait "$pid" 2>/dev/null || rc=$?
  printf '%s\t%s\t%s\n' "${LABEL[$pid]}" "$pid" "$rc" >> "$EVIDENCE/cleanup-waits.tsv"
  ACTIVE[$pid]=0
}

scan_installed() {
  local destination=$1 proc actual expected pid
  : > "$destination"
  for proc in /proc/[0-9]*/exe; do
    [[ -e "$proc" ]] || continue
    actual=$(readlink -f "$proc" 2>/dev/null || true)
    for expected in "${NODE_EXECUTABLES[@]}"; do
      if [[ "$actual" == "$expected" ]]; then
        pid=${proc#/proc/}; pid=${pid%/exe}
        printf '%s\t%s\n' "$pid" "$actual" >> "$destination"
      fi
    done
  done
}

cleanup() {
  local original_rc=$? index pid
  trap - EXIT INT TERM
  set +e
  for ((index=${#PIDS[@]} - 1; index >= 0; --index)); do
    pid=${PIDS[$index]}
    stop_pid "$pid"
  done
  : > "$EVIDENCE/cleanup-pid-scan.txt"
  for pid in "${PIDS[@]}"; do
    process_is_running "$pid" && printf '%s\t%s\n' "$pid" "${LABEL[$pid]}" \
      >> "$EVIDENCE/cleanup-pid-scan.txt"
  done
  scan_installed "$EVIDENCE/cleanup-installed-path-scan.txt"
  [[ ! -s "$EVIDENCE/cleanup-identity-failures.tsv" ]] || original_rc=97
  [[ ! -s "$EVIDENCE/cleanup-pid-scan.txt" ]] || original_rc=98
  [[ ! -s "$EVIDENCE/cleanup-installed-path-scan.txt" ]] || original_rc=99
  printf '%s\n' "$original_rc" > "$EVIDENCE/run.rc"
  exit "$original_rc"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

clean_ros_environment() {
  unset CONDA_EXE CONDA_PREFIX CONDA_PROMPT_MODIFIER CONDA_SHLVL
  unset CONDA_PYTHON_EXE CONDA_DEFAULT_ENV PYTHONHOME PYTHONPATH
  unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH LD_LIBRARY_PATH
  unset ROS_DISTRO ROS_VERSION ROS_PYTHON_VERSION
  export PATH=/usr/sbin:/usr/bin:/bin
  set +u
  source "/opt/ros/$ros_distro/setup.bash"
  source "$WORKSPACE/install/setup.bash"
  set -u
}

clean_ros_environment

if ! ip link show vcan0 >/dev/null 2>&1 && [[ ${SETUP_VCAN:-0} == 1 ]]; then
  timeout --signal=TERM --kill-after=2s 20s \
    /bin/bash "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/setup_vcan.sh" vcan0 \
    > "$EVIDENCE/setup-vcan.log" 2>&1 || true
fi
if ! ip -details link show vcan0 > "$EVIDENCE/vcan0.txt" 2>&1; then
  printf 'NEEDS_CONTEXT: vcan0 is missing; run scripts/setup_vcan.sh vcan0\n' \
    | tee "$EVIDENCE/precondition.log" >&2
  exit 2
fi

MONITOR="$WORKSPACE/install/runtime_monitor/lib/runtime_monitor/runtime_monitor_node"
BRIDGE="$WORKSPACE/install/device_bridge/lib/device_bridge/device_bridge_node"
EXECUTOR="$WORKSPACE/install/task_executor/lib/task_executor/task_executor_node"
VIRTUAL="$WORKSPACE/install/virtual_can_device/lib/virtual_can_device/virtual_can_device_node"
HISTORY="$WORKSPACE/install/runtime_history/lib/runtime_history/runtime_history_node"
HISTORY_REPORT="$WORKSPACE/install/runtime_history/lib/runtime_history/runtime_history_report"
ORCHESTRATOR="$WORKSPACE/install/task_orchestrator/lib/task_orchestrator/task_orchestrator_node"
GATEWAY="$WORKSPACE/install/runtime_gateway/lib/runtime_gateway/runtime_gateway_node"
CLIENT="$WORKSPACE/install/runtime_gateway/lib/runtime_gateway/runtime_gateway_client"
NODE_EXECUTABLES=("$MONITOR" "$BRIDGE" "$EXECUTOR" "$VIRTUAL" "$HISTORY" "$ORCHESTRATOR" "$GATEWAY")
for executable in "${NODE_EXECUTABLES[@]}" "$HISTORY_REPORT" "$CLIENT" /usr/bin/candump /usr/bin/python3; do
  [[ -x "$executable" ]] || fail "missing executable: $executable"
done
scan_installed "$EVIDENCE/before-installed-path-scan.txt"
[[ ! -s "$EVIDENCE/before-installed-path-scan.txt" ]] ||
  fail "an installed runtime node is already running"

register_process() {
  local label=$1 pid=$2 executable=$3 logfile=$4 deadline actual expected command_line
  expected=$(readlink -f "$executable")
  PIDS+=("$pid"); ACTIVE[$pid]=1; LABEL[$pid]=$label; EXPECTED[$pid]=$expected; LAST_PID=$pid
  deadline=$((SECONDS + 3))
  actual=""
  while ((SECONDS < deadline)); do
    process_is_running "$pid" || fail "$label exited during startup; see $logfile"
    actual=$(readlink -f "/proc/$pid/exe" 2>/dev/null || true)
    [[ -n "$actual" ]] && break
    sleep 0.05
  done
  command_line=$(tr '\0' ' ' < "/proc/$pid/cmdline")
  printf '%s\t%s\t%s\t%s\t%s\n' "$label" "$pid" "$expected" "$actual" "$command_line" \
    >> "$EVIDENCE/processes.tsv"
  [[ "$actual" == "$expected" ]] || fail "$label PID $pid is not the direct executable"
}

start_direct() {
  local label=$1 logfile=$2 executable=$3
  shift 3
  "$executable" "$@" > "$logfile" 2>&1 &
  local pid=$!
  register_process "$label" "$pid" "$executable" "$logfile"
}

wait_log() {
  local file=$1 text=$2 timeout_seconds=$3 deadline
  deadline=$((SECONDS + timeout_seconds))
  while ((SECONDS < deadline)); do
    grep -Fq "$text" "$file" 2>/dev/null && return 0
    sleep 0.05
  done
  fail "deadline waiting for '$text' in $file"
}

start_virtual() {
  local scenario=$1 mode=$2
  [[ -z "$VIRTUAL_PID" ]] || stop_pid "$VIRTUAL_PID"
  mkdir -p "$EVIDENCE/$scenario"
  start_direct "virtual-$scenario" "$EVIDENCE/$scenario/virtual.log" "$VIRTUAL" \
    --ros-args -p interface_name:=vcan0 -p mode:="$mode" -p delay_ms:=0
  VIRTUAL_PID=$LAST_PID
  wait_log "$EVIDENCE/$scenario/virtual.log" "Ready on vcan0 mode=$mode" 3
}

begin_can_window() {
  local scenario=$1
  mkdir -p "$EVIDENCE/$scenario"
  CAN_START[$scenario]=$(($(wc -l < "$EVIDENCE/can.log") + 1))
}

finish_can_window() {
  local scenario=$1 minimum_lines=$2 first deadline last target
  first=${CAN_START[$scenario]}
  deadline=$((SECONDS + 3))
  target=$((first + minimum_lines - 1))
  while ((SECONDS < deadline)); do
    last=$(wc -l < "$EVIDENCE/can.log")
    ((last >= target)) && break
    sleep 0.05
  done
  last=$(wc -l < "$EVIDENCE/can.log")
  ((last >= target)) || fail "$scenario CAN window expected at least $minimum_lines frames"
  if ((last >= first)); then
    sed -n "${first},${last}p" "$EVIDENCE/can.log" > "$EVIDENCE/$scenario/can.log"
  else
    : > "$EVIDENCE/$scenario/can.log"
  fi
  printf '%s\t%s\t%s\n' "$scenario" "$first" "$last" >> "$EVIDENCE/can-windows.tsv"
}

cat > "$EVIDENCE/observer.py" <<'PY'
import json
import sys
import time

import rclpy
from action_msgs.msg import GoalStatusArray
from diagnostic_msgs.msg import DiagnosticArray
from rclpy.executors import ExternalShutdownException
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.qos import qos_profile_action_status_default
from robot_task_interfaces.msg import DeviceState, TaskEvent

rclpy.init()
node = rclpy.create_node("runtime_industrial_e2e_observer")
output = open(sys.argv[1], "a", buffering=1)

def scalar(value):
    return value[0] if isinstance(value, bytes) else int(value)

def emit(kind, values):
    values["kind"] = kind
    values["observed_at_ns"] = time.time_ns()
    output.write(json.dumps(values, sort_keys=True) + "\n")

node.create_subscription(
    TaskEvent, "/runtime/task_events",
    lambda event: emit("task_event", {
        "task_id": event.task_id, "target_id": event.target_id,
        "request_id": event.request_id, "workflow_id": event.workflow_id,
        "goal_id": event.goal_id, "stamp": {"sec": event.stamp.sec, "nanosec": event.stamp.nanosec},
        "action_status": scalar(event.action_status), "outcome": scalar(event.outcome),
        "error_code": int(event.error_code), "duration_ms": int(event.duration_ms),
        "message": event.message}),
    QoSProfile(depth=32, reliability=ReliabilityPolicy.RELIABLE))
node.create_subscription(
    DeviceState, "/device_state",
    lambda state: emit("device_state", {
        "device_id": state.device_id, "device_mode": scalar(state.mode),
        "fault_code": int(state.fault_code), "last_command_id": int(state.last_command_id)}), 10)

def diagnostics(message):
    for status in message.status:
        emit("diagnostic", {
            "name": status.name, "level": scalar(status.level), "message": status.message,
            "values": {item.key: item.value for item in status.values}})

node.create_subscription(DiagnosticArray, "/diagnostics", diagnostics, 20)

def workflow_statuses(message):
    for item in message.status_list:
        emit("workflow_goal_status", {
            "goal_uuid": bytes(item.goal_info.goal_id.uuid).hex(),
            "status": scalar(item.status)})

node.create_subscription(
    GoalStatusArray, "/execute_workflow/_action/status", workflow_statuses,
    qos_profile_action_status_default)
emit("observer", {"state": "ready"})
try:
    rclpy.spin(node)
except (ExternalShutdownException, KeyboardInterrupt):
    pass
finally:
    output.close()
    node.destroy_node()
    if rclpy.ok():
        rclpy.shutdown()
PY

/usr/bin/candump -L vcan0 > "$EVIDENCE/can.log" 2>&1 &
register_process candump $! /usr/bin/candump "$EVIDENCE/can.log"
/usr/bin/python3 -u "$EVIDENCE/observer.py" "$EVIDENCE/action-diagnostics.jsonl" \
  > "$EVIDENCE/observer.log" 2>&1 &
register_process observer $! /usr/bin/python3 "$EVIDENCE/observer.log"
OBSERVER_PID=$LAST_PID

DB="$EVIDENCE/runtime.sqlite3"
# Stage evidence for offline cross-layer diagnosis. Both nodes append to one
# file, so a single capture spans the whole task path.
STAGE_EVENTS="$EVIDENCE/stage_events.jsonl"
: > "$STAGE_EVENTS"
start_virtual normal normal
start_direct monitor "$EVIDENCE/monitor.log" "$MONITOR" --ros-args \
  --params-file "$WORKSPACE/src/runtime_monitor/config/runtime_monitor.yaml"
start_direct bridge "$EVIDENCE/bridge.log" "$BRIDGE" --ros-args \
  --params-file "$WORKSPACE/src/device_bridge/config/device_bridge.yaml" \
  -p stage_event_log_path:="$STAGE_EVENTS"
BRIDGE_PID=$LAST_PID
start_direct executor "$EVIDENCE/executor.log" "$EXECUTOR" --ros-args \
  --params-file "$WORKSPACE/src/task_executor/config/targets.yaml" \
  -p stage_event_log_path:="$STAGE_EVENTS"
EXECUTOR_PID=$LAST_PID
start_direct history "$EVIDENCE/history.log" "$HISTORY" --ros-args -p database_path:="$DB"
start_direct orchestrator "$EVIDENCE/orchestrator.log" "$ORCHESTRATOR"
start_direct gateway "$EVIDENCE/gateway.log" "$GATEWAY" --ros-args \
  -p port:="$GATEWAY_PORT" -p database_path:="$DB"
wait_log "$EVIDENCE/gateway.log" "gRPC listening on 127.0.0.1:$GATEWAY_PORT" 5

deadline=$((SECONDS + 8))
while ((SECONDS < deadline)); do
  "$CLIENT" get-stats > "$EVIDENCE/startup-stats.json" 2> "$EVIDENCE/startup-stats.stderr" && break
  sleep 0.1
done
[[ -s "$EVIDENCE/startup-stats.json" ]] || fail "gateway startup deadline exceeded"
deadline=$((SECONDS + 15))
while ((SECONDS < deadline)); do
  timeout 5 ros2 action list > "$EVIDENCE/actions.txt" 2> "$EVIDENCE/actions.stderr" || true
  if grep -Fxq '/execute_workflow' "$EVIDENCE/actions.txt" &&
    grep -Fxq '/execute_task' "$EVIDENCE/actions.txt" &&
    grep -Fxq '/execute_device_command' "$EVIDENCE/actions.txt"; then
    break
  fi
  sleep 0.1
done
grep -Fxq '/execute_workflow' "$EVIDENCE/actions.txt" || fail "ROS Action discovery deadline exceeded"
wait_log "$EVIDENCE/action-diagnostics.jsonl" '"name": "runtime/history"' 5

submit() {
  local scenario=$1 request_id=$2 task_id=$3
  "$CLIENT" submit --request-id "$request_id" --workflow single_task --task-id "$task_id" \
    --target dock_a --timeout-ms 3000 > "$EVIDENCE/$scenario/submit.json"
}

wait_task() {
  local scenario=$1 task_id=$2 expected_state=$3 deadline tmp
  deadline=$((SECONDS + 10))
  tmp="$EVIDENCE/$scenario/task.tmp"
  while ((SECONDS < deadline)); do
    if "$CLIENT" get-task --task-id "$task_id" > "$tmp" 2> "$EVIDENCE/$scenario/get-task.stderr" &&
      /usr/bin/python3 - "$tmp" "$expected_state" <<'PY'
import json, pathlib, sys
value = json.loads(pathlib.Path(sys.argv[1]).read_text())
raise SystemExit(0 if value.get("state") == sys.argv[2] and value.get("target_id") else 1)
PY
    then
      mv "$tmp" "$EVIDENCE/$scenario/task.json"
      "$HISTORY_REPORT" --db "$DB" --task "$task_id" > "$EVIDENCE/$scenario/sqlite.json"
      return 0
    fi
    sleep 0.1
  done
  fail "$scenario task did not reach $expected_state"
}

wait_executor_dispatch() {
  local task_id=$1 deadline
  deadline=$((SECONDS + 5))
  while ((SECONDS < deadline)); do
    grep -Fq "Dispatching task_id=$task_id " "$EXECUTOR_LOG" && return 0
    sleep 0.05
  done
  fail "ExecuteTask dispatch not observed for $task_id"
}

wait_runtime_ready_since() {
  local since_ns=$1 deadline
  deadline=$((SECONDS + 8))
  while ((SECONDS < deadline)); do
    if /usr/bin/python3 - "$EVIDENCE/action-diagnostics.jsonl" "$since_ns" <<'PY'
import json, pathlib, sys
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    event = json.loads(line)
    if all((event.get("observed_at_ns", 0) > int(sys.argv[2]),
            event.get("kind") == "diagnostic", event.get("name") == "runtime/system",
            event.get("values", {}).get("ready") == "true")):
        raise SystemExit(0)
raise SystemExit(1)
PY
    then
      return 0
    fi
    sleep 0.1
  done
  fail "runtime did not recover after fault restart"
}

workflow_goal_count() {
  /usr/bin/python3 - "$EVIDENCE/action-diagnostics.jsonl" <<'PY'
import json, pathlib, sys
goals = set()
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        continue
    if event.get("kind") == "workflow_goal_status":
        goals.add(event["goal_uuid"])
print(len(goals))
PY
}

wait_workflow_goal_count() {
  local expected=$1 deadline actual
  deadline=$((SECONDS + 5))
  while ((SECONDS < deadline)); do
    actual=$(workflow_goal_count)
    ((actual >= expected)) && return 0
    sleep 0.05
  done
  fail "workflow Goal status count expected at least $expected actual=$actual"
}

assert_workflow_goal_quiet_window() {
  local expected=$1 quiet_ms=$2 deadline quiet_deadline actual
  deadline=$((SECONDS + 5))
  while ((SECONDS < deadline)); do
    process_is_running "$OBSERVER_PID" || fail "observer exited before workflow Goal quiet window"
    actual=$(workflow_goal_count)
    ((actual <= expected)) || fail "accepted workflow Goal UUIDs expected=$expected actual=$actual"
    if ((actual == expected)); then
      quiet_deadline=$(($(date +%s%N) + quiet_ms * 1000000))
      while (( $(date +%s%N) < quiet_deadline )); do
        process_is_running "$OBSERVER_PID" || fail "observer exited during workflow Goal quiet window"
        actual=$(workflow_goal_count)
        ((actual == expected)) ||
          fail "accepted workflow Goal UUIDs changed during quiet window expected=$expected actual=$actual"
        sleep 0.05
      done
      printf '{"accepted_workflow_goal_count":%s,"quiet_window_ms":%s}\n' \
        "$expected" "$quiet_ms" > "$EVIDENCE/stats/workflow-goals.json"
      return 0
    fi
    sleep 0.05
  done
  fail "accepted workflow Goal UUIDs expected=$expected actual=$actual"
}

wait_runtime_ready_since 0

begin_can_window normal
task_id="industrial-normal-$$"
submit normal "request-normal-$$" "$task_id"
wait_task normal "$task_id" COMPLETED
finish_can_window normal 2

start_virtual fault302 fault
begin_can_window fault302
task_id="industrial-fault-$$"
submit fault302 "request-fault-$$" "$task_id"
wait_task fault302 "$task_id" DEVICE_FAULT
finish_can_window fault302 2

stop_pid "$EXECUTOR_PID"
stop_pid "$BRIDGE_PID"
recovery_started_ns=$(date +%s%N)
start_direct bridge-recovered "$EVIDENCE/bridge-recovered.log" "$BRIDGE" --ros-args \
  --params-file "$WORKSPACE/src/device_bridge/config/device_bridge.yaml" \
  -p stage_event_log_path:="$STAGE_EVENTS"
BRIDGE_PID=$LAST_PID
start_direct executor-recovered "$EVIDENCE/executor-recovered.log" "$EXECUTOR" --ros-args \
  --params-file "$WORKSPACE/src/task_executor/config/targets.yaml" \
  -p stage_event_log_path:="$STAGE_EVENTS"
EXECUTOR_PID=$LAST_PID
EXECUTOR_LOG="$EVIDENCE/executor-recovered.log"
wait_runtime_ready_since "$recovery_started_ns"

start_virtual cancel drop_ack
begin_can_window cancel
task_id="industrial-cancel-$$"
submit cancel "request-cancel-$$" "$task_id"
wait_executor_dispatch "$task_id"
"$CLIENT" cancel --request-id "request-cancel-$$" > "$EVIDENCE/cancel/cancel.json"
wait_task cancel "$task_id" CANCELED
finish_can_window cancel 3

start_virtual duplicate normal
begin_can_window duplicate
task_id="industrial-duplicate-$$"
workflow_goals_before=$(workflow_goal_count)
before_goals=$(grep -Fc "Accepting task_id=$task_id target_id=dock_a" "$EXECUTOR_LOG" || true)
dispatch_marker="Dispatching ExecuteWorkflow Goal request_id=request-duplicate-$$ task_id=$task_id"
gateway_dispatches_before=$(grep -Fc "$dispatch_marker" "$EVIDENCE/gateway.log" || true)
submit duplicate "request-duplicate-$$" "$task_id"
cp "$EVIDENCE/duplicate/submit.json" "$EVIDENCE/duplicate/submit-first.json"
submit duplicate "request-duplicate-$$" "$task_id"
cp "$EVIDENCE/duplicate/submit.json" "$EVIDENCE/duplicate/submit-second.json"
wait_task duplicate "$task_id" COMPLETED
finish_can_window duplicate 2
wait_workflow_goal_count "$((workflow_goals_before + 1))"
workflow_goals_after=$(workflow_goal_count)
after_goals=$(grep -Fc "Accepting task_id=$task_id target_id=dock_a" "$EXECUTOR_LOG" || true)
gateway_dispatches_after=$(grep -Fc "$dispatch_marker" "$EVIDENCE/gateway.log" || true)
/usr/bin/python3 - "$DB" "$task_id" "$((workflow_goals_after - workflow_goals_before))" \
  "$((after_goals - before_goals))" "$((gateway_dispatches_after - gateway_dispatches_before))" \
  > "$EVIDENCE/duplicate/assertions.json" <<'PY'
import json, sqlite3, sys
with sqlite3.connect(sys.argv[1]) as database:
    records = database.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (sys.argv[2],)).fetchone()[0]
print(json.dumps({"workflow_goal_count": int(sys.argv[3]),
                  "execute_task_goal_count": int(sys.argv[4]),
                  "gateway_dispatch_count": int(sys.argv[5]),
                  "record_count": records}, sort_keys=True))
PY

start_virtual ack_timeout drop_ack
begin_can_window ack_timeout
task_id="industrial-ack-timeout-$$"
submit ack_timeout "request-ack-timeout-$$" "$task_id"
wait_task ack_timeout "$task_id" SAFE_STOP
finish_can_window ack_timeout 4

stop_pid "$EXECUTOR_PID"
stop_pid "$BRIDGE_PID"
recovery_started_ns=$(date +%s%N)
start_direct bridge-after-ack-timeout "$EVIDENCE/bridge-after-ack-timeout.log" "$BRIDGE" \
  --ros-args --params-file "$WORKSPACE/src/device_bridge/config/device_bridge.yaml"
BRIDGE_PID=$LAST_PID
start_direct executor-after-ack-timeout "$EVIDENCE/executor-after-ack-timeout.log" "$EXECUTOR" \
  --ros-args --params-file "$WORKSPACE/src/task_executor/config/targets.yaml"
EXECUTOR_PID=$LAST_PID
EXECUTOR_LOG="$EVIDENCE/executor-after-ack-timeout.log"
wait_runtime_ready_since "$recovery_started_ns"

start_virtual drop_stop_ack drop_stop_ack
begin_can_window drop_stop_ack
task_id="industrial-safe-stop-$$"
submit drop_stop_ack "request-safe-stop-$$" "$task_id"
wait_executor_dispatch "$task_id"
"$CLIENT" cancel --request-id "request-safe-stop-$$" > "$EVIDENCE/drop_stop_ack/cancel.json"
wait_task drop_stop_ack "$task_id" SAFE_STOP
finish_can_window drop_stop_ack 2

mkdir -p "$EVIDENCE/stats"
begin_can_window stats
"$CLIENT" get-stats > "$EVIDENCE/stats/grpc.json"
"$HISTORY_REPORT" --db "$DB" --stats > "$EVIDENCE/stats/sqlite.json"
finish_can_window stats 0
assert_workflow_goal_quiet_window 6 1000

require_file() { [[ -s "$1" ]] || fail "missing orchestration evidence: $1"; }
assert_task() {
  local scenario=$1 state=$2 outcome=$3 error_code=$4
  require_file "$EVIDENCE/$scenario/task.json"
  /usr/bin/python3 - "$EVIDENCE/$scenario/task.json" "$EVIDENCE/$scenario/sqlite.json" \
    "$state" "$outcome" "$error_code" <<'PY'
import json, pathlib, sys
grpc = json.loads(pathlib.Path(sys.argv[1]).read_text())
sqlite = json.loads(pathlib.Path(sys.argv[2]).read_text())
assert grpc["found"] is True and sqlite["found"] is True, (grpc, sqlite)
assert grpc["state"] == sys.argv[3], grpc
for field, expected in (("outcome", int(sys.argv[4])), ("error_code", int(sys.argv[5]))):
    assert grpc[field] == expected and sqlite[field] == expected, (grpc, sqlite)
assert grpc["task_id"] == sqlite["task_id"], (grpc, sqlite)
PY
}
assert_event() {
  local scenario=$1 outcome=$2 error_code=$3
  local task_id
  task_id=$(/usr/bin/python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["task_id"])' \
    "$EVIDENCE/$scenario/task.json")
  /usr/bin/python3 - "$EVIDENCE/action-diagnostics.jsonl" "$task_id" "$outcome" "$error_code" <<'PY'
import json, pathlib, sys
events = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
assert any(event.get("kind") == "task_event" and event.get("task_id") == sys.argv[2]
           and event.get("outcome") == int(sys.argv[3])
           and event.get("error_code") == int(sys.argv[4]) for event in events), sys.argv[2:]
PY
}
assert_stop_can() {
  local scenario=$1 expect_response=$2
  require_file "$EVIDENCE/$scenario/can.log"
  /usr/bin/python3 - "$EVIDENCE/$scenario/can.log" "$expect_response" <<'PY'
import pathlib, re, sys

frames = []
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    match = re.search(r"\s([0-9A-Fa-f]+)#([0-9A-Fa-f]{16})$", line)
    assert match, line
    frames.append((int(match.group(1), 16), bytes.fromhex(match.group(2))))
stops = [data for frame_id, data in frames if frame_id == 0x100 and data[3] == 0xFF]
assert len(stops) == 1, frames
stop_id = int.from_bytes(stops[0][1:3], "big")
responses = [data for frame_id, data in frames
             if frame_id == 0x101 and int.from_bytes(data[1:3], "big") == stop_id]
if sys.argv[2] == "present":
    assert len(responses) == 1 and responses[0][4] == 3, responses
else:
    assert not responses, responses
PY
}

assert_task normal COMPLETED 0 0; assert_event normal 0 0
printf 'normal\tPASS\tCOMPLETED/0\n' >> "$EVIDENCE/summary.tsv"
assert_task fault302 DEVICE_FAULT 3 302; assert_event fault302 3 302
printf 'fault302\tPASS\tDEVICE_FAULT/302\n' >> "$EVIDENCE/summary.tsv"
assert_task cancel CANCELED 1 0; assert_event cancel 1 0
assert_stop_can cancel present
grep -Fq 'STOP acknowledged' "$EVIDENCE/cancel/virtual.log" || fail "cancel did not observe STOPPED"
printf 'cancel\tPASS\tCANCELED/0 STOPPED\n' >> "$EVIDENCE/summary.tsv"
assert_task drop_stop_ack SAFE_STOP 2 204; assert_event drop_stop_ack 2 204
assert_stop_can drop_stop_ack absent
grep -Fq 'Dropping STOP ACK' "$EVIDENCE/drop_stop_ack/virtual.log" ||
  fail "drop_stop_ack did not drop STOP response"
! grep -Fq 'STOP acknowledged' "$EVIDENCE/drop_stop_ack/virtual.log" ||
  fail "drop_stop_ack unexpectedly received STOP response"
printf 'drop_stop_ack\tPASS\tSAFE_STOP/204 no STOP response\n' >> "$EVIDENCE/summary.tsv"
assert_task ack_timeout SAFE_STOP 2 201; assert_event ack_timeout 2 201
assert_stop_can ack_timeout present
grep -Fq 'STOP acknowledged' "$EVIDENCE/ack_timeout/virtual.log" ||
  fail "ACK timeout did not observe STOPPED"
printf 'ack_timeout\tPASS\tSAFE_STOP/201 STOP acknowledged\n' >> "$EVIDENCE/summary.tsv"
/usr/bin/python3 - "$EVIDENCE/duplicate/assertions.json" <<'PY'
import json, pathlib, sys
assert json.loads(pathlib.Path(sys.argv[1]).read_text()) == {
    "workflow_goal_count": 1, "execute_task_goal_count": 1,
    "gateway_dispatch_count": 1, "record_count": 1}
PY
[[ $(grep -Fc "$dispatch_marker" "$EVIDENCE/gateway.log") == 1 ]] ||
  fail "duplicate Gateway dispatch log count is not one"
printf 'duplicate\tPASS\tone Gateway dispatch one workflow Goal one ExecuteTask Goal one record\n' \
  >> "$EVIDENCE/summary.tsv"
/usr/bin/python3 - "$EVIDENCE/stats/grpc.json" "$EVIDENCE/stats/sqlite.json" <<'PY'
import json, pathlib, sys
grpc = json.loads(pathlib.Path(sys.argv[1]).read_text())
sqlite = json.loads(pathlib.Path(sys.argv[2]).read_text())
fields = ("has_data", "sample_count", "outcome_counts", "p50_ms", "p95_ms", "p99_ms", "max_ms")
assert all(grpc[field] == sqlite[field] for field in fields), (grpc, sqlite)
assert grpc["has_data"] is True and grpc["sample_count"] == 6, grpc
assert grpc["outcome_counts"] == [2, 1, 2, 1], grpc
assert all(isinstance(grpc[field], int) and grpc[field] > 0 for field in fields[3:]), grpc
PY
printf 'stats\tPASS\t6 samples [2,1,2,1] percentiles agree\n' >> "$EVIDENCE/summary.tsv"

grep -Fq '"kind": "diagnostic"' "$EVIDENCE/action-diagnostics.jsonl" ||
  fail "no diagnostics evidence observed"
[[ -s "$EVIDENCE/can.log" ]] || fail "no CAN frames observed"
process_is_running "$OBSERVER_PID" || fail "observer exited before final assertions"
! grep -Fq 'Traceback' "$EVIDENCE/observer.log" || fail "observer traceback observed"
/usr/bin/python3 - "$EVIDENCE/stats/workflow-goals.json" <<'PY'
import json, pathlib, sys
assert json.loads(pathlib.Path(sys.argv[1]).read_text()) == {
    "accepted_workflow_goal_count": 6, "quiet_window_ms": 1000}
PY
record "PASS: seven industrial E2E scenarios"
