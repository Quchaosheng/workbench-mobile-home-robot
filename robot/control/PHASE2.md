# 阶段 2 详细实现文档 — 关节限位 + ros2_control + 控制器

> 配套 `PLAN.md §阶段 2`。本文件是阶段 2 的施工图：判定现状、锚定官方标准、列清可复用资产、给出高内聚低耦合的落地方案、逐文件清单、验收映射、攻击性测试规则。与 PLAN.md 同为本地工作文档。
>
> **契约不变**：ActionResult（outcome/dispatch_state/device_state）以 main 冻结版为准；阶段 2 不产 ActionResult（阶段 4/5/6 才产），只交付「限位 + 控制器 + 越限校验内核」的地基。

## 0. 官方标准锚点（验收措辞一律挂到这里，不自造）

阶段性标准的三个权威源，本文件所有验收都引用其具体条目，不含糊说"阶段 2 零运动"：

- **`docs/context/CONSTRAINTS.yaml`**（`make context-check` 强制存在，`tools/scripts/check_context.py` 门控）——唯一被工具校验的约束：
  - `safety.agent_may_emit_joint_commands: false`
  - `release.collision_or_limit_violation_must_equal_zero: true`
  - `release.false_completion_must_equal_zero: true`
- **README「Metrics」表**——官方验收数字；标 **0** 的三条 Safety 是 release blocker：False completion=0、Collisions/joint limit violations=0、Model emitting raw joint control=0。**注意：该表没有"controllers active"行**——控制器 active、TF 帧链是 **C2** 验收（项目派生），别挂到 README Metrics（§7 已据此更正）。
- **`05-机械臂资产.md` / `06-抓取与恢复.md` 的 C 系列分期 + `ADR-0001`**——任务归属哪个 P（月）、什么出 v0.1 范围（P0 = 纯仿真固定桌面单臂）。

**关键定性（决定阶段 2 交付边界，见 §5）**：`collision_or_limit_violation_must_equal_zero` 与 `agent_may_emit_joint_commands: false` 是 **v0.1 发布线**，不是"阶段 2 门"。官方无任何条目要求"阶段 2 必须是唯一 trajectory gate"。越限=0 的**端到端 ROS 闭合**天然落在阶段 4（适配器唯一接口 + 防绕过 + 命名空间隔离，PLAN §阶段 4）。阶段 2 交付其**纯逻辑校验内核**并记录反面证据。

## 1. Scope Matrix（本阶段做/不做，防误读）

按 C 系列官方分期显式划界，避免阶段 2 完成后被误解为已覆盖真机安全或抓取恢复：

