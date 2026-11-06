# Runtime Monitor 运维手册

[English](README.md) | **简体中文**

`runtime_monitor` 汇总 Device Bridge 和 Task Executor 的健康状态，发布系统诊断和
聚合 readiness 位。监控链路只负责观测：不会发送 Action goal、访问 SocketCAN、
停止设备、重启进程或修改被监控节点状态。

## 架构与接口

```text
虚拟 CAN 设备 <-> vcan0 <-> Device Bridge <-> Task Executor <-> AI adapter
                                  |                  |
                                  +-- /diagnostics --+
                                           |
                                   Runtime Monitor
                                      |         |
                              /diagnostics  /runtime/ready
```

稳定诊断名称为 `runtime/device_bridge`、`runtime/task_executor` 和
`runtime/system`。标准诊断等级为 `OK=0`、`WARN=1`、`ERROR=2`、`STALE=3`。
默认每 500 ms 发布一次，核心诊断超过 2000 ms 视为过期。

只有两个核心来源均已出现、数据新鲜且均不是 `ERROR/STALE` 时，聚合 Ready 才为
true。`WARN` 表示降级但仍可用，因此 Ready 保持 true。后续成功任务可清除当前错误；
Bridge 累计计数器会保留到进程重启，但本包不跨重启持久化这些计数。

## 构建与公共准备

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

按需创建 `vcan0`：

```bash
sudo ip link add dev vcan0 type vcan
sudo ip link set vcan0 up
ip -brief link show vcan0
```

使用隔离 ROS domain，并解析安装后的可执行文件：

```bash
export ROS_DOMAIN_ID=77
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST
MONITOR_EXE="$(ros2 pkg prefix runtime_monitor)/lib/runtime_monitor/runtime_monitor_node"
BRIDGE_EXE="$(ros2 pkg prefix device_bridge)/lib/device_bridge/device_bridge_node"
EXECUTOR_EXE="$(ros2 pkg prefix task_executor)/lib/task_executor/task_executor_node"
VIRTUAL_EXE="$(ros2 pkg prefix virtual_can_device)/lib/virtual_can_device/virtual_can_device_node"
ADAPTER_EXE="$(ros2 pkg prefix ai_task_adapter)/lib/ai_task_adapter/ai_task_adapter_node"
```

分别观察 `/diagnostics` 和 `/runtime/ready`：

```bash
timeout 120s ros2 topic echo /diagnostics diagnostic_msgs/msg/DiagnosticArray
timeout 120s ros2 topic echo /runtime/ready std_msgs/msg/Bool
```

## 场景 1：来源缺失与核心启动

先只启动 Monitor，预期 `runtime/system=STALE` 且 Ready=false；再启动 Bridge 和
Executor，等待 3 秒后，两个组件和系统都应为 `OK`，Ready=true。

```bash
"$MONITOR_EXE" --ros-args --params-file src/runtime_monitor/config/runtime_monitor.yaml &
MONITOR_PID=$!
"$BRIDGE_EXE" --ros-args --params-file src/device_bridge/config/device_bridge.yaml &
BRIDGE_PID=$!
"$EXECUTOR_EXE" --ros-args --params-file src/task_executor/config/targets.yaml \
  -p ack_timeout_ms:=200 -p validation_delay_ms:=50 &
EXECUTOR_PID=$!
```

## 场景 2：ACK 延迟重试与恢复

以 `delay_ack/300 ms` 启动虚拟设备并提交任务。Bridge 应短暂报告
`WARN/RETRYING`，`retry_count` 增加，系统为 WARN 但 Ready 保持 true；ACK 到达后
任务成功并恢复为 OK。

```bash
"$VIRTUAL_EXE" --ros-args -p interface_name:=vcan0 -p mode:=delay_ack -p delay_ms:=300 &
VIRTUAL_PID=$!
"$ADAPTER_EXE" --ros-args -p request:="go to dock_a" -p task_id:=retry_demo \
  -p allowed_duration_ms:=3000 -p cancel_after_ms:=-1
```

## 场景 3：最终 ACK 超时与恢复

将虚拟设备替换为 `drop_ack`。预期 Adapter 以 `SAFE_STOP/201` 退出，Bridge 和
Executor 变为 ERROR，`ack_timeout_count` 增加，Ready=false。随后换回 `normal`
设备并提交新任务，健康恢复为 OK、Ready=true，累计计数保留。

## 场景 4：设备故障与恢复

使用 `mode:=fault`。预期 `DEVICE_FAULT/302`、两个组件 ERROR、
`device_fault_count` 增加且 Ready=false。换回正常设备并提交新任务后恢复。

## 场景 5：STOP ACK 失败与恢复

使用 `mode:=drop_stop_ack`，并令父任务在 100 ms 后取消。预期
`SAFE_STOP/204`、`stop_failure_count` 增加、组件 ERROR 且 Ready=false。换回正常
设备并提交新任务后恢复。

## 场景 6：Executor 过期与恢复

保留 Bridge 和 Monitor，停止 Executor 超过 2 秒。系统应变为 STALE，
`task_executor_level=3` 且 Ready=false。重启 Executor 并等待下一次诊断后，系统
恢复为 OK、Ready=true；Executor 本地历史因重启而清零，Bridge 计数继续保留。

## 清理与范围

只终止本手册启动的 PID：

```bash
kill -TERM "$VIRTUAL_PID" "$EXECUTOR_PID" "$BRIDGE_PID" "$MONITOR_PID"
wait "$VIRTUAL_PID" "$EXECUTOR_PID" "$BRIDGE_PID" "$MONITOR_PID"
pgrep -af 'runtime_monitor_node|device_bridge_node|task_executor_node|virtual_can_device_node'
```

本手册覆盖本地标准诊断、新鲜度、恢复和聚合 readiness，不提供 Dashboard、
Prometheus/Grafana、长期指标存储、远程告警、自动重启或自动故障处理。
