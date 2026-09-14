# M1 运动控制平滑性修正与真实验证

日期：2026-09-14。分支：`feat/motion-quintic-point-to-point`，基线 `ee24d61c4216a3ba07ce8cf6c55b73273b94b93b`，本次修改未提交或推送。范围仅为已授权的 M1 修正；不进入 M2。

**结果：最终源码的真实 Gazebo 49 次试验通过，110 组测量指标全部位于原加速度 0.5 rad/s²、jerk 2 rad/s³ 限值内。最大观测分别为 0.2310532 和 1.8017614。** 这是所测工况的离散观测结果，不是连续物理限值证明。旧的失败报告保留于 [M1 基线报告](motion-m1-report.md)，不覆盖历史数据。

## 根因与分离验证

### 1. 仿真时间被异步 `/clock` 覆盖

实际环境是 controller_manager 4.45.2、JointTrajectoryController（JTC）4.40.1、gz_ros2_control 1.2.19。Gazebo 插件传入物理引擎的 simulation time，原 controller_manager 却在 update 内用经 DDS 传递的 ROS clock 重新取时间。其到达延迟让本应 2 ms 的控制周期变为 1/3 ms，并影响反馈时间戳。JTC 按该周期推进参考相位，但命令仍固定前视 2 ms。position 插件把位置差乘以 `position_proportional_gain * update_rate = 0.1 * 500 = 50` 转换为速度，放大相位跳动。

固定版本源码证据：

