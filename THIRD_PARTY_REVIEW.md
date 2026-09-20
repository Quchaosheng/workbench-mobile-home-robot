# Third-party review register

| Dependency or asset | Version / digest | License checked | Owner | Exit path |
|---|---|---:|---|---|
| ROS 2 Jazzy | pending | pending | Linux | pinned container |
| Gazebo Harmonic | pending | pending | Linux + Simulation | single simulator baseline |
| JetPack 6.2.1 / Jetson Linux 36.4.4 | NVIDIA vendor distribution | redistribution terms pending | Linux | pin vendor package and retain license notice |
| MoveIt 2 | `ros-jazzy-moveit` 2.12.4 | Apache-2.0 (checked 2026-08-08) | Motion | fixed validated trajectory |
| UR description (arm URDF/xacro) | `ros-jazzy-ur-description` | **BSD-3-Clause (code) — OK** | Motion | primitive-only description |
| UR meshes (visual/collision STL/DAE) | shipped in `ur_description/meshes` | **PROPRIETARY: "Universal Robots A/S' Terms and Conditions for Use of Graphical Documentation" — NOT open-source; distribution unverified** | Motion + Legal | drop visual meshes / use primitive collision geometry, or swap to Panda (config-only, see ADR-0004) |
| Robotiq 2F-85 gripper (description) | `ros-jazzy-robotiq-description` | BSD (checked 2026-08-08) | Motion | primitive-only gripper description |
| TRAC-IK kinematics plugin | `ros-jazzy-trac-ik-kinematics-plugin` | BSD (upstream trac_ik) — reconfirm at pin | Motion | fall back to KDL (ships with MoveIt) |
| UFACTORY xArm ROS 2 packages | `xArm-Developer/xarm_ros2` (`humble`) | BSD-3-Clause (public repository) | Motion + Integration | keep vendor controller behind adapter; confirm hardware and firmware terms |
| OpenCV / AprilTag | pending | pending | Perception Owner | known-object baseline |
| Ollama runtime image | `sha256:b88c73ace3e115f8ec53dc8761ae1c0aabfa675406e3681786b98757ce050f42` | Apache-2.0 (verify at release) | Runtime + Integration | localhost-only endpoint and internal network |
| Qwen2.5 0.5B weights | `qwen2.5:0.5b` (397 MB pulled locally) | pending model-card review | Runtime + Product | remove model profile and use template runner |
| Lucide icons | 0.468.0 | ISC (`apps/dashboard/vendor/LUCIDE-LICENSE.txt`) | Interaction | replace with text labels |
| MonoSim | invited access; version pending | use invitation recorded; license and redistribution terms pending | Simulation + Integration | keep behind an external adapter; remove from release if terms do not permit distribution |
| RLSOK | version pending; no release agreed | no written terms recorded; maintainer contacted us by email 2026-09-03 | Simulation + Integration | keep behind an external adapter; remove from release if terms do not permit distribution |
| LeRobot | `huggingface/lerobot` (`main`) | **Apache-2.0 (code) — OK**; model weights, datasets and checkpoints reviewed separately | Evaluation + Integration | keep behind an adapter; use an optional extra; never a mandatory core dependency (see [ADR-0008](docs/decisions/ADR-0008-learning-data-plane.md)) |
| StarVLA | `starVLA/starVLA` (`master`) | **MIT (code) — OK**; model weights, datasets and checkpoints reviewed separately | Evaluation + Safety | prefer a process/HTTP policy boundary over vendoring; drop the adapter when ML dependencies are absent |
| RLSOK public repository | `realitywarden/rlsok` (`main`) | Apache-2.0 (public repository) | Safety + Integration | verify release version and hosted-service terms before deployment |

LeRobot and StarVLA code licenses were checked on 2026-09-20 (Apache-2.0 and MIT respectively). Those are code licenses only. Model weights, datasets and checkpoints carry their own terms, are not covered by the code license, and must be reviewed separately before adoption. Do not vendor either project's source, weights or datasets; keep the integration behind an adapter or process boundary, and keep the deterministic scripted runner and existing verifier usable when the ML dependencies are absent. See [ADR-0008](docs/decisions/ADR-0008-learning-data-plane.md).

Model weights, CAD, mesh, images, audio and code are reviewed separately.

MonoSim and RLSOK are unaffiliated third-party tools, not
co-created project assets. Do not vendor their source, models or datasets, or
claim joint development, until the maintainers confirm the applicable license,
publication and redistribution terms in writing.
