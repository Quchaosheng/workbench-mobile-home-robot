# Third-Party Dependencies and Acknowledgements

This repository uses third-party software through ROS 2, Ubuntu packages, and
public operating-system interfaces. It does not vendor the upstream source
trees listed below. Each dependency remains governed by its own license.

This inventory is informational and does not replace upstream license texts.
Before distributing a binary, image, or container, review the exact versions,
transitive packages, notices, and source-offer obligations included in that
artifact.

## Runtime and Build Dependencies

| Project | Use in this repository | Upstream license |
| --- | --- | --- |
| [ROS 2 Jazzy](https://docs.ros.org/en/jazzy/) | Nodes, Actions, messages, launch tests, ament, and package discovery | Predominantly Apache-2.0; verify each installed ROS package |
| [BehaviorTree.CPP](https://github.com/BehaviorTree/BehaviorTree.CPP) | Fixed workflow orchestration in `task_orchestrator` | MIT |
| [gRPC](https://github.com/grpc/grpc) | Loopback RPC transport in `runtime_gateway` | Apache-2.0 |
| [Protocol Buffers](https://github.com/protocolbuffers/protobuf) | Gateway service and message serialization | BSD-3-Clause |
| [SQLite](https://sqlite.org/) | Durable task history in `runtime_history` | Public domain |
| [OpenCV](https://opencv.org/) | Image and USB-camera input plus ArUco detection | Apache-2.0 for current OpenCV 4 releases; verify the installed version |
| [Linux SocketCAN](https://docs.kernel.org/networking/can.html) | CAN raw sockets and the `vcan0` software interface | Linux kernel UAPI: GPL-2.0-only WITH Linux-syscall-note |

## Test, Tooling, and CI Dependencies

| Project | Use in this repository | Upstream license |
| --- | --- | --- |
| [GoogleTest](https://github.com/google/googletest) | C++ unit and integration tests | BSD-3-Clause |
| [pytest](https://github.com/pytest-dev/pytest) | Python and launch-oriented tests invoked through ament | MIT |
| [colcon](https://colcon.readthedocs.io/) | ROS workspace build and test orchestration | Apache-2.0 for the core packages; verify installed extensions |
| [rosdep](https://github.com/ros-infrastructure/rosdep) | System dependency resolution | BSD-3-Clause |
| [can-utils](https://github.com/linux-can/can-utils) | CAN traffic in the software `vcan0` smoke test | GPL-2.0-only |
| [ros-tooling/setup-ros](https://github.com/ros-tooling/setup-ros) | GitHub Actions ROS environment setup | Apache-2.0 |

The repository's Apache-2.0 license covers its own source. It does not relicense
third-party packages, operating-system components, or external services.
