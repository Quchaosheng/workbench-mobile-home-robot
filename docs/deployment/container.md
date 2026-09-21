# Ubuntu 24.04 + ROS 2 Jazzy 容器部署

此仓库只维护一个 `linux/amd64` 全功能开发镜像。镜像基础固定为
`nvidia/cuda:12.8.1-runtime-ubuntu24.04@sha256:828c4d…ca2e`，容器内安装 ROS 2 Jazzy、Gazebo Harmonic、MoveIt 2、
TRAC-IK、ros2_control 和 MuJoCo 3.3.7。默认服务仍是无 GPU、无设备权限的只读 dashboard。

当前开发机的 NVIDIA 驱动和 Container Toolkit 能识别 RTX 4060 Laptop GPU；但容器只发现 Mesa EGL vendor，
缺少 NVIDIA EGL vendor JSON。GPU 能力预检因此为 `NOT_EXECUTED`，不会把不可用主机能力误报为成功。已有的
诊断证据必须分开记录：绕过 GPU 门禁的 Gazebo server smoke 在 `gz::common::FileLogger::Init` 附近以退出码
139 (`FAIL`) 崩溃；MuJoCo 若退回 Mesa/llvmpipe 则为软件渲染 (`FAIL`，不是 NVIDIA GPU 验收)。配置目标不等于
实卡验收结果，也不能把 Mesa/llvmpipe 软件渲染描述成 GPU 成功。

集成审查范围包括 Dockerfile/Compose、entrypoint、ROS/Gazebo 启动脚本和 CI；合并前必须由集成负责人复核
`docker compose config`、镜像构建、dashboard 健康检查、colcon 和 profile fail-closed 行为。此记录不替代人工审批。

## 宿主前提

宿主可以是 Ubuntu 22.04 或 24.04，不需要安装 ROS。必须安装：

- Docker Engine，Docker Compose `>= 2.30`；
- 仅 GPU profile 需要 NVIDIA Linux 驱动 `>= 570.26`；
- 仅 GPU profile 需要 NVIDIA Container Toolkit `>= 1.17`；
- 当前用户能访问 Docker daemon；
- X11、Wayland、SocketCAN、USB 和 SROS2 keystore 均由宿主负责。

检查宿主，不会修改系统：

```bash
make container-host-doctor
```

NVIDIA 驱动和 Container Toolkit 不能装进镜像。请按 Docker 与 NVIDIA 官方文档安装，并用下面命令验证运行时：

```bash
docker run --rm --gpus all nvidia/cuda:12.8.1-runtime-ubuntu24.04 nvidia-smi
```

## 容器目录

| 路径 | 内容 | 写权限 |
| --- | --- | --- |
| `/opt/ros/jazzy` | apt 安装的 ROS 2 Jazzy | 镜像只读 |
| `/opt/workbench_ws` | 镜像构建时完成的 `workbench_motion` colcon workspace | 镜像只读 |
| `/opt/workbench-venv` | dashboard 项目 Python 3.12 venv | 镜像只读 |
| `/opt/workbench-mujoco-venv` | MuJoCo 3.3.7 容器专用 venv | 镜像只读 |
| `/workspace/src` | 当前仓库的只读 bind mount | 只读 |
| `/workspace/build` | 开发 colcon build volume | 命名卷 |
| `/workspace/install` | 开发 colcon install volume | 命名卷 |
| `/workspace/log` | colcon 和验收证据 volume | 命名卷 |

entrypoint 依次 source `/opt/ros/jazzy/setup.bash`、`/opt/workbench_ws/install/setup.bash`、可选的
`/workspace/install/setup.bash`，最后用 `exec` 启动命令。

## 新开发者快速开始

```bash
git clone https://github.com/Quchaosheng/workbench-mobile-home-robot.git
cd workbench-mobile-home-robot
make container-build
make container-check
docker compose up dashboard
```

浏览器访问 `http://127.0.0.1:8080`。dashboard 不声明 `gpus`、设备映射、DDS LAN、额外 capability 或 host network，
所以无 NVIDIA 显卡的开发机也可以使用。

VS Code / Codex Dev Container 复用根目录 Dockerfile，不存在第二套 Ubuntu/ROS 依赖。源码按当前安全基线只读挂载；在宿主编辑，
在容器内构建和测试：

```bash
make container-colcon-build
make container-colcon-test
```

通过 `WORKBENCH_BUILD_HTTP_PROXY`、`WORKBENCH_BUILD_HTTPS_PROXY`、`WORKBENCH_BUILD_NO_PROXY` 显式传入标准
BuildKit proxy 参数；不会意外继承开发者终端中的代理。代理凭据不得写入 Dockerfile、Compose 或镜像层。

