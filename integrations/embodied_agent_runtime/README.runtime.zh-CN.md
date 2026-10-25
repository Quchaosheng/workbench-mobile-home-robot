# Embodied Agent Runtime

[English](README.runtime.md) | **简体中文**

[![ROS 2 Runtime CI](https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/workflows/ros2-runtime.yml/badge.svg)](https://github.com/Quchaosheng/workbench-mobile-home-robot/actions/workflows/ros2-runtime.yml)
[![ROS 2](https://img.shields.io/badge/ROS%202-Jazzy%20(CI)%20%7C%20Humble%20(X5)-22314E?logo=ros)](https://docs.ros.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-2EA44F)](../../LICENSE)

这是一个执行路径固定、可审查的 ROS 2 任务运行时。它将受控工作流请求交由
BehaviorTree.CPP 编排，经由嵌套 ROS 2 Action 和 SocketCAN 设备控制执行任务，
并记录运行时诊断与 SQLite 任务历史。

软件链路已经实现并通过测试。X5 原生 ARM64 运行、实体 USB 摄像头、ArUco 检测和
双 CANable 双向 SocketCAN 台架通信也已验证。电机运动和硬件急停回路不在当前证据
范围内，不能把台架通信或软件 `SAFE_STOP` 写成完整机器人闭环。

## 运行架构

```mermaid
flowchart LR
    G["gRPC Gateway"] --> W["ExecuteWorkflow"]
    A["规则式/模型文本适配器"] --> W
    P["ArUco 图像/相机适配器"] --> W
    W --> B["固定 BehaviorTree.CPP"]
    B --> T["ExecuteTask"]
    T --> V["目标校验 + 截止时间"]
    V --> D["ExecuteDeviceCommand"]
    D --> R["Device Bridge"]
    R --> C["SocketCAN"]
    C --> E["真实或虚拟设备"]
    T --> X["TaskEvent"]
    X --> H["SQLite 历史"]
    R -. 诊断 .-> M["Runtime Monitor"]
    T -. 诊断 .-> M
```

模型和感知适配器不能直接控制设备，只能通过 `ExecuteWorkflow` 提交白名单工作流；
实际执行流保持固定、可审查。

## 已实现组件

| 包 | 职责 |
| --- | --- |
| `robot_task_interfaces` | ROS 2 Action、消息和服务契约 |
| `runtime_can` | 固定经典 CAN 协议编解码与校验 |
| `virtual_can_device` | 正常、故障、延迟和丢 ACK 测试用软件 ECU |
| `device_bridge` | SocketCAN 命令、ACK 重试、STOP、取消和诊断 |
| `task_executor` | 目标白名单、截止时间预算、嵌套 Action 和 `TaskEvent` |
| `runtime_monitor` | 聚合 readiness 及降级/错误诊断 |
| `runtime_history` | SQLite 任务持久化、查询和百分位统计 |
| `task_orchestrator` | 固定 BehaviorTree.CPP 工作流和有界子任务取消 |
| `runtime_gateway` | Loopback gRPC、请求身份、去重和 Action 桥接 |
| `ai_task_adapter` | 确定性规则规划器和可选 OpenAI 兼容模型适配器 |
| `perception_task_adapter` | 可选 ArUco 图像或 USB 相机触发器 |

### Action 分层与 TaskEvent

运行时使用三层职责分离的 ROS 2 Action：

1. `ExecuteWorkflow` 接收白名单工作流请求，并运行固定的 BehaviorTree.CPP 编排。
2. `ExecuteTask` 校验目标、施加任务级截止时间预算，并协调设备命令子 Action。
3. `ExecuteDeviceCommand` 负责 SocketCAN 命令传输、ACK 处理、重试、取消和协议
   STOP 行为。

`TaskEvent` 是 `task_executor` 发布的终态任务结果消息，记录任务 ID、目标、Action
终态、运行结果、错误码、消息、事件时间戳和时长，供历史与统计使用。它还携带任务的
关联标识：`request_id`、`workflow_id`，以及被接受的 `ExecuteTask` goal 的 ROS 2
`goal_id`；调用方未提供时为空。这些字段是标识符而非执行证据，该消息也不是另一条
命令通道，不证明物理执行器已经运动或停止。

取消与 STOP 的证据路径见
[`docs/cancel-stop-sequence.md`](docs/cancel-stop-sequence.md)。软件取消是一种意图，
运行时只有在匹配到 STOP 命令 ID 之后才记录设备侧 STOP 响应；该协议证据同样不
证明物理运动或停止距离。

## 已验证的软件证据

2026-07-29，本机在 Ubuntu 24.04 / ROS 2 Jazzy 中完成 WSL2 隔离构建和测试，覆盖
11 个包。仓库中已提交的测试源码当前定义了 **123 个 GoogleTest 用例和 27 个 pytest
用例**（2026-07-29 当次记录的代码树为 119 和 18）；这是行为测试用例数，不是通过数，
也不是 `colcon test-result` 的记录数。
后者还包含测试运行器和 `ament_lint` 条目，不能把聚合数写成行为用例数。GitHub
Actions 也通过 Windows 工具检查，以及 Ubuntu 24.04/Jazzy 构建、测试、ARM64 配置
和条件式 `vcan0` 工作流。

当前 11 包代码也已在 Ubuntu 22.04 / ROS 2 Humble 的 X5 上原生构建。顺序执行的
ARM smoke 未报告测试失败；本文不把该次 smoke 的聚合记录当作行为测试用例数。
Humble smoke 只排除发行版自带的 `uncrustify` 0.72，因为它与 CI 使用的 Jazzy 标准
格式器输出不一致；代码风格仍由 Jazzy CI 强制检查。X5 还验证了模型适配器默认禁用
会拒绝运行，并从本地假接口严格解析出 `single_task/dock_a/1000 ms`；随后在故意
离线的 `ExecuteWorkflow` 处停止，没有触碰 CAN。

2026-07-28，实体 UVC 摄像头通过 `/dev/video0` 连续 30/30 帧识别出
`DICT_4X4_50` 的 ID 10 标记。两只 CANable2 也被识别为 `can1`、`can2`，并通过
`cansend`/`candump` 完成双向经典 CAN 帧收发，接口错误计数为 0。这是收发器台架
证据，不是电机或机器人闭环证据。

工业 `vcan0` E2E 覆盖正常、设备故障、取消、STOP ACK 丢失、普通 ACK 超时、
重复请求去重和 SQLite 统计七个场景：

```bash
WORKSPACE="$PWD/ros2_ws" SETUP_VCAN=0 bash scripts/run_industrial_e2e.sh
```

`vcan0`、虚拟设备、生成图像和 Fake Action 服务器都是测试替身，不是物理硬件证据。

## 快速开始

### Windows + WSL2

在 Windows PowerShell 的仓库根目录运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\windows_wsl.ps1 -Mode Check

powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\windows_wsl.ps1 -Mode BuildTest
```

默认发行版是 `Ubuntu-24.04`。脚本不会调用 `sudo`，构建产物保存在 WSL 原生的
`$HOME/.cache/embodied-agent-runtime-wsl`。使用 `-DryRun` 可预览行为。

### Ubuntu 24.04 / ROS 2 Jazzy

Ubuntu 24.04 / ROS 2 Jazzy 是 x86_64、WSL2、CI 和 Docker 的主要基线。下文的 X5
证据是在 Ubuntu 22.04 / ROS 2 Humble 上单独采集的；不表示 X5 已在 Jazzy 上验证，
两个发行版也不能混用在同一环境中。

```bash
set +u
source /opt/ros/jazzy/setup.bash
set -u
cd ros2_ws
rosdep install --from-paths src --ignore-src --rosdistro jazzy -y
colcon build --cmake-args -DBUILD_TESTING=ON
colcon test --return-code-on-test-failure
colcon test-result --test-result-base build --verbose
```

### Docker 软件 E2E

在装有 Docker Engine 和 GNU Make 的 Linux 主机上，于仓库根目录构建并运行软件 E2E：

```bash
git clone https://github.com/Quchaosheng/embodied-agent-runtime.git
cd embodied-agent-runtime
make docker-build
make demo
```

`make image` 是 `make docker-build` 的别名。镜像基于 `ros:jazzy-ros-base`，按仓库中
已提交的 package manifest 执行 `rosdep`，并用 `colcon` 构建 `ros2_ws`。`make demo`
在 Docker 内以 host 网络和 `--privileged` 运行 `scripts/run_industrial_e2e.sh`；这些
仅限 Linux 的权限用于创建并使用 `vcan0`（`SETUP_VCAN=1`）。`make demo-local` 是容器内
入口，不应替代 Docker 外层调用。

这是使用 `vcan0` 和仓库虚拟 CAN 设备的软件/虚拟 CAN E2E。通过运行不等于 X5 实机运行、
相机、物理 CAN 适配器、真实执行器或硬件急停行为。Windows 或 macOS 上的 Docker Desktop
不是该 vcan 演示的受支持宿主，请使用 Linux Docker 主机。

### 原生 ARM64

ARM 板上不得复用 x86_64 的 `build`、`install` 或 `log` 目录。
下面的通用 ARM64 示例使用 Jazzy/Ubuntu 24.04；RK3568 示例使用 Humble/Ubuntu
22.04；下文的 X5 证据也单独使用 Humble。这些是分开的发行版/操作系统配对。

```bash
RUNTIME_PLATFORM_PROFILE=generic-arm64 ROS_DISTRO=jazzy \
  bash scripts/check_arm64_environment.sh
bash scripts/build_on_arm64.sh
bash scripts/run_arm64_smoke.sh
```

RK3568 的 Ubuntu 22.04 镜像使用受支持的 Humble 组合：

```bash
RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble \
  bash scripts/check_arm64_environment.sh
RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble \
  bash scripts/build_on_arm64.sh
RUNTIME_PLATFORM_PROFILE=rk3568 ROS_DISTRO=humble \
  bash scripts/run_arm64_smoke.sh
```

ARM 脚本只接受 Jazzy/Ubuntu 24.04 和 Humble/Ubuntu 22.04 配对。
ARM smoke 会顺序执行各包测试，避免小型板卡上的 ROS 发现和资源竞争。

## 可选工作流输入

### 文本输入

原有 C++ `ai_task_adapter` 继续提供离线、确定性的规则匹配。新增的
`ai_model_adapter_node.py` 可以调用 OpenAI 兼容的 Chat Completions 或 Responses
接口，但默认禁用。模型输出必须严格只含 `workflow_id`、`target_id`、`duration_ms` 三个字段，
并通过白名单和时限校验后才能提交 `ExecuteWorkflow`；模型不能直接发送 CAN 或设备
命令。

使用带密钥的接口时必须使用 HTTPS；密钥只放环境变量，不写入 YAML 或日志：

```bash
export OPENAI_API_KEY='replace-me'
ros2 run ai_task_adapter ai_model_adapter_node.py --ros-args \
  --params-file "$(ros2 pkg prefix ai_task_adapter)/share/ai_task_adapter/config/ai_model_adapter.yaml" \
  -p mode:=openai_compatible \
  -p request:='去 dock_a。'
```

使用本地 Ollama 兼容接口时，将 `model_endpoint` 改为
`http://127.0.0.1:11434/v1/chat/completions`，`model_name` 改为已安装模型，并把
`api_key_env` 设为空字符串。请求有超时限制、拒绝重定向，响应最大 1 MiB；额外字段、
输入最多 4096 字符、输出最多 128 tokens。额外字段、越界时长和非白名单目标都会在
进入 ROS Action 前失败关闭。Goal 响应和 Action 结果同样有超时边界；结果超时会请求
取消并按失败退出。

对于 `/v1/responses`，设置 `api_style:=responses` 和对应的 `model_endpoint`；请求会使用
`instructions` 和 `input`，同样执行白名单和超时校验。CI 对两种 API 风格都使用本地假
接口。X5 曾运行配置好的 Responses 兼容服务；这只证明有界适配路径的集成，不证明模型
质量、可用性或执行器运动。

CI 使用本地假模型接口验证 HTTP 协议和契约用例。安装配置默认保持 `disabled`；显式
启用时，本文档的运行 backend 是 `openai_compatible`。模型准入边界只在工作流接纳前
失败关闭：格式错误、超时、额外字段或非白名单输出会在提交新的 `ExecuteWorkflow`
goal 前被拒绝。这不会取消已经接纳的 goal，也不提供 ROS 2/DDS 授权、CAN 鉴权或物理
急停保证；这些分别属于 Action 取消路径、部署安全、设备协议和硬件安全系统。

### ArUco 输入

`perception_task_adapter` 从图像或 USB 相机检测 `DICT_4X4_50` 标记，并通过
`ExecuteWorkflow` 提交：ID `10` 映射 `single_task/dock_a`，ID `20` 映射
`ready_then_task/home`。相机模式要求连续三帧一致、抑制重复提交，在五帧空画面后
重新启用，并拒绝包含多个已映射标记的画面。CI 感知测试使用生成图像，下面单独
展示 X5 实体摄像头证据。

这是基于标记的图像适配器，不是视觉语言模型（VLM）集成；本文不声称具备 VLM 能力。

| 标记 ID | 工作流 | 目标 |
| --- | --- | --- |
| `10` | `single_task` | `dock_a` |
| `20` | `ready_then_task` | `home` |

## 平台状态

| 环境 | 当前状态 | 证据 |
| --- | --- | --- |
| Windows + WSL2 x86_64 | 软件已验证 | Ubuntu 24.04/Jazzy 隔离构建；已提交测试源码定义 123 个 GoogleTest 用例和 27 个 pytest 用例 |
| Ubuntu 24.04 + Jazzy x86_64 | CI 已验证 | 构建、测试、配置检查和条件式 `vcan0` E2E |
| 通用 ARM64 Linux | 配置和脚本已准备，不声称板卡已验证 | `generic-arm64` Jazzy/Ubuntu 24.04 配置 |
| RK3568 | CPU-only ARM64 配置，等待原生运行 | 不声明厂商 NPU/GPIO/相机能力 |
| X5 | 原生运行时和实体 I/O 台架已验证 | Ubuntu 22.04/Humble 构建、未报告测试失败的顺序 ARM smoke、受限假模型检查、UVC ArUco 和双 CANable 通信 |
| 32-bit ARM | 不支持 | 运行时目标为 64-bit Linux |

## 硬件演示

**状态：X5 原生运行、实体 UVC/ArUco 输入和双 USB-CAN 台架链路已验证；不声明
电机或硬件急停已经验证。**

[![X5 实体摄像头识别 ArUco ID 10](docs/assets/x5-aruco-live-demo.jpg)](docs/assets/x5-aruco-live-demo.mp4)

[6 秒裁切真机演示](docs/assets/x5-aruco-live-demo.mp4)只保留纸张和状态面板，展示
连续三帧确认以及 `single_task/dock_a` 映射。录制时工作流 Action 服务器保持离线，
因此没有发送工作流 CAN 帧，也没有运动命令。

双 CANable 台架测试另行使用实体 `can1`/`can2` 完成两个方向的 `cansend`/`candump`。
它证明 Linux SocketCAN、USB-CAN 和接线可用，不代表已经验证电机行为。

## 证据边界

| 已证明 | 尚未证明 |
| --- | --- |
| 固定工作流和嵌套 ROS 2 Action | 模型动态生成控制流 |
| 本地假接口验证的严格 OpenAI 兼容模型契约 | 真实模型质量、可用性或提示词准确率 |
| X5 实体 UVC 和稳定 ArUco ID 10 检测 | 相机标定、复杂光照覆盖或模型准确率 |
| 双适配器物理 SocketCAN 通信和 `vcan0` 协议测试 | 执行器行为或机器人闭环控制 |
| 软件 `SAFE_STOP` 和持久化任务证据 | 硬件急停或实测停止距离 |
| 分开的 x86_64 Jazzy 和 X5/Humble 原生运行 | RK3568 原生执行和厂商加速器 |

Loopback Gateway 尚未提供 TLS、身份认证、高可用或生产吞吐量证据。

## 开发流程

本项目包含直接实现、上游组件集成和 AI 辅助迭代。“AI 辅助”只描述开发辅助，
不用于认定作者身份、运行时自主性或 VLM 能力，也不是验证方法。运行时能力仅由
源代码、测试、CI 或明确标注的硬件证据支持；计划、生成文本和 AI 辅助本身不是运行时
证据。Jazzy 和 Humble 的表述对应上文列出的分开环境。公共 Git 历史保持不变。

## 许可证

项目使用 [Apache License 2.0](../../LICENSE)。第三方归属见
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
