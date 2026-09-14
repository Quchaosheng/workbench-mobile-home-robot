"""Gazebo Harmonic + ros2_control + MoveIt bringup for phase-2 evidence."""

from __future__ import annotations

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
from workbench_motion.arm_config import load_arm_config
from workbench_motion.launch_utils import move_group_parameters, require_file, robot_description


def _next_if_success(failed_process: str, next_action=None):
    def handler(event, _context):
        if event.returncode == 0:
            return [next_action] if next_action is not None else []
        return [Shutdown(reason=f"{failed_process} failed with return code {event.returncode}")]

    return handler


def _setup(_context, *_args, **_kwargs):
    share = Path(get_package_share_directory("workbench_motion"))
    ros_gz_share = Path(get_package_share_directory("ros_gz_sim"))
    xacro = require_file(share / "config" / "arm_on_workbench.urdf.xacro", "arm xacro")
    controllers = require_file(share / "config" / "controllers.yaml", "controllers.yaml")
    arm = load_arm_config(share / "config" / "arm.yaml")
    description = robot_description(
        xacro,
        {
            "sim_gz": "true",
            "driver_joint": arm.driver_joint,
            "controllers_yaml": str(controllers),
        },
    )
    common_time = {"use_sim_time": True}

    gazebo = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(str(ros_gz_share / "launch" / "gz_sim.launch.py")),
        launch_arguments={"gz_args": "-s -r -v 3 empty.sdf"}.items(),
    )
    clock_bridge = Node(
        package="ros_gz_bridge",
        executable="parameter_bridge",
        output="screen",
        arguments=["/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock"],
    )
    rsp = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        output="screen",
        parameters=[description, common_time],
    )
    spawn = Node(
        package="ros_gz_sim",
        executable="create",
        output="screen",
        arguments=["-topic", "robot_description", "-name", "workbench_arm", "-allow_renaming", "false"],
    )
    move_group = Node(
        package="moveit_ros_move_group",
        executable="move_group",
        output="screen",
        parameters=move_group_parameters(share, description, use_sim_time=True),
    )

    def spawner(name: str) -> Node:
        return Node(
            package="controller_manager",
            executable="spawner",
            output="screen",
            arguments=[name, "--controller-manager", "/controller_manager", "--controller-manager-timeout", "60"],
        )

    jsb = spawner(arm.joint_state_broadcaster)
    arm_controller = spawner(arm.arm_trajectory_controller)
    gripper = spawner(arm.gripper_controller)
    gui = IncludeLaunchDescription(
        PythonLaunchDescriptionSource(str(ros_gz_share / "launch" / "gz_sim.launch.py")),
        launch_arguments={"gz_args": "-g -v 3"}.items(),
        condition=IfCondition(LaunchConfiguration("gui")),
    )
    rviz = Node(
        package="rviz2",
        executable="rviz2",
        output="screen",
        arguments=["-d", str(require_file(share / "config" / "motion.rviz", "motion RViz config"))],
        parameters=[common_time],
        condition=IfCondition(LaunchConfiguration("rviz")),
    )
    return [
        gui,
        rviz,
        clock_bridge,
        gazebo,
        rsp,
        spawn,
        move_group,
        RegisterEventHandler(OnProcessExit(target_action=spawn, on_exit=_next_if_success("robot spawn", jsb))),
        RegisterEventHandler(
            OnProcessExit(
                target_action=jsb,
                on_exit=_next_if_success(arm.joint_state_broadcaster, arm_controller),
            )
        ),
        RegisterEventHandler(
            OnProcessExit(
                target_action=arm_controller,
                on_exit=_next_if_success(arm.arm_trajectory_controller, gripper),
            )
        ),
        RegisterEventHandler(OnProcessExit(target_action=gripper, on_exit=_next_if_success(arm.gripper_controller))),
    ]


def generate_launch_description() -> LaunchDescription:
    return LaunchDescription(
        [
            DeclareLaunchArgument("gui", default_value="false", description="Gazebo GUI for this simulation server"),
            DeclareLaunchArgument("rviz", default_value="false", description="RViz RobotModel and TF"),
            OpaqueFunction(function=_setup),
        ]
    )