## GPU 依赖三级

| 层 | 作用 | 何时需要 GPU |
| --- | --- | --- |
| `gpu-runtime` | CUDA 12.8 用户态库；驱动和设备由宿主注入 | 构建和 dashboard 不需要 |
| `gpu-simulation` | Gazebo/OGRE/EGL 与 MuJoCo EGL | ROS headless、GUI、MuJoCo profile |
| `gpu-validation` | 校验驱动、真实产品名、compute capability、NVIDIA renderer 和真实图像 | 发布实卡验收 |

矩阵同时匹配产品名和 compute capability：RTX 30/Ampere `8.6`、RTX 40/Ada `8.9`、RTX 50/Blackwell `12.0`。
只匹配 capability 的 A10、L40 等卡不会被误记为 GeForce RTX 实卡证据。每次只验收当前主机的一代卡：

```bash
WORKBENCH_GPU_TIER=rtx30 WORKBENCH_IMAGE_DIGEST=sha256:<tested-image> make container-gpu-matrix-check
# 在另外两台实卡主机分别使用 rtx40、rtx50，且必须使用同一 image digest。
```

缺少任何一代报告时，只能声明“配置目标覆盖 RTX 30/40/50”，不能声明“已验证”。`llvmpipe`、`softpipe`、`swrast` 和空白图像
均失败，不能作为 NVIDIA EGL 通过证据。

## Compose profiles

```bash
# NVIDIA headless Gazebo + controller + /clock + Phase 2 probe
docker compose --profile ros-sim run --rm ros-sim /usr/local/bin/workbench-sim-smoke

# MuJoCo 固定 seed、有限步进和 EGL 像素 checksum
make container-mujoco-check
```

X11 必须使用当前会话的 Xauthority cookie，禁止 `xhost +`：

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) DISPLAY=$DISPLAY XAUTHORITY=$XAUTHORITY \
  docker compose --profile gz-gui-x11 run --rm gz-gui-x11
```

Wayland 只映射当前用户的一个 socket；没有桌面会话时为 `NOT_EXECUTED`：

```bash
HOST_UID=$(id -u) HOST_GID=$(id -g) XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR WAYLAND_DISPLAY=$WAYLAND_DISPLAY \
  docker compose --profile gz-gui-wayland run --rm gz-gui-wayland
```

hardware shell 不启动控制节点，不配置 CAN，不发送帧，也不替代急停。串口必须是稳定的 `/dev/serial/by-id/...`；CAN 和 DDS
网卡必须已由宿主创建；LAN 模式强制 SROS2 Enforce 和外部只读 keystore：

```bash
WORKBENCH_SERIAL_DEVICE=/dev/serial/by-id/usb-EXACT_DEVICE \
WORKBENCH_CAN_INTERFACE=can0 \
WORKBENCH_DDS_INTERFACE=eno1 \
WORKBENCH_SROS2_KEYSTORE=/absolute/path/to/keystore \
  make container-hardware-doctor
```

不接受 `/dev/ttyUSB*`、设备目录、`privileged`、`NET_ADMIN`、host IPC 或由容器配置 CAN 总线。

## 构建证据与供应链

镜像内 `/usr/share/workbench/container/` 记录基础 digest、Ubuntu/ROS/CUDA/MuJoCo 版本、完整 apt 包版本和两个 venv 的
`pip freeze`。`.github/workflows/container-full-stack.yml` 生成 SPDX JSON SBOM，并用固定 SHA 的 Anchore/Grype action 扫描
高危和严重漏洞。发布 provenance 必须绑定 registry image digest；本地 tag 不能替代 registry digest。

## 后续容易遗漏的事项

- 单镜像体积、压缩/展开大小、构建时间和 registry 配额需要持续记录；未经 Owner 确认不拆镜像。
- ROS apt、PyPI wheel、CUDA 基础镜像和 GitHub Actions 都需要定期重建、重新生成 SBOM，并审核许可证与 CVE。
- 国内或离线团队应维护经过校验的镜像/apt/PyPI 镜像源，但不得静默改版本或绕过 checksum。
- Laptop GPU、eGPU、MIG、WSL2、Jetson、ARM64 和专业 RTX/A 系列不属于当前三代 GeForce 验收范围，应单独建 Task Packet。
- X11 与 Wayland 截图证据可能包含桌面信息，上传前要做隐私审查；私钥和 keystore 永远不能进仓库或 CI artifact。
- 真机 USB/CAN 需要 udev、组权限、总线速率、急停和现场安全评审；容器通过 doctor 不代表允许运动。
- DDS 双物理主机、错误网卡、跨 Domain 隔离、发现服务器和缺失 keystore 的测试仍必须在真实网络上完成。