- [gz_ros2_control 1.2.19 插件](https://github.com/ros-controls/gz_ros2_control/blob/1.2.19/gz_ros2_control/src/gz_ros2_control_plugin.cpp)：PreUpdate/PostUpdate 使用 `_info.simTime`，约第 497、514 行。
- [controller_manager 4.45.2](https://github.com/ros-controls/ros2_control/blob/4.45.2/controller_manager/src/controller_manager.cpp)：约第 3369、3398 行，从传入 time 改取 trigger clock / `this->now()`。
- [JTC 4.40.1](https://github.com/ros-controls/ros2_controllers/blob/4.40.1/joint_trajectory_controller/src/joint_trajectory_controller.cpp)：约第 298—306 行，周期累积与固定前视。
- [Gazebo position 接口](https://github.com/ros-controls/gz_ros2_control/blob/1.2.19/gz_ros2_control/src/gz_system.cpp)：约第 790—808 行，位置差到速度的比例映射，无独立加速度/jerk 限幅。

修正只在 `use_sim_time` 下统一使用传入 time；硬件的 steady clock 路径保留。上游 C++ 回归令 ROS clock 从 900 ms 到 901 ms，而传入 simulation time 从 1 s 到 1.002 s：同一测试加载原库时失败（收到 901 ms、周期 1 ms），加载补丁库时通过（收到 1.002 s、周期 2 ms）。不能将普通升级宣称为已包含本修正。

### 2. 停止时用过时测量重置了整条轨迹

旧实现将稍早测得的 q/v/a 作为新轨迹 t=0 起点，并发送零 header stamp。JTC 立即替换旧轨迹，首次 update 才确定新 epoch；t=0 的首点使它直接进入新曲线。`interpolate_from_desired_state=false` 不能修复这个过时首点。

历史 hold 数据中，新首点 q=0.0109397956 rad，而发送时实测已经到 q=0.0116136768 rad；随后实测速度从 +0.06872798 到 −0.04075225 rad/s，1 ms 内出现 −109.48 rad/s²。存在真实速度反向，不能归因于“仅差分噪声”。

源码证据：[Trajectory::sample 4.40.1](https://github.com/ros-controls/ros2_controllers/blob/4.40.1/joint_trajectory_controller/src/trajectory.cpp) 约第 125—148 行的零 epoch / 首点处理，以及 JTC update 约第 252—261 行的轨迹替换。

修正后的 stop/hold 保留**已准入的前缀和原绝对 epoch**，在未来参考时刻取 q/v/a，再接入满足连续限值的五次制动。采样值属于参考，日志中的 `state` 始终是真实反馈。仅设置一个未来 header 而不保留前缀会提前替换当前命令，不采用该方法。

| 实验 | 范围 | 最大加速度 rad/s² | 最大 jerk rad/s³ |
|---|---|---:|---:|
| 修正前 `m1-final-verified` | 49 次；所有可用分段 | 109.48022 | 73096.91262 |
| 仅时钟修正 `clock-only` | 13 次；普通往返 | 0.2310532 | 1.7997021 |
| 仅时钟修正 `clock-only` | 同一实验；停止 | 70.2897507 | 38587.0081754 |
| 两项修正 `combined-final` | 49 次；含跨切换整段指标 | 0.2310532 | 1.8017614 |

对照使用同样的 0/7/42 seed、目标幅度和限值。clock-only 每 seed 1 次重复，只包含 nominal/stop；最终矩阵每 seed 2 次重复，包含 nominal/stop/hold/feedback_loss。不同范围已明确列出，不视为完全配对的统计试验。

## 变更、接口与兼容性

| 文件（仓库相对路径） | 原因和影响 |
|---|---|
| `docker/build_motion_runtime.py` | 离线提取固定 Git revision，构建匹配版本测试资源及 controller_manager；强制原版失败/补丁版通过后才写 manifest 和 setup。拒绝错误版本及已有输出目录；不改变 apt 包 |
| `docker/patches/controller-manager-sim-time.patch` | 仿真 update 统一时钟、只读就绪标记及上游 C++ 回归测试 |
| `robot/control/workbench_motion/workbench_motion/motion_safety.py` | 复用 AcceptedTrajectory/preflight；新增起始静止前缀与未来 C2 制动衔接，重验整条曲线的连续极值 |
| `robot/control/workbench_motion/workbench_motion/trajectory_executor.py` | 内部 MotionTransport.send 新增必填 `start_time_s`。发送/接收期限、落盘后重查期限、活动场景绑定；反馈仍独立检查，不用参考冒充观察 |
| `robot/control/workbench_motion/workbench_motion/gazebo_adapter.py` | 写入绝对 header stamp；同时参考同域 JointState 与 DDS clock；未检测到 patched simulation clock 标记时拒绝执行 |
| `robot/control/workbench_motion/workbench_motion/motion_benchmark.py` | 有界等待 DDS 就绪；记录运行时 manifest；增加跨 request 切换的 whole-trial 指标，不遗漏边界差分 |
| `robot/control/workbench_motion/config/motion_control.yaml` | 显式 `dispatch_lead_s=0.25`、`dispatch_margin_s=0.02`；原 v/a/jerk、跟踪和停止阈值不变 |
| `robot/control/workbench_motion/test/test_{motion_runtime,motion_safety,trajectory_executor,gazebo_adapter,motion_benchmark}.py` | 固定源码来源、C2 连续性、同 epoch、时间/场景/日志阻塞、晚接受、标记缺失、证据依赖与就绪超时测试 |
| `docs/task_packets/motion-m1-smoothness.json`、`docs/plans/2026-09-14-motion-control.md`、两份 M1 evaluation 文档 | 授权范围、修正后的契约、历史/当前证据与复现方法 |

正常轨迹先有 250 ms 的验证过的静止前缀，允许在正的绝对 epoch 下投递；stop/hold 继续跟踪原前缀约 250 ms 后进入制动。接受必须早于变更点至少 20 ms。耗时 preflight 或日志 fsync 耗尽期限时零下发；接受过晚则取消并锁定 faulted/unconfirmed，不能报告安全停止。这些是监督层保护，不是安全认证的通信或实时延迟上界。

内部传输调用者必须提供 epoch；现有公开 RobotState、RobotCommand、Controller、WorldState、TaskGraph、VerificationResult、JSON Schema/Pydantic 和事件语义不变。ROS/Gazebo 依赖仍只在适配层。未更改 Compose、Dockerfile、硬件模式、厂家限值、firmware、RL 或 MuJoCo；原 `docs/DEVELOPMENT_ROADMAP.md` 未改动。

## 最终真实运行结果

最终 run 为 `combined-final`，容器 `workbench-m1-clock-v3-20260914`，ROS domain 44、Gazebo partition `workbench-m1-clock`。Gazebo server/GUI/RViz 来自项目现有 launch，使用宿主机 NVIDIA/X11 显示。

- Image ID：`sha256:4afb873e5cc6d466c6d0bda73db2455d977da52219839ad74eff442a7f9e5e0e`。
- 当前源码 motion overlay：`/workspace/log/m1-smooth-install`；旧 clock-only overlay 单独保留。
- ros2_control revision：`4324cabf03a1371951f0a039d239fcf09f563e54`（4.45.2）。
- patch SHA256：`1dbf1a44376a88a2afad2f22244c8347730ca5da74027edfa2496ec028664e6b`。
- 提交前移除了 patch 中一行冗余空白上下文以通过 staged whitespace 检查；提交版本 SHA256：`31d9cca0de4cbcac7518feccfee8495238a06758eec1de28f48662a0a330eb00`。分别对固定上游 revision 应用两份 patch，全部输出文件逐字节相同；上面的原始运行 manifest/归档保留原 patch 哈希，物理实现未变。
- 实际加载库 SHA256：`4b84006b39ded1c459dfb0cbe875621f456d956db50b848da26f6dcfc7551941`；通过 Gazebo `/proc` maps 核对加载路径为 `/workspace/log/m1-clock-runtime-v3/build/libcontroller_manager.so`。
- manifest SHA256：`ffbefe5bfd02329e67fe3a56f621f546c24252dc069b2edd4a27195a8f6f009b`。

49 次试验包括 31 次 nominal（含零位移和回位）、6 次 stop、6 次 hold、6 次显式反馈屏蔽故障注入。全部通过执行/拒绝/确认门禁。110 组指标包括 61 个 request 分段和 49 个 whole-trial 统计，两者有重叠，**不是 110 次独立试验**。

| 指标 | 最终值 |
|---|---:|
| 超过原 a/jerk 限值的指标组 | 0 / 110 |
| 最大单关节 RMS 位置误差 | 0.001010596 rad |
| 最大绝对位置误差 | 0.001632406 rad |
| 最大实测速度差分加速度 | 0.231053177 rad/s² |
| 最大实测速度二阶差分 jerk | 1.801761423 rad/s³ |
| 指标内反馈采样率 | 488.932—500.000 Hz |
| 仿真/墙钟时间比 | 0.973171—1.029000 |
| SQLite reopen/replay 事件数 | 49 |
| 运动执行新增 WorldState 实体事实 | 0 |
| verified success | 未判定：缺少独立世界观察 |

同 seed 的 nominal 肩关节 RMS 误差（rad）：seed 0 为 0.000815016 / 0.000813057；seed 7 为 0.000874241 / 0.000871190；seed 42 为 0.000551790 / 0.000552166。seed 仅控制目标生成，不承诺物理轨迹逐 bit 相同。轮次间使用受控回位，不 reset 物理世界。

## 构建与运行

以下路径是容器工作目录示例，不依赖个人 home、设备节点或 IP。先使用项目 Docker 镜像和对应 GUI profile；当前代码必须经 colcon 构建，不能使用镜像内旧版 motion 模块。

1. 获取 ros2_control 4.45.2 源码并固定到上述 revision。将 Git 对象复制到构建卷，由构建 UID 解包；这也避免不同 UID 的只读 checkout 触发 Git ownership 检查。构建工具仅从固定 revision 提取 archive，本身不访问网络：

```bash
# 宿主机；源码临时保留在仓库之外
git clone --depth 1 --branch 4.45.2 \
  https://github.com/ros-controls/ros2_control.git /tmp/workbench-ros2-control-4.45.2

# BUILD_CONTAINER 是已经启动的项目构建/仿真容器；UID 以卷所有者为准
tar -C /tmp/workbench-ros2-control-4.45.2 -cf - .git | \
  docker exec -i -u 10001:10001 "$BUILD_CONTAINER" bash -c \
    'mkdir /workspace/log/motion-clock-source && tar -xf - -C /workspace/log/motion-clock-source'

# 以下在项目容器内执行；--source 指向刚复制的 Git 对象目录
source /opt/ros/jazzy/setup.bash
python3 /workspace/src/docker/build_motion_runtime.py \
  --source /workspace/log/motion-clock-source \
  --workspace /workspace/log/motion-clock-runtime
```

`--workspace` 必须是新目录，安装的 controller_manager 必须为 4.45.2。生成的 `baseline-regression.log` 和 `patched-regression.log` 必须分别失败/通过，否则没有可用 setup。仅通过 `LD_LIBRARY_PATH` 覆盖同 ABI 的 shared library，系统包和硬件时钟路径不修改。

2. 在可写构建卷中构建本分支：

```bash
source /opt/ros/jazzy/setup.bash
colcon --log-base /workspace/log/motion-colcon-log build \
  --base-paths /workspace/src/robot/control \
  --build-base /workspace/log/motion-build \
  --install-base /workspace/log/motion-install \
  --merge-install --packages-select workbench_motion
```

构建进程用户须拥有构建卷。本次卷属于 UID 10001，而 GUI 属于宿主机用户，因此构建和运行使用不同用户；**所有运行中的 ROS 客户端必须与 Gazebo 控制进程同 UID**，否则 Fast DDS SHM 可能只发现话题却无法收到数据。

3. 用现有 `gz-gui-x11` profile 加载两个 overlay 后启动（也可将下面命令作为 Compose run 的 `bash -c` 内容）：

```bash
source /opt/ros/jazzy/setup.bash
source /workspace/log/motion-install/setup.bash
source /workspace/log/motion-clock-runtime/setup.bash
ros2 launch workbench_motion sim_control.launch.py gui:=true rviz:=true
```

本机 NVIDIA/X11 临时 override 的必要部分如下；不提交个人显示配置：

```yaml
services:
  gz-gui-x11:
    runtime: nvidia
    tmpfs:
      - /home/workbench:size=128m,mode=755,uid=${HOST_UID},gid=${HOST_GID}
    environment:
      ROS_LOCALHOST_ONLY: "1"
      NVIDIA_DRIVER_CAPABILITIES: compute,utility,graphics,display
```

宿主机设 `HOST_UID/HOST_GID` 为当前用户，并将 `WORKBENCH_UID/WORKBENCH_GID` 设为相同值，确保 Compose 中 `.ros`、`.cache` tmpfs 与 GUI 用户匹配；传入有效 `DISPLAY` 和 `XAUTHORITY`。采用独立 `ROS_DOMAIN_ID` / `GZ_PARTITION`，不要与其他仿真实例共用。实际构建和运行中遇到的缺测试资源、UID/日志权限、DDS discovery 初期未就绪均保留为环境故障；未以假运动结果替代。

4. 在同一运行容器、同一 UID 的另一个终端运行基准。项目 Python 必须能 import 当前版本 contracts 和 world model；本次使用了当前源码的隔离安装 `/workspace/log/m1-python`：

```bash
source /opt/ros/jazzy/setup.bash
source /workspace/log/motion-install/setup.bash
source /workspace/log/motion-clock-runtime/setup.bash
python3 -c 'import workbench_contracts, workbench_world_model'
python3 -m workbench_motion.motion_benchmark \
  --output /tmp/my-motion-run \
  --seeds 0 7 42 --repeats 2 \
  --image-id '<docker inspect 的不可变 Image ID>' \
  --source-revision '<git revision 和 dirty 状态>'
```

也可用 `ros2 run --prefix python3 workbench_motion motion_benchmark ...`。输出目录必须不存在。默认四种模式，可用 `--modes nominal stop hold feedback_loss` 选择。缺 manifest、patched 标记、状态或证据依赖会失败；不能只看 `status=PASS`，同时检查 `measured_derivative_bounds`、fault、reset、cleanup 和 replay 证据。从 `/tmp` 运行时，停止容器前先导出数据；较长实验应使用当前运行 UID 可写的持久化实验目录。

## 测试与审查

| 检查 | 结果 |
|---|---|
| 上游 C++ 时钟回归 | 同一测试：原版 exit 1，补丁 exit 0；构建成功 |
| 项目镜像中 motion pytest | 375 passed，包含契约持久化用例 |
| colcon test / test-result | 375 tests，0 errors，0 failures，1 skipped；系统 Python 缺语义包的该用例已在上行执行 |
| `make test` | 1175 passed、363 subtests passed、2 skipped（既有可选 Controller Compose runtime smoke） |
| `make contract` / `make scenario-check` | PASS；契约往返、12 frozen + 24 expanded、seed 确定性 |
| `make context-check` / 两份 M1 Task Packet | PASS |
| Ruff check / format、`git diff --check` | PASS |

测试在项目镜像中使用当前源码进行。主机单独 `uv run --offline --directory robot/control pytest` 缺 UR description，不能替代容器测试。

```bash
# 项目 Python；仓库根目录
make test
make contract
make scenario-check
make context-check
make task-check PACKET=docs/task_packets/motion-m1-smoothness.json

# 已 source Jazzy 与当前 overlay；robot/control 目录
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q

# 使用与 build 相同的目录；需显式 --merge-install
colcon --log-base /workspace/log/motion-colcon-test-log test \
  --build-base /workspace/log/motion-build \
  --install-base /workspace/log/motion-install --merge-install \
  --packages-select workbench_motion
```

审查只覆盖本次修正：新构建脚本及其测试经过复用/质量/效率三个独立视角，无需简化；重叠已有 M1 工作的文件未做文件级简化。执行器、适配器、数值衔接、C++ patch 和实验代码进行了针对性人工式代码扫描，没有把无关分支工作纳入审查。审查发现并修复“日志落盘后过期仍发送”和“stop 重新接纳变化场景”两项，新增测试先失败后通过，随后重跑最终 49 次真实矩阵。没有遗留本范围内的未处理审查项。

## 证据与边界

原始数据、编译结果和运行日志不入 Git：

- 本地导出：`/tmp/workbench-m1-smoothness-evidence.tar`，包含 clock-only、组合验证和失败启动/就绪记录、最终 JSONL/summary/SQLite、运行库 manifest、回归及项目测试日志、GUI 日志和模块哈希核对。
- 容器持久化数据：`/workspace/log/m1-smoothness-evidence/combined-final` 与 `clock-only`。
- 修正前对照：`/tmp/workbench-m1-final-evidence.tar`；旧报告的失败结论仍可追溯。

已实现的是 position JTC 上的有界参考、监督执行、受控停止/保持与证据闭环。本轮只激励肩关节约 0.03—0.06 rad，其他关节保持原位，未覆盖六轴耦合、负载、重力方向变化、外力接触或真机。

反馈丢失试验显式屏蔽观察；注入前的可用数据和随后静止/reset 确认不能证明取消瞬间的平滑性。全部导数仍由原始实测速度及时间戳有限差分得到，未提高阈值、滤波隐藏尖峰或以参考导数替换实测。Python 监督层、DDS 延迟和 position 插件都不是安全认证的实时保障；完全断链/进程被杀不能由这里证明停止。

launch 仍记录现有 `No 3D sensor plugin(s) defined for octomap updates` 与 MoveIt `No controller_names specified`。本轮使用已有静态规划场景的状态有效性服务和直接 JTC action 传输，未验证动态 octomap 感知或 MoveIt 自身的轨迹执行配置。OGRE 的实际 `GL_VENDOR/GL_RENDERER` 为 NVIDIA；EGL 枚举警告原样保留在证据中。

下一阶段需单独授权：优先扩展低速六轴及耦合工况验证，再按 [路线](../plans/2026-09-14-motion-control.md) 做 M2 笛卡尔键盘 jog：target → 可替换 IK planner → joint trajectory → Controller。动力学辨识、重力补偿、导纳、阻抗和残差碰撞检测仍按后续阶段推进，当前不声称已实现。