| 能力 | 官方任务 | 期 | 阶段 2 |
|---|---|---|---|
| 仿真标称硬限位（position/velocity/effort）接入 URDF/ros2_control | C2 | P1 | ✅ 做（复用厂商已注入的 `<limit>`） |
| MoveIt 规划层限位 | C2 | P1 | 已存在（阶段 1 交付），本阶段不动 |
| ros2_control + 控制器 + 控制器 active | C2 | P1 | ✅ 做 |
| 越限**校验纯逻辑内核** + 反面证据 | C6/C12 前置 | — | ✅ 做（判定，不 clamp） |
| 越限**端到端 ROS 零运动闭合**（唯一 gate / 防绕过 / 命名空间隔离） | — | — | ❌ **归阶段 4**（PLAN §阶段 4） |
| 真机关节限位 + 安全参数、"仿真参数不能直接用真机" | C13/C14 | **P3** | ❌ 只建结构，内容留空（见 §4） |
| 力控柔顺、工作空间显式拒绝不可达 | C10/C12 | P2 | ❌ 后续 |
| 抓取失败/取消/重执行恢复、有界安全停车 | C5/C6 | P1(#6) | ❌ 阶段 4/6，非本阶段 |

## 2. 现状判定（可直接复用的阶段 1 资产）

阶段 1 已提交（`feat/motion-phase1-arm`），阶段 2 直接复用、不重造：

| 资产 | 位置 | 阶段 2 怎么用 |
|---|---|---|
| 合并 URDF（臂+世界） | `config/arm_on_workbench.urdf.xacro` | 同一份 xacro 内加 `<ros2_control>` + Gazebo 插件（`sim_gz` arg 门控），不新开描述文件 |
| 换臂唯一入口 | `config/arm.yaml` | 新增 `controllers:` 段作控制器命名唯一源；`gripper.driver_joint` 已在，复用 |
| arm.yaml 读取路径 | `workbench_motion/arm_config.py` | 扩 `ArmConfig` 加控制器字段 + `driver_joint`；生成器/校验器复用 |
| MoveIt 规划层限位 | `config/moveit/joint_limits.yaml` | 三路限位中的「规划路」已存在，本阶段不改 |
| SRDF / group 定义 | `config/moveit/workbench_arm.srdf` | 控制器控的关节名基准；一致性单测比对 |
| 证据事件 + Sink | `workbench_motion/evidence.py` | **阶段 4 用**（越限拒绝产 `ExecutionEvent` + `event_id`）；**阶段 2 的 `check_trajectory` 纯判定不碰它**（§5.5） |
| 统一日志 | `workbench_motion/logging_setup.py` | 拒绝/降级按 WARNING/ERROR，带 run_id |
| move_group 启动 + 路径解析 | `launch/move_group.launch.py` | share 解析、xacro→robot_description、`_require()` 抽公共，供新 sim launch 复用 |
| 纯逻辑 + ROS-free 单测范式 | `reachability.py` / `test_*` | 限位校验器同范式：纯 Python、uv 可测、无 rclpy |

**厂商已带可复用 ros2_control 宏，不要自拼 `<joint>` 接口：**
- `ur_description/urdf/inc/ur_joint_control.xacro` → 宏 `ur_joint_control_description`，给 6 臂关节 position/velocity/effort 接口。
- `robotiq_description/urdf/robotiq_gripper.ros2_control.xacro` → 宏 `robotiq_gripper_ros2_control`。**其仿真分支绑死 `ign_ros2_control/IgnitionSystem`（旧 Ignition 名），Jazzy/Harmonic 要 `gz_ros2_control/GazeboSimSystem`**——所以只借关节接口描述，插件由我们顶层 `<ros2_control>` 指定（§5.1）。

## 3. 环境前置（真正的卡点，先说清）

PLAN.md 说「阶段 2 无外部**模块**依赖」，指不等别的 Owner 交代码；但**运行时 apt 包本机未装齐**，是阶段 2 跑起来的硬前置，别和"模块依赖"混。另有 **Linux Owner 治理审批依赖**（launch/package.xml/setup.py 变更需 Linux Owner review，AGENTS.md:18）——无代码依赖，但有治理门槛。

本机现状（已核实 2026-08-10）：`gz-sim`(Harmonic)、`gz_ros2_control`、`controller_manager`、`joint_trajectory_controller`、`joint_state_broadcaster`、`ur_simulation_gz`、`robotiq_controllers` **均未安装**；apt 源有候选，可装。纯 Python 单测不受此阻，先行。

开工前补装（写进 `package.xml` 交 rosdep）：

```bash
sudo apt-get install -y \
  ros-jazzy-gz-ros2-control \
  ros-jazzy-controller-manager \
  ros-jazzy-joint-trajectory-controller \
  ros-jazzy-joint-state-broadcaster \
  ros-jazzy-gripper-controllers \
  ros-jazzy-ros-gz-sim \
  ros-jazzy-ros-gz-bridge \
  ros-jazzy-ros2controlcli
# Gazebo Harmonic 由 Jazzy gz vendor 依赖链提供，无需单独的 gz-harmonic 包。
```

夹爪控制器见 §5.3。**包与插件命名空间不一致，别混**（已按 Jazzy `ros-jazzy-gripper-controllers` 4.40.1 的 `ros_control_plugins.xml` 核实）：`GripperActionController` 由**包 `gripper_controllers`** 提供，但注册的**插件类型是 `position_controllers/GripperActionController`**（历史命名空间）。所以：装 `ros-jazzy-gripper-controllers` + `<exec_depend>gripper_controllers`，但 controllers.yaml 的 `type` 写 `position_controllers/GripperActionController`。另：`robotiq-controllers` 是**真机** Robotiq 激活控制器（C13/P3），与仿真夹爪 action 控制器无关。冻结前仍以 `ros2 control list_controller_types` 实测为准。

## 4. 三路限位，别混（FRAMES.md：限位归 `robot/control/`，不进 description）

| 路 | 层 | 来源文件 | 谁消费 | 松紧 |
|---|---|---|---|---|
| ① 硬限位 | 物理/驱动 | 厂商 `ur_description/config/ur5e/joint_limits.yaml`，经 `ur_macro` 已注入合并 URDF 的 `<limit>` | URDF / gz_ros2_control / 校验器基准 | 标称，最松 |
| ② 规划限位 | 规划 | `config/moveit/joint_limits.yaml`（**已存在**） | MoveIt 规划 | ≤ ①（见下「单位」） |
| ③ 真机安全覆盖 | 安全 | **新增** `config/joint_limits.hw_override.yaml`（C13/P3，阶段 2 只建结构） | 真机/校验器叠加 | 只能 ≤ ①，更严 |

**单位与"更保守"的精确定义（回应 review P2）**：
- ② 比 ① 保守体现在**两层**（回应 review：并非"数值与厂商 nominal 相同、只靠 scaling"）：(i) **数值本身已主动收紧**——如 shoulder_pan/lift 的 `max_velocity` 写 120°/s，而厂商 `ur_description` 3.5.1 是 180°/s；(ii) 再叠 `default_velocity_scaling_factor=0.1` / `acceleration_scaling_factor=0.1`。所以"② 比 ① 保守"比的是**规划时运行 scaling 后的有效限位**（`planning_value × scaling`），不是 YAML nominal。**规划层自己采用 scaling（MoveIt 消费），安全校验器不消费它**——校验器只检查 ① 硬限位 ∩ ③ 真机覆盖，不叠加 ② 的规划缩放。阶段 1 的 `joint_limits.yaml` 里"UR5e nominal"注释不实，本阶段改注释如实记录（见 §6）。
- 硬限位**不重写**——UR 的 `<limit>` 已在合并 URDF（`ur_macro` 从厂商 yaml 读入）。只**引用**作校验基准，不抄数字（抄=双源漂移）。

**③ `hw_override.yaml` 的失败语义（回应 review P2，全部 fail closed）**：
- 文件**为空 / 无 `joint_limits` 段**：合法，等价"不覆盖"（P0 纯仿真下正常，ADR-0001）。
- 某关节**缺失**：合法，该关节不叠加（不覆盖 ≠ 违规）。
- 某关节**比硬限更松**（`override.max_velocity > hard` 或 pos 区间更宽）：**fail closed**，加载即报错退出。安全层只能更严。
- **未知关节名**（不在 arm.yaml `joints`）：**fail closed**，报错——防止手滑写错关节名却静默无效。
- **单位/类型错误**（非数值、min>max）：**fail closed**。

## 5. 架构（高内聚低耦合）

三块解耦：**描述层**（xacro：ros2_control + Gazebo 插件）、**控制器配置层**（controllers.yaml）、**启动/校验层**（launch + 纯 Python 校验器）。描述层不知控制器 YAML 长相；校验器不 import 任何 ROS 消息类型（延续 PLAN §阶段 4「Runtime 包不 import MoveIt/ros2_control 消息」防绕过线）。

### 5.0 控制器命名 schema（冻结，回应 review P0-2）

**权威唯一源 = `config/arm.yaml` 新增 `controllers:` 段**。运行时 Python 经 `arm_config.load_arm_config()` 读取，不写死；controllers.yaml 里的名字与之一致由单测强制。冻结命名（统一用 `arm_trajectory_controller`，废弃早前草稿里混用的 `arm_controller`）：

`config/arm.yaml` 追加（不改已有字段；**不新增顶层 vendor: 段**——arm.vendor_description_pkg / arm.ur_type 已存在于 line 11-14）：
```yaml
# 控制器命名 + controller_manager 参数：ros2_control 层的唯一命名源。
# controllers.yaml 的键必须与此处一致（test_controllers_config.py 强制）。
controllers:
  update_rate_hz: 500                     # controller_manager 更新率，冻结入配置
  joint_state_broadcaster: joint_state_broadcaster
  arm_trajectory_controller: arm_trajectory_controller
  gripper_controller: gripper_controller
```

`ArmConfig`（`arm_config.py`）扩字段（保持 `frozen=True` dataclass 风格）。**注意：`vendor_description_pkg` / `ur_type` 的 YAML 字段虽已存在于 `arm.yaml:13-14`，但当前 `ArmConfig` dataclass（`arm_config.py:30`）并未解析它们**——阶段 2 需向 `ArmConfig` **新增**这两个字段并由 `parse_arm_config()` 解析：
```python
vendor_description_pkg: str  # 新增，来自 arm.vendor_description_pkg（YAML 已有，dataclass 未解析）
ur_type: str  # 新增，来自 arm.ur_type（YAML 已有，dataclass 未解析）
driver_joint: str  # 来自 gripper.driver_joint
update_rate_hz: int  # controllers.update_rate_hz
arm_trajectory_controller: str  # controllers.arm_trajectory_controller
gripper_controller: str  # controllers.gripper_controller
joint_state_broadcaster: str  # controllers.joint_state_broadcaster
```
`parse_arm_config()` 对应解析；缺 `controllers`/`gripper.driver_joint` 段时给显式错误（不静默默认）——命名与路径是安全相关配置，fail closed。测试：`test_arm_config.py` 加断言 `vendor_description_pkg == "ur_description"`、`ur_type == "ur5e"`、`driver_joint == "robotiq_85_left_knuckle_joint"`、控制器名齐全。

### 5.1 描述层：ros2_control 块 + Gazebo 插件

**在合并 URDF 里加，不新开描述文件。** `sim_gz` xacro arg 门控（默认 `false`）：
- `false`（阶段 1 现状）：纯几何，move_group/可达性不受影响，**阶段 1 零回归**。
- `true`：注入 `<ros2_control>` + `gz_ros2_control` 插件，供 Gazebo 起控制器。

**xacro 方案决策（回应 review P1）：采用「xacro 参数传入」，不采用构建时生成。** 关节名、tf_prefix、controllers.yaml 路径都作 xacro `params`/`arg` 传入宏；关节接口用厂商宏展开。理由：xacro 本就是参数化模板，运行期一次展开即定；构建时生成会引入"生成步骤+生成物入库"的额外维护面，与"合并 URDF 单一来源"相悖。

新增 `config/ros2_control.xacro`（本包宏，复用厂商关节接口）：
```xml
<robot xmlns:xacro="http://www.ros.org/wiki/xacro">
  <xacro:include filename="$(find ur_description)/urdf/inc/ur_joint_control.xacro"/>

  <!-- 夹爪控制接口：见 5.2，本包自定义以避免厂商仿真分支的 Ignition 插件 -->
  <xacro:macro name="workbench_gripper_control" params="tf_prefix driver_joint">
    <joint name="${tf_prefix}${driver_joint}">
      <command_interface name="position"/>
      <state_interface name="position"><param name="initial_value">0.0</param></state_interface>
      <state_interface name="velocity"/>
    </joint>
  </xacro:macro>

  <xacro:macro name="workbench_ros2_control" params="tf_prefix driver_joint controllers_yaml">
    <ros2_control name="workbench_arm_system" type="system">
      <hardware>
        <plugin>gz_ros2_control/GazeboSimSystem</plugin>
      </hardware>
      <xacro:ur_joint_control_description tf_prefix="${tf_prefix}"/>   <!-- 6 臂关节 -->
      <xacro:workbench_gripper_control tf_prefix="${tf_prefix}" driver_joint="${driver_joint}"/>
    </ros2_control>

    <gazebo>
      <plugin filename="gz_ros2_control-system"
              name="gz_ros2_control::GazeboSimROS2ControlPlugin">
        <parameters>${controllers_yaml}</parameters>
      </plugin>
    </gazebo>
  </xacro:macro>
</robot>
```

`arm_on_workbench.urdf.xacro` 顶部加 arg + 条件 include：
```xml
<xacro:arg name="sim_gz"          default="false"/>
<xacro:arg name="driver_joint"    default="robotiq_85_left_knuckle_joint"/>  <!-- 镜像 arm.yaml gripper.driver_joint -->
<xacro:arg name="controllers_yaml" default="$(find workbench_motion)/config/controllers.yaml"/>
...
<xacro:if value="$(arg sim_gz)">
  <xacro:include filename="$(find workbench_motion)/config/ros2_control.xacro"/>
  <xacro:workbench_ros2_control tf_prefix="$(arg tf_prefix)"
      driver_joint="$(arg driver_joint)" controllers_yaml="$(arg controllers_yaml)"/>
</xacro:if>
```

**"从 arm.yaml 读取"的落地方式（回应 review P1）**：xacro **不能**直接读 YAML。约定：`driver_joint` 既是 xacro `arg`（默认值）**也**是 arm.yaml 字段，二者一致由 `test_arm_config.py` 断言守护（延用阶段 1「arg 默认值↔arm.yaml」一致性做法）；launch 层（Python，能读 YAML）用 `load_arm_config().driver_joint` 显式传 `xacro ... driver_joint:=<值>`，运行期以 arm.yaml 为准。这样"唯一源是 arm.yaml"落到运行期，xacro 默认值只是可离线展开的兜底。

**臂+夹爪合一个 system**：gz_ros2_control 一个插件实例管一棵树全部关节即可，不必拆两个 system。

### 5.2 夹爪关节接口（宏来源明确，回应 review P1）

`workbench_gripper_control` 宏在 §5.1 给出，随 `config/ros2_control.xacro` 交付、经 setup.py `glob("config/*.xacro")` 装入 share。最小 spike 的 Gazebo 日志确认 driver-only system 只导出驱动关节，`joint_state_broadcaster` 无法观测五个从动关节，因此已启用预定 fallback：驱动关节声明 position command+state 接口，五个从动关节按厂商列表和 multiplier 显式声明为 state-only 接口。

**mimic 完整性是未验证假设，先做最小 spike（回应 review 阻塞项）**：厂商仿真宏（`robotiq_gripper.ros2_control.xacro:38+`）在 `<ros2_control>` 里把**五个从动关节**（`right_knuckle` / `left_inner_knuckle` / `right_inner_knuckle` / `left_finger_tip` / `right_finger_tip`，各带 multiplier ±1）**逐个显式声明**，而非只靠 URDF `<mimic>`。这说明"只声明驱动关节 + 靠 gz_ros2_control 处理 `<mimic>`"能否让 2F-85 在 Harmonic 下正确联动**尚未证实**。开工**第一步做最小 spike**：起 gz + 只声明驱动关节的 system，发一个夹爪目标，订阅 `/joint_states` 看五个从动关节是否按比例动。
- spike **通过**（gz_ros2_control 正确解析 URDF `<mimic>`）：保留最小宏。
- spike **不通过**：回退到"在 system 里显式声明五个从动关节 + multiplier"（照厂商宏的关节列表，但插件换 `gz_ros2_control/GazeboSimSystem`）。

**验收加一条（不能只看 controller active）**：夹爪 open→close 后，采 `/joint_states`，断言**五个 mimic 关节都满足对驱动关节的比例关系**（multiplier ±1，容差内），结果入 `phase2-controllers.json`。

不复用厂商 `robotiq_gripper_ros2_control` 整宏：它绑死 `ign_ros2_control/IgnitionSystem`（旧 Ignition），Harmonic 不适用（§2）；但**其从动关节列表 + multiplier 是 spike 失败时的权威参照**。

**验收纳入 xacro 展开结果**（回应 review P1）：`xacro arm_on_workbench.urdf.xacro sim_gz:=true > /tmp/wb_ctrl.urdf` 后断言 —— 展开物含 `<plugin>gz_ros2_control/GazeboSimSystem</plugin>`、含 6 臂关节 + 驱动关节的 `<command_interface name="position">`、含 gz_ros2_control-system gazebo 插件且 `<parameters>` 指向 controllers.yaml；`check_urdf /tmp/wb_ctrl.urdf` 单树无错。

### 5.3 控制器配置层：`config/controllers.yaml`

命名全部引用 §5.0 的 arm.yaml `controllers:` 段（此处示例值即冻结值）：

```yaml
controller_manager:
  ros__parameters:
    update_rate: 500                      # = arm.yaml controllers.update_rate_hz
    joint_state_broadcaster:
      type: joint_state_broadcaster/JointStateBroadcaster
    arm_trajectory_controller:
      type: joint_trajectory_controller/JointTrajectoryController
    gripper_controller:
      # 插件类型命名空间是 position_controllers/，但由包 gripper_controllers 提供（见下）
      type: position_controllers/GripperActionController

arm_trajectory_controller:
  ros__parameters:
    joints:                               # 必须 == arm.yaml arm.joints（单测强制）
      - shoulder_pan_joint
      - shoulder_lift_joint
      - elbow_joint
      - wrist_1_joint
      - wrist_2_joint
      - wrist_3_joint
    command_interfaces: [position]
    state_interfaces: [position, velocity]
    constraints:
      stopped_velocity_tolerance: 0.01    # 执行期跟踪容差，非入口越限判定
      goal_time: 0.5
      shoulder_pan_joint: {goal: 0.02}
      shoulder_lift_joint: {goal: 0.02}
      elbow_joint: {goal: 0.02}
      wrist_1_joint: {goal: 0.02}
      wrist_2_joint: {goal: 0.02}
      wrist_3_joint: {goal: 0.02}

gripper_controller:
  ros__parameters:
    joint: robotiq_85_left_knuckle_joint  # = arm.yaml gripper.driver_joint
```

每关节 goal tolerance 必须显式非零；否则 JTC 会把 Gazebo 硬限位夹到边界的越限目标也报告为 succeeded，probe 分类为 `clamped` 并触发失败门。上述配置让该路径实测为 `aborted`。

**夹爪控制器类型冻结（回应 review P1，已核实 Jazzy deb）**：`GripperActionController`（阶段 5 抓放要 GripperCommand action，MoveIt simple controller manager 也按此对接）——
- **包**：`gripper_controllers`（`<exec_depend>gripper_controllers`、`sudo apt install ros-jazzy-gripper-controllers`）。
- **插件类型（写进 controllers.yaml `type`）**：`position_controllers/GripperActionController`。这是 `ros-jazzy-gripper-controllers` 4.40.1 的 `ros_control_plugins.xml` 实际注册的命名空间（历史遗留，包名≠命名空间）。**不是**说它归 `position_controllers` 包。

**冻结前仍在 Jazzy/Harmonic 实机确认**：
```bash
ros2 control list_controller_types | grep -i gripper   # 期望见 position_controllers/GripperActionController
```
**三个易混项区分**：包 `gripper_controllers`=提供 GripperActionController（本阶段用，插件在 position_controllers/ 命名空间）；包 `position_controllers`=通用 position forward 控制器（另一回事）；`robotiq-controllers`=**真机** Robotiq 激活/驱动控制器（C13/P3 才用，阶段 2 不依赖）——README 阶段 2 段落会写清。

**双写守护**：controllers.yaml 的 `joints` 列表与 `arm.yaml arm.joints` 必然重复（ros2_control 要求列关节名）。用 `test_controllers_config.py` 断言二者逐项相等、夹爪 `joint == arm.yaml gripper.driver_joint`、三个控制器名 == arm.yaml `controllers:` 段——把双写钉成"被测试守护的一致性"，非散落魔数（延用阶段 1 arm.yaml↔SRDF 一致性做法）。

### 5.4 启动层：`launch/sim_control.launch.py`（可复现设计，回应 review P1）

目标：任何人照抄能稳定得到"3 控制器 active"。明确每一步：

1. **共享逻辑抽取——但绝不 include 阶段 1 的 `move_group.launch.py`（回应 review）**：那个 launch 会额外起 `robot_state_publisher` + **静态 `joint_state_publisher`**、且硬编码 `use_sim_time=False`（`move_group.launch.py:91,113`）——在仿真里会与 gz 时钟冲突、JSP 会与 `joint_state_broadcaster` 抢发 `/joint_states`。正确做法：把「构造 move_group Node 所需的参数装配」（SRDF/kinematics/OMPL/joint_limits 读取、`robot_description_semantic` 等）抽成 `launch_utils.py` 的**返回参数字典/Node 工厂的 helper**，`sim_control.launch.py` 用它自建一个 move_group Node，参数改为 `use_sim_time=true`；**只起一个 RSP（`use_sim_time=true`），不起 JSP**；`/joint_states` 唯一来源是 `joint_state_broadcaster`。`move_group.launch.py` 改为调用同一 helper（其行为不变：仍 `use_sim_time=False` + JSP，因为它是无控制器的纯 IK/可达性用途）。helper 只共享「参数装配」，不共享「起哪些节点/时钟源」。
2. **robot_description**：`Command(["xacro ", arm_xacro, " sim_gz:=true", " driver_joint:=", <arm.yaml driver_joint>, " controllers_yaml:=", <path>])`，`driver_joint` 由 `load_arm_config()` 注入（运行期以 arm.yaml 为准，§5.1）。
3. **world/SDF**：起 `ros_gz_sim` 的 `gz_sim.launch.py`，server-only `gz_args:="-s -r -v 3 empty.sdf"`（P0 空世界自测，不依赖外部 bringup，PLAN §阶段 1 精神），并用 `ros_gz_bridge parameter_bridge` 单向桥接 `/clock`，供所有 `use_sim_time=true` 节点使用。世界几何来自 URDF spawn，不另建 SDF 世界物体。
4. **robot_state_publisher**：起，喂 `robot_description` + `use_sim_time:=true`。
5. **spawn 机器人**：`ros_gz_sim` 的 `create` 节点，`-topic robot_description -name workbench_arm`。
6. **控制器 spawn 顺序（用事件串，不靠 sleep）**：`joint_state_broadcaster` → `arm_trajectory_controller` → `gripper_controller`，每个是 `controller_manager` 的 `spawner` 节点，后一个由前一个的 `RegisterEventHandler(OnProcessExit)` 触发。**注意 `OnProcessExit` 在 spawner 成功和失败时都会触发**——handler 里必须判 `event.returncode == 0` 才 spawn 下一个；非 0 则发 `Shutdown`（携带失败控制器名），不得在前一个失败时继续往下 spawn（否则串起半截控制器还报 active 假象）。`spawner` 自带对 `~/list_controllers` 服务的等待，避免竞态。
7. **use_sim_time**：所有节点显式 `use_sim_time:=true`（gz 时钟）。
8. **失败退出**：`spawner` 失败即非零退出，launch 传播；文档给出排障顺序（gz 起没起 → controller_manager 插件加载没有 → 控制器类型插件缺失）。

9. **move_group（本阶段随 sim 一起起，为 probe 的碰撞检查供 `/check_state_validity`）**：复用阶段 1 的 move_group 配置（SRDF/kinematics/OMPL/joint_limits），与控制器同一 launch 起来。**"无穿模"证据路径定为 MoveIt `/check_state_validity`**（见 §5.5 说明），不接 Gazebo contact sensor——仓库当前无 contact sensor/bridge，且合并 URDF 已是 MoveIt 的碰撞权威（PLAN §阶段 3）。

产物：`ros2 control list_controllers` 三个均 `active` 即达标（**C2 派生验收**，非 README Metrics——见 §0/§7）。

### 5.5 越限校验：保守前置筛查纯内核（P0-1 按"降级为纯逻辑权威"收口）

**边界定性（对齐 §0）**：`collision_or_limit_violation=0` 是 v0.1 发布线，其 ROS 端到端闭合（唯一 gate + 防绕过 + 命名空间隔离）归阶段 4。阶段 2 **不**建 action proxy、**不**做命名空间隔离——那会提前实现本属阶段 4 的机制、与冻结的 PLAN 阶段划分冲突、且可能在阶段 4 重构。阶段 2 交付：

**(a) 纯逻辑校验内核** `workbench_motion/joint_limits.py`（**无 rclpy**，uv 可测）。**职责边界（回应 review 阻塞项）：阶段 2 只交付纯判定——输入轨迹、输出 `Violation | None`，不产事件、不下发、不碰 EvidenceSink。** 事件生成、零下发、EvidenceSink 闭合全部归阶段 4 的适配器（它有 ROS 上下文、run_id/action_id、唯一入口）。阶段 4 复用**同一个** `check_trajectory`，不重写。
- `load_hard_limits(vendor_description_pkg, ur_type)`：读厂商 `{vendor_description_pkg}/config/{ur_type}/joint_limits.yaml`。含自定义 `!degrees` YAML tag，加 constructor 转弧度。路径参数来自 `arm.yaml` 的 `arm.vendor_description_pkg` / `arm.ur_type`（已存在字段），不写死。得每关节 `(min_pos, max_pos, max_vel, max_eff)`。
- `load_override(path)`：读 `hw_override.yaml`，按 §4 失败语义（空/缺/更松/未知关节/单位错）fail closed。
- `effective_limits(hard, override)`：`hard ∩ override` 取最严。**不接收 scaling 参数**——MoveIt 规划期缩放只留在规划层（路②），不进入这个安全校验器（回应 review 语义冲突问题）。
- `check_trajectory(traj, current_joint_positions: dict[str, float]) -> Violation | None`：**纯判定，绝不 clamp、绝不产生副作用**。接收当前关节状态，构造虚拟"段 0"（`current → points[0]`）检查启动瞬时速度，防单点大跳跃绕过。

**(a′) 校验口径明确（回应 review 阻塞项）**：`check_trajectory` 执行 **① 硬限位 ∩ ③ 真机覆盖**（二者取最严），**不叠加 ② 规划限位**——② 归 MoveIt 规划期，阶段 2 校验器不越界。阶段 4 要保证「发布线收到的轨迹已经过 MoveIt 规划且满足 ②」，但那是调用链保证（只从 MoveIt 接轨迹），非校验器职责。校验内核是**保守前置筛查（pre-filter）**：只查 position/velocity/effort 点值 + 段平均速度（线性口径），**不复现 JTC 的 cubic/quintic 样条区间峰值、不硬检加速度**，能挡住明显越限，但**不足以单独保证「零越限」**。阶段 4 若要据它保证发布线的越限=0，**必须二选一**：① 冻结控制器插值方法并按真实样条计算区间极值再判定；或 ② 补执行期监控（订阅 `/joint_states` 实时查速度/位置越限并触发停机）。此约束写进阶段 4 的待办，PLAN §阶段 4 已承接。

**(b) 实测控制器越限行为（回应 review，不预设结论，六分类）**：直发一条越限 `FollowJointTrajectory` 到 JTC，**观测 `/joint_states` 前后 + action result**，分六类（**不止 clamp/reject/abort**）：
| 分类 | 含义 | gate |
|---|---|---|
| `rejected` | 控制器拒收目标（goal rejected），关节零运动 | 安全 |
| `aborted` | 收下但执行中 abort，未越界 | 安全 |
| `clamped` | 收下、执行，但把越限值**静默截断**到边界（关节停在 bound 内） | **登记为阶段 4 必须消除的绕过风险** |
| `executed_over_limit` | 控制器接受目标且关节**实际越界**（最严重、完全可能） | **fail gate**——发布线 `collision_or_limit_violation=0` 被直接违反，阶段 4 必须消除 |
| `timeout` | action 超时或无 result | **fail gate** |
| `unclassified` | 行为无法归入上述五类（防御性兜底） | **fail gate**——不得当"安全"记 |

实测结果（版本号、分类、`/joint_states` 前后、越界关节与幅度）写进 `docs/evaluation/phase2-controllers.json`（§7）。`clamped` / `executed_over_limit` / `timeout` / `unclassified` 都**不能**被记成"控制器已提供防护"；只有 `rejected` / `aborted`（且确认未越界）才算控制器侧有一层防护，且**无论哪类**，越限=0 的保证都在阶段 4（唯一入口 + 校验内核 + 真实极值/执行期监控），不依赖控制器行为。

**(c) 段平均速度背后的线性插值假设（回应 review 小修项）**：JTC 实际沿 cubic/quintic 样条插值，区间峰值速度 > 段平均；阶段 2 校验器只按线性 `Δpos/Δt` 检查——**能挡住大部分越限，但留缺口（真实样条可能峰值越限）**。缺口由阶段 4 闭合（①冻结插值法+真实极值计算 或 ②执行期监控），PLAN 已承接。

**`check_trajectory` 完整安全语义（回应 review P1，"每点合法≠整条合法"）**，逐条 fail closed，各配攻击性单测：
| 规则 | 判定 |
|---|---|
| `joint_names` 缺失/为空 | 拒 |
| `joint_names` 有重复 | 拒 |
| `joint_names` 含未知关节（不在 arm.yaml joints） | 拒 |
| **partial goal**（只给部分关节） | 拒（阶段 2 要求全关节；宁可拒不可漏检） |
| `points` 为空 | 拒 |
| 某点 `positions` 长度 ≠ `joint_names` | 拒 |
| 可选 `velocities`/`accelerations`/`effort` 长度既非 0 也非关节数 | 拒（要么整条不给，要么与关节一一对应；半截数组模糊对应） |
| **当前状态边界条件（回应 review P0）**：`current_joint_positions` 缺关节/含未知关节/NaN·Inf/自身已越限 | 拒（fail closed：不对有毒当前状态做检查） |
| **当前 → 首点段速度**（防瞬移）：`first.time_from_start < 0` | 拒（时间不能倒退） |
| `first.time_from_start == 0` | 首点 `positions` 必须与 `current_joint_positions` 在 `limit_epsilon` 内一致，否则拒（t=0 意味"立即是当前状态"） |
| `first.time_from_start > 0` | 检查虚拟段 0：`\|first.pos - current.pos\| / first.time_from_start` 越 effective `max_vel` → 拒（单点短时大跳跃会绕过相邻点段平均检查） |
| 位置越 effective `[min,max]`（任一点、任一关节） | 拒，指明关节/值/界 |
| 速度 `|velocity|` 越 effective `max_vel` | 拒 |
| effort 越 `max_eff`（若给） | 拒 |
| 含 NaN/Inf（pos/vel/acc/eff 任一） | 拒（复用 evidence.py 已有的有限性拒绝思路） |
| `time_from_start` 有负值（除首点已检查） | 拒（时间不能倒退到起点之前） |
| `time_from_start` 非**严格**递增（含相邻相等 Δt=0） | 拒（Δt=0 会让段速度除零/无穷，且时序无意义） |
| 相邻点**段平均速度** `Δpos/Δt` 越 `max_vel`（线性插值假设，见下） | 拒（点上 `velocity` 合法但段位移过大——整条轨迹级检查，非逐点） |
| 边界容差 | 用配置化 `limit_epsilon`（默认 1e-6）**只向内收，绝不向外放宽硬限位**。明确公式：`effective_max = max - epsilon`、`effective_min = min + epsilon`、`effective_max_vel = max_vel - epsilon`；判越限用 `value > effective_max` / `value < effective_min`。即 epsilon 把可接受区间**缩小**（更严），绝不写成 `value > max + epsilon`（那是放宽）。速度/effort 同理向内收 |
| 加速度 | 阶段 2 记录字段并可选检查（`max_acceleration` 厂商未公开，②里是保守选值）；不作硬门，注明留阶段 6 |

**段速度检查的插值口径（回应 review 语义不一致）**：阶段 2 **只保证线性插值口径**，检查相邻点的**段平均速度** `Δpos/Δt`，不是 JTC 实际的 cubic/quintic spline 区间真实速度峰值。理由：`check_trajectory` 是 ROS-free 纯逻辑内核，不复现 JTC 的样条插值器；线性段平均速度是**保守下界**（样条峰值 ≥ 线性平均值时才更严，但至少能抓住"两点间位移/时间已超 max_vel"这类整条轨迹级越限）。**明确不声称"段峰值"**——那需要按 JTC 样条算真实峰值，留待后续阶段若接入 JTC 插值器再做。文档、表格、Task Packet 统一用「段平均速度（线性口径）」措辞，不用「segment-peak」。

拒绝路径（**在阶段 4 的适配器里闭合，非阶段 2**）：越限 → 不下发任何指令（零运动来自"根本没发"）→ WARNING 日志（带 run_id）+ 产 `ExecutionEvent`（`event_type="trajectory_rejected"`，payload 含结构化违规）→ `EvidenceSink.append()` 拿稳定 `event_id`。阶段 2 只交付返回 `Violation` 的纯判定内核；上述副作用链归阶段 4（它才有 ROS 上下文与 run_id/action_id）——延用 evidence.py 既定分工。

## 6. 逐文件清单

新增：
- `config/ros2_control.xacro` — ros2_control 宏（复用厂商臂关节接口 + 本包夹爪宏 + Harmonic 插件）+ gz_ros2_control gazebo 插件（§5.1 已给全定义）。
- `config/controllers.yaml` — controller_manager + 臂 JTC + 夹爪 + joint_state_broadcaster（§5.3）。
- `config/joint_limits.hw_override.yaml` — 真机安全覆盖（第③路，C13/P3）；阶段 2 结构在、内容空，含头注说明失败语义。
- `workbench_motion/joint_limits.py` — 纯逻辑：**硬限位 + override 两路加载与合成**（不叠加规划 scaling）、`!degrees` 解析、fail-closed override、`check_trajectory`（§5.5）。无 rclpy。
- `workbench_motion/launch_utils.py` — 从 move_group.launch.py 抽的共享 xacro/路径逻辑。
- `launch/sim_control.launch.py` — Gazebo + spawn + 控制器顺序 spawn + **move_group**（为 probe 供 `/check_state_validity`，复用阶段 1 MoveIt 配置）（§5.4）。
- `test/test_joint_limits.py` — `!degrees`、硬限位加载、override fail-closed 全分支、`check_trajectory` 全规则攻击性用例、"校验器不改输入点"。
- `test/test_controllers_config.py` — controllers.yaml↔arm.yaml 关节/夹爪/控制器名一致。
- `test/test_phase2_probe.py` — probe 的**纯逻辑部分**单测（把 ROS IO 抽成可注入接口，用假响应喂）：越限行为**六分类**（rejected/aborted/clamped/executed_over_limit/timeout/unclassified）判定 + 后四类 fail gate、各类超时、TF/JointState 陈旧（stale）判定、缺 topic/action/service 时非零退出且不产空 JSON、JSON 生成 schema 正确、非 GazeboSimSystem 时拒发越限轨迹（fail-closed）、mimic 比例按增量计算。ROS 依赖用 mock，纯逻辑在 uv 下可测。
- `workbench_motion/phase2_probe.py` + console_scripts 入口 `phase2_probe` — **一条命令产全套证据**（回应 review 工具缺口）。范式同阶段 1 `reachability_check`（ROS 控制台脚本产 JSON）。实现要点（写死路径，不留"可能没数据源"的空洞）：
  - **步骤**：① `controller_manager_msgs/ListControllers` 采三控制器 state；② `tf2_ros.Buffer` 查 `world→base_link→…→tool0→grasp_tcp` 每段 `lookup_transform`，缺段记 `missing`；③ 发**合法** `FollowJointTrajectory`（`control_msgs`），用 **MoveIt `/check_state_validity`（`moveit_msgs/GetStateValidity`）** 判无碰撞（**碰撞证据唯一路径 = MoveIt，不接 Gazebo contact**——仓库无 contact sensor/bridge，合并 URDF 已是 MoveIt 碰撞权威）。`/check_state_validity` **一次只验一个状态**，故必须定义"沿途"分辨率：对每个**实测到的 `/joint_states` 采样**逐个验（执行期实时验），**且**对规划轨迹按固定关节步长 `collision_check_resolution_rad`（默认 0.05 rad）线性加密后逐点验——终点合法≠路径无碰撞，两者都记；任一状态 invalid 即判该轨迹穿模。④ 发**越限** `FollowJointTrajectory`，采 `/joint_states` 前后 + action result **实测**控制器行为，按 §5.5(b) **六分类**（rejected/aborted/clamped/executed_over_limit/timeout/unclassified），后四类 fail gate；⑤ 本地跑 `check_trajectory` 记 `Violation`（无 event_id）；⑥ 夹爪 open→close（`GripperCommand`），采 `/joint_states` 验五从动关节 multiplier 比例（Δfollower/Δdriver）。
  - **fail-closed 安全（回应 review）**：probe 会主动发越限轨迹，**执行前必须确认 hardware 插件是 `gz_ros2_control/GazeboSimSystem`**（查 `robot_description` 里的 `<plugin>` 或 controller_manager 参数）；**不是 GazeboSimSystem 就立即中止、绝不发送**——防止误连未来真机控制器把越限指令打到真臂。
  - **健壮性**：`ListControllers`/action server/`/check_state_validity`/TF 任一超时或缺失 → 记 `unavailable` 并**非零退出**，不写"看似成功"的空 JSON；TF/JointState 时间戳过期（超 `staleness_s`）判 stale 而非静默采旧值。
- `docs/task_packets/motion-002-limits-control.json` — 阶段 2 Task Packet（已随本次落盘，`make task-check` 过）。
- `docs/evaluation/phase2-controllers.json` — 机器可读证据（由 `phase2_probe` 生成，§7）。

修改：
- `config/arm.yaml` — 加 `controllers:` 段（§5.0）；`gripper.driver_joint` 已在，复用。
- `workbench_motion/arm_config.py` — `ArmConfig` 加 `driver_joint`/`update_rate_hz`/三控制器名字段，`parse_arm_config` 解析 + 缺失 fail closed（§5.0）。
- `config/arm_on_workbench.urdf.xacro` — 加 `sim_gz`/`driver_joint`/`controllers_yaml` arg + 条件 include（默认 false，阶段 1 零回归）。
- `launch/move_group.launch.py` — 改用 `launch_utils.py`（仅去重）。
- `package.xml` — 加 exec_depend：
  - 控制/仿真运行时：`gz_ros2_control`、`controller_manager`、`joint_trajectory_controller`、`joint_state_broadcaster`、`gripper_controllers`、`ros_gz_sim`、`ros_gz_bridge`（仅桥 `/clock`）。
  - **`phase2_probe` 用到的消息/客户端库**（回应 review：先前漏列）：`control_msgs`（FollowJointTrajectory / GripperCommand action）、`trajectory_msgs`（JointTrajectory）、`controller_manager_msgs`（ListControllers 服务）、`tf2_ros`（帧链查询）、`moveit_msgs`（GetStateValidity 服务，已在阶段 1 依赖）、`sensor_msgs`（JointState，已在）。**不引 Gazebo contact bridge**——碰撞证据走 MoveIt（见下）；`ros_gz_bridge` 仅用于 `/clock`。
- `test/test_arm_config.py` — 断言新增控制器/driver_joint 字段。
- `README.md` — 阶段 2 段落：apt 前置、`sim_control` 起法、`list_controllers` 验证、夹爪控制器类型澄清（vs robotiq-controllers）、越限校验复现命令；换臂清单加 controllers.yaml 关节列表 + hw_override 两项。
- `setup.py` — data_files 现有 glob 已覆盖新 `.xacro`/`.yaml`/`launch`；`console_scripts` 加 `phase2_probe = workbench_motion.phase2_probe:main`。
- `config/moveit/joint_limits.yaml`（阶段 1 交付物，本阶段仅**改注释**不改值）— 现注释 `# UR5e nominal` **不实**：厂商 `ur_description` 3.5.1 的 shoulder_pan/lift 是 **180°/s**，我们写 120°/s 是**主动收紧**。改注释为"主动收紧值（厂商 nominal 180°/s，规划层保守取 120°/s），来源与理由见此注 + `arm.yaml`"，如实记录。

## 7. 验收映射（挂官方标准）+ 机器可读证据

| 验收 | 官方锚点 | 本方案 | 证据（稳定，非"日志行"） |
|---|---|---|---|
| 3 控制器 `active` | **C2（项目派生，非 README Metrics）** | §5.3+§5.4 | `phase2-controllers.json`.controllers[] state |
| **TF 帧链完整**（C2 明列 URDF/**TF**/控制器/限位） | **C2** | §5.4 rsp + spawn | `/tf` + `/tf_static` 采样，断言 `world→base_link→…→tool0→grasp_tcp` 链完整入 json |
| 合法 `FollowJointTrajectory` 平滑到位、**无穿模** | CONSTRAINTS `collision_or_limit_violation=0` | §5.1 gz system + JTC | 关节前后状态 + **MoveIt `/check_state_validity` 结果（唯一路径，逐采样+加密点）**，不只靠无 warning，入 json |
| **校验内核（保守前置筛查）**判定越限（不 clamp）+ 实测控制器越限行为 | 同上（发布线，端到端 + 真实极值/监控归阶段 4） | §5.5 `check_trajectory` | 单测输出 + `Violation` + **六分类实测(rejected/aborted/clamped/executed_over_limit/timeout/unclassified)**，后四类 fail gate，入 json |
| xacro 展开含正确插件/接口、单树 | C2 | §5.2 展开断言 | `check_urdf` 输出 |
| **夹爪 mimic 比例关系**（open→close 后五从动关节按 multiplier 联动） | C2/C3 前置 | §5.2 spike + 验收 | `phase2-controllers.json`.gripper_mimic |
| override ⊆ hard、命名一致 | — | §4/§5.0/§5.3 单测 | 单测输出 |
| `sim_gz=false` 回归 | 阶段 1 门 | §5.1 门控 | 可达性 gate 仍过 |

> **关于官方锚点的诚实标注（回应 review）**：README「Metrics」表**只有三条 Safety（False completion / Collisions·joint-limit-violations / raw joint control 均=0）+ Task/Evidence/System**，**没有"controllers active"这一行**。"3 控制器 active"和"TF 帧链"是 **C2**（`05-机械臂资产.md:13`「URDF/TF/控制器/关节限位独立 launch 能加载」）的验收，属**项目派生**，不冒充 README 指标。

**`docs/evaluation/phase2-controllers.json`（回应 review P1「日志不是稳定证据」）** 至少记录（镜像 phase1-reachability.json 风格）：
```
generated_at, commit(git rev-parse HEAD), versions{ros_distro, gz, gz_ros2_control, jtc, gripper_controllers},
arm(=arm.yaml arm_label), controllers[{name,type,state}],
tf_chain{expected: ["world","base_link",…,"tool0","grasp_tcp"], present: bool, missing: []},  # C2 的 TF 验收
gripper_mimic{
    tolerance_abs: 0.02,                                    # |observed_ratio - nominal| ≤ tol 判 ok
    driver_joint,
    followers[{
        joint,
        nominal_multiplier,                                 # 来自 URDF <mimic multiplier="...">
        observed_ratio,                                     # Δfollower / Δdriver（开→关增量比，非绝对位置比）
        ok: bool                                            # |observed - nominal| ≤ tolerance_abs
    }]},
legal_trajectory{
    tracking_tolerance_rad: 0.05,                           # |final - target| ≤ tol 判 tracking_ok
    joint_states_before, joint_states_after, tracking_ok,
    collision{
        source: "moveit",                                   # 唯一路径: /check_state_validity
        resolution_rad: 0.05,                               # 固定（冻结）：线性插值加密到 ≤0.05 rad 关节步
        states_checked: N,                                  # 轨迹点加密后数量 + 实测 /joint_states 采样数
        all_valid: bool,
        first_invalid: {...}|null
    }},
observed_controller_over_limit_behavior{                   # 实测六分类，不预设
    kind: "rejected"|"aborted"|"clamped"|"executed_over_limit"|"timeout"|"unclassified",
    joint_states_before, joint_states_after, over_limit_joints: [{joint, value, bound}],
    gate: "safe"|"fail",                                   # clamped/executed_over_limit/timeout/unclassified => fail
    is_phase4_bypass_risk: bool},                          # clamped/executed_over_limit 时 true
validator_violation{joint, kind, value, bound}             # check_trajectory 返回的纯 Violation（无 event_id：事件归阶段 4）
```

合法 arm 动作现在必须先通过 `preflight_trajectory`，再由
`AcceptedTrajectoryExecutor` 交给 `GazeboTrajectoryController`；报告中的
`legal_trajectory.execution_path=accepted_trajectory_adapter` 与
`legal_trajectory.execution` 保存 gate、state/context hash、action 状态、fresh
feedback sequence/timestamp、最大位置/速度误差及 `verified` / `not_converged`。
越限动作的 `execution_path=raw_safety_probe` 仍是独立安全探针，不得作为正常轨迹执行证据。`verified` 只表示
真实 Gazebo feedback 已刷新并满足控制器收敛阈值，不表示 WorldState 已验证任务成功。
关节前后状态从 `/joint_states` 采。**无穿模证据路径唯一定为 MoveIt `/check_state_validity`**（仓库无 Gazebo contact sensor/bridge，合并 URDF 是 MoveIt 碰撞权威）——`/joint_states` + 无 warning 不足以证明无穿模（review 小修项）。**一条命令产全套证据**：见 §6 的 `phase2_probe` 控制台脚本。

**probe 健壮性（回应 review）**：
- JSON 原子写：先写临时文件，校验 schema 通过后 `os.replace()` 覆盖目标。六分类一旦完成就发布证据：`clamped` 的 controller-protection gate 仍为 fail 且标记 phase-4 bypass risk，但按 Issue #116 不阻塞 phase-2；`executed_over_limit`/`timeout`/`unclassified` 发布诊断证据后退出 1。端点、TF、合法轨迹、碰撞或 mimic 证据不完整时退出 2 且不发布新 JSON。
- 记录 git 状态：`"git_dirty": bool`（`git status --porcelain` 非空时 true；`git diff-index` 不含未跟踪文件，不够全）。
- 记录关键配置 hash：`"config_hashes": {"arm.yaml": "sha256:...", "controllers.yaml": "sha256:..."}`（防配置漂移后误用旧证据）。
- 碰撞检查插值口径冻结为 0.05 rad 关节步（线性插值加密轨迹点；若后续需匹配 JTC 样条真实路径，留阶段 4/6 升级）。

## 8. 测试清单（随阶段交付，AGENTS.md:7）

纯 Python（`uv run pytest`，无 ROS）：
- `!degrees` tag：360°→2π。
- 硬限位加载：6 关节齐全，pos/vel/eff 边界对得上厂商表。
- override fail-closed **全分支**：空文件→放行；缺关节→放行该关节；更松→报错；未知关节→报错；min>max/非数值→报错。
- `check_trajectory` 全规则（§5.5 表逐行）：合法→None；**当前→首点段速度越限→拒**；越 pos/vel/eff→违规且定位；缺/空/重复/未知/partial joint_names→拒；空 points；positions 长度不匹配；**可选 velocities/accelerations/effort 长度非 0 且非关节数→拒**；NaN/Inf；**`time_from_start` 负值 / 非严格递增（含 Δt=0）→拒**；**段平均速度（线性口径）`Δpos/Δt` 越 max_vel**；**`limit_epsilon` 不向外放宽硬限位**（越 bound 的值不因 epsilon 被判合法）；**断言不修改输入点（不 clamp）**。
- controllers.yaml ↔ arm.yaml：关节列表、夹爪关节、三控制器名一致。
- arm_config：新字段解析（vendor/controllers）+ 缺失 fail closed。
- `phase2_probe` 纯逻辑（mock ROS IO）：越限六分类（rejected/aborted/clamped/executed_over_limit/timeout/unclassified）+ 后四类 fail gate、超时、TF/JointState stale、缺 topic/action/service→非零退出不产空 JSON、JSON schema、非 GazeboSimSystem→拒发越限（fail-closed）、mimic 比例按增量计算。

ROS/仿真（需 §3 apt 装齐；PR 附本地日志 + phase2-controllers.json，同阶段 1 可达性门做法）：
- `colcon build` 通过。
- xacro `sim_gz:=true` 展开断言（§5.2）+ `check_urdf`。
- `list_controllers` 三个 active。
- 合法 `FollowJointTrajectory` 到位（`/joint_states` 前后）；无穿模由 **MoveIt `/check_state_validity`** 佐证（唯一路径）。
- 越限行为实测（**六分类**，不预设）：`rejected`/`aborted`/`clamped`/`executed_over_limit`/`timeout`/`unclassified`，后四类 fail gate。
- 直发越限轨迹 → **实测并记录**控制器行为（六分类，不预设；后四类 fail gate）；经校验内核 → 返回 `Violation`（**阶段 2 无 event_id——事件归阶段 4**）。
- `sim_gz=false`：move_group + 可达性 gate 仍过（阶段 1 零回归）。
- 根 `make lint/test/contract/scenario-check/context-check` 全绿（AGENTS.md 四项检查齐 + lint）。

## 9. 卡点与风险

- **apt 未装齐（§3）**：真阻塞，先补装。纯逻辑单测先行不受阻。
- **gz_ros2_control mimic 支持**：2F-85 靠 mimic 联动，需确认 Harmonic 下 gz_ros2_control 正确处理 `<mimic>`。开工第一步做 §5.2 的最小 spike。**不通过则回退到"在 `<ros2_control>` 里显式声明五个从动关节 + multiplier"（照厂商宏关节列表，插件换 GazeboSimSystem），与 §5.2 fallback 一致**——不是"只控驱动关节"。记入 PR。
- **夹爪控制器插件全名**：§5.3 —— 包 `gripper_controllers`，插件类型 `position_controllers/GripperActionController`（命名空间历史遗留，已核 Jazzy deb）；**冻结前仍须 pluginlib 实际加载确认**，以真实可加载名落定 controllers.yaml + package.xml。
- **update_rate 500Hz**：需与 gz 物理步长匹配，值冻结入 controllers.yaml，实测可调（不留裸魔数）。
- **越限=0 归属**：阶段 2 只交校验内核 + 反面证据；端到端唯一入口/防绕过/命名空间隔离归阶段 4，复用**同一** `check_trajectory`，不重写（§0/§5.5）。

## 10. Task Packet

`docs/task_packets/motion-002-limits-control.json`（已随本文件落盘）：
- issue 5、human_owner `cyathea152-bit`（沿用 motion-001）。
- allowed_paths：`robot/control/**`、`docs/task_packets/motion-002-limits-control.json`、`docs/evaluation/phase2-controllers.json`。
- read_only：`interfaces/**`、`robot/description/**`、`docs/context/**`、`AGENTS.md`。
- forbidden：`firmware/**`、`services/**`、`interfaces/**`、`robot/description/**`。
- acceptance/commands/evidence/stop_conditions 照 §7/§8/§9 填。
- 校验：`make task-check PACKET=docs/task_packets/motion-002-limits-control.json` 通过。

## 11. 分支 / PR 策略（回应 review git 卫生项）

现状：仍在 `feat/motion-phase1-arm`，阶段 1 PR **未合并**，`PHASE2.md` + `motion-002-*.json` 两个文件当前 untracked。**不要把阶段 2 代码堆进阶段 1 的 PR**（污染 review、放大 diff）。二选一：
- **推荐：等阶段 1 PR 合并后，从更新的 `main` 新开 `feat/motion-phase2-limits-control`。** 阶段 2 依赖阶段 1 的合并 URDF/arm.yaml/arm_config，从合并后的 main 起最干净。
- **次选：stacked PR。** 若阶段 1 短期不合并且阶段 2 要并行，`feat/motion-phase2-limits-control` 从 `feat/motion-phase1-arm` 切出，PR 显式声明 base 为阶段 1 分支（不是 main），阶段 1 合并后 rebase 到 main。

无论哪种，本轮这两个规划文档（PHASE2.md / Task Packet）属**规划产物**：可先随阶段 1 分支带上，或单独一个 docs-only commit——但阶段 2 的**代码**（xacro/yaml/py）必须落在阶段 2 分支，不混进阶段 1。
