"""Spawn the Revision D description in Gazebo and start its controllers.

This launch file is deliberately explicit about what it does and does not do.
It spawns the model and the named controllers. It exposes no unguarded
trajectory-execution path: there is no joint-group passthrough, no topic relay
that would let a caller command the lift directly, and the STOP / safe-stop
boundary stays with Motion and the safety controller (#327 acceptance).

Requires a sourced ROS 2 workspace with ros_gz_sim and gz_ros2_control; without
them the launch fails loudly rather than starting a half-configured simulator.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    RegisterEventHandler,
    Shutdown,
)
from launch.conditions import IfCondition
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node

DESCRIPTION_DIR = Path(__file__).resolve().parents[1]
MODEL_NAME = "workbench_revision_d"


def _require(path: Path, what: str) -> Path:
    if not path.is_file():
        raise RuntimeError(f"Revision D launch cannot find the {what}: {path}")
    return path


def _expand(description: Path, sim_gz: bool, controllers: Path) -> str:
    """Expand the xacro ourselves so a failure is a message, not a spawn error."""
    import xacro

    document = xacro.process_file(
        str(description),
        mappings={
            "sim_gz": "true" if sim_gz else "false",
            "controllers_yaml": str(controllers),
            "ros2_control_xacro": str(description.parent / "revision_d_ros2_control.xacro"),
        },
    )
    return document.toxml()


def _next_if_success(failed_process: str, next_action=None):
    def handler(event, _context):
        if event.returncode == 0:
            return [next_action] if next_action is not None else []
        return [Shutdown(reason=f"{failed_process} failed with return code {event.returncode}")]

    return handler


def _setup(context, *_args, **_kwargs):
    if shutil.which("gz") is None and shutil.which("ign") is None:
        raise RuntimeError(
            "Revision D launch needs a Gazebo installation on PATH. "
            "Absent simulator is not a pass: the Gazebo acceptance cases stay NOT_EXECUTED."
        )

    description = _require(DESCRIPTION_DIR / "revision_d.urdf.xacro", "Revision D xacro")
    controllers = _require(DESCRIPTION_DIR / "revision_d_controllers.yaml", "Revision D controllers.yaml")
    ros_gz_share = Path(get_package_share_directory("ros_gz_sim"))
    world = LaunchConfiguration("world").perform(context)
    common_time = {"use_sim_time": True}

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(str(ros_gz_share / "launch" / "gz_sim.launch.py")),
        launch_arguments={"gz_args": f"-s -r -v 3 {world}"}.items(),
    )
    clock_bridge = Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        output="screen",
        arguments=["/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock"],
    )
    state_publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[{"robot_description": _expand(description, True, controllers)}, common_time],
    )
    spawn = Node(
        package="ros_gz_sim",
        executable="create",
        output="screen",
        arguments=["-topic", "robot_description", "-name", MODEL_NAME, "-allow_renaming", "false"],
    )

    def spawner(name: str) -> Node:
        return Node(
            package="controller_manager",
            executable="spawner",
            output="screen",
            arguments=[
                name,
                "--controller-manager",
                "/controller_manager",
                "--controller-manager-timeout",
                "60",
            ],
        )

    joint_state_broadcaster = spawner("joint_state_broadcaster")
    lift = spawner("lift_trajectory_controller")
    left_arm = spawner("left_arm_trajectory_controller")
    right_arm = spawner("right_arm_trajectory_controller")

    return [
        DeclareLaunchArgument(
            "world",
            default_value="empty.sdf",
            description="Gazebo world. The neutral default keeps this launch a wiring test.",
        ),
        DeclareLaunchArgument(
            "gui",
            default_value="false",
            description="Gazebo GUI. Off by default so CI does not need a display.",
        ),
        gazebo,
        clock_bridge,
        state_publisher,
        spawn,
        # Each controller waits for the previous one: spawning them in parallel
        # races the controller manager and reports a flaky failure that looks
        # like a model problem.
        RegisterEventHandler(
            OnProcessExit(
                target_action=spawn,
                on_exit=_next_if_success("robot spawn", joint_state_broadcaster),
            )
        ),
        RegisterEventHandler(
            OnProcessExit(
                target_action=joint_state_broadcaster,
                on_exit=_next_if_success("joint_state_broadcaster", lift),
            )
        ),
        RegisterEventHandler(OnProcessExit(target_action=lift, on_exit=_next_if_success("lift controller", left_arm))),
        RegisterEventHandler(
            OnProcessExit(target_action=left_arm, on_exit=_next_if_success("left arm controller", right_arm))
        ),
        Node(
            package="rviz2",
            executable="rviz2",
            output="screen",
            parameters=[common_time],
            condition=IfCondition(LaunchConfiguration("gui")),
        ),
    ]


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription([OpaqueFunction(function=_setup)])
