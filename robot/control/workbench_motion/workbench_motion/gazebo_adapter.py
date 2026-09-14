"""Gazebo-only ROS transport. All ROS messages remain inside this adapter."""

from __future__ import annotations

import math
import time
from collections import deque

from workbench_motion.arm_config import ArmConfig
from workbench_motion.joint_limits import AcceptedTrajectory
from workbench_motion.motion_safety import sample_trajectory
from workbench_motion.motion_types import RobotState
from workbench_motion.phase2_probe import RosProbeIO, hardware_is_gazebo
from workbench_motion.trajectory_executor import digest


def decode_joint_state(
    message, *, robot_id: str, joint_names: tuple[str, ...], previous: RobotState | None = None
) -> RobotState:
    names = tuple(message.name)
    if len(set(names)) != len(names) or len(message.position) != len(names) or len(message.velocity) != len(names):
        raise ValueError("incomplete or duplicate JointState")
    indices = [names.index(joint) for joint in joint_names]
    stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
    if message.header.stamp.sec < 0 or not 0 <= message.header.stamp.nanosec < 1_000_000_000:
        raise ValueError("invalid JointState timestamp")
    q = tuple(message.position[index] for index in indices)
    v = tuple(message.velocity[index] for index in indices)
    acceleration = None
    if previous:
        dt = stamp - previous.observed_at_s
        if dt <= 0:
            raise ValueError("non-increasing JointState timestamp")
        if 0.02 <= dt <= 0.1:
            acceleration = tuple((a - b) / dt for a, b in zip(v, previous.velocities, strict=True))
    return RobotState(robot_id, joint_names, q, v, stamp, "ros_sim", acceleration)


def trajectory_message(trajectory: AcceptedTrajectory, message_type, point_type):
    if not isinstance(trajectory, AcceptedTrajectory):
        raise TypeError("only AcceptedTrajectory may be materialized")
    message = message_type()
    message.joint_names = list(trajectory.snapshot.joint_names)
    for source in trajectory.snapshot.points:
        point = point_type()
        point.positions = list(source.positions)
        point.velocities = list(source.velocities)
        point.accelerations = list(source.accelerations)
        point.time_from_start.sec, point.time_from_start.nanosec = divmod(source.time_from_start_ns, 1_000_000_000)
        message.points.append(point)
    return message


class GazeboAdapter:
    """FJT action transport with live scene snapshots and typed feedback.

    This is an execution transport, not a complete SimulatorBackend. The phase-2
    probe provides reusable read-only ROS services; its raw execution methods
    are never used by this adapter.
    """

    def __init__(self, arm: ArmConfig, *, max_velocity: float, timeout_s: float = 0.5):
        from moveit_msgs.srv import GetPlanningScene
        from rclpy.parameter_client import AsyncParameterClient
        from sensor_msgs.msg import JointState

        self.arm = arm
        self.robot_id = arm.arm_label
        self.probe = RosProbeIO(arm, timeout_s=timeout_s)
        self.node = self.probe.node
        self.max_velocity = max_velocity
        self._states: deque[RobotState] = deque(maxlen=128)
        self._latest: RobotState | None = None
        self._clock_fault = False
        self._handles = {}
        self._results = {}
        self._pending_send = None
        self._late_result = None
        self._late_rejected = False
        self.send_count = 0
        self._description = None
        self.scene_client = self.node.create_client(GetPlanningScene, "/get_planning_scene")
        self.controller_parameters = AsyncParameterClient(self.node, f"/{arm.arm_trajectory_controller}")
        self.manager_parameters = AsyncParameterClient(self.node, "/controller_manager")
        self.node.create_subscription(JointState, "/joint_states", self._on_state, 50)

    def _on_state(self, message):
        try:
            stamp = message.header.stamp.sec + message.header.stamp.nanosec / 1e9
            if self._latest and stamp < self._latest.observed_at_s:
                self._clock_fault = True
            if self._latest and stamp == self._latest.observed_at_s:
                return
            previous = next((s for s in reversed(self._states) if stamp - s.observed_at_s >= 0.04), None)
            state = decode_joint_state(message, robot_id=self.robot_id, joint_names=self.arm.joints, previous=previous)
            self._latest = state
            self._states.append(state)
        except (ValueError, TypeError, OverflowError):
            self._latest = None

    def spin(self, timeout_s: float = 0.005):
        self.probe.rclpy.spin_once(self.node, timeout_sec=timeout_s)

    def now_s(self) -> float:
        # JointState carries embedded simulator time; DDS /clock can arrive
        # later. Both are the same clock domain. Stalled feedback is still
        # rejected by the executor's independent wall watchdog.
        clock = self.node.get_clock().now().nanoseconds / 1e9
        return max(clock, self._latest.observed_at_s) if self._latest else clock

    def read_state(self) -> RobotState | None:
        if self._clock_fault:
            raise RuntimeError("Gazebo clock reset requires a new execution session")
        return self._latest

    def readiness(self) -> bool:
        if self._pending_send is not None:
            return False
        if not all(self.probe.endpoint_status().values()) or not self.scene_client.wait_for_service(timeout_sec=0.5):
            return False
        description = self.probe.robot_description()
        if not hardware_is_gazebo(description):
            raise RuntimeError("M1 transport refuses non-Gazebo hardware")
        if self._description is not None and self._description != description:
            raise RuntimeError("robot description changed during session")
        self._description = description
        clock = self.probe._spin(self.manager_parameters.get_parameters(["workbench_sim_time_clock"]))
        if clock is None or len(clock.values) != 1 or clock.values[0].type != 1 or not clock.values[0].bool_value:
            return False
        active = {item["name"] for item in self.probe.controller_states() if item["state"] == "active"}
        if active != {
            self.arm.joint_state_broadcaster,
            self.arm.arm_trajectory_controller,
            self.arm.gripper_controller,
        }:
            return False
        names = [
            "command_interfaces",
            "state_interfaces",
            "interpolation_method",
            "interpolate_from_desired_state",
            "allow_partial_joints_goal",
        ]
        response = self.probe._spin(self.controller_parameters.get_parameters(names))
        if response is None or len(response.values) != len(names):
            return False
        command, state, interpolation, desired, partial = response.values
        return (
            [value.type for value in response.values] == [9, 9, 4, 1, 1]
            and list(command.string_array_value) == ["position"]
            and {"position", "velocity"}.issubset(state.string_array_value)
            and interpolation.string_value == "splines"
            and not desired.bool_value
            and not partial.bool_value
        )

    def scene_revision(self) -> str:
        from moveit_msgs.msg import PlanningSceneComponents
        from moveit_msgs.srv import GetPlanningScene
        from rosidl_runtime_py.convert import message_to_ordereddict

        request = GetPlanningScene.Request()
        request.components.components = (
            PlanningSceneComponents.WORLD_OBJECT_GEOMETRY
            | PlanningSceneComponents.OCTOMAP
            | PlanningSceneComponents.TRANSFORMS
            | PlanningSceneComponents.ALLOWED_COLLISION_MATRIX
            | PlanningSceneComponents.LINK_PADDING_AND_SCALING
            | PlanningSceneComponents.ROBOT_STATE_ATTACHED_OBJECTS
        )
        response = self.probe._spin(self.scene_client.call_async(request))
        if response is None:
            raise TimeoutError("planning scene unavailable")
        scene = message_to_ordereddict(response.scene)
        # JointState and scene timestamps advance without geometry changing.
        # Geometry, ACM, padding, scales, attachments and fixed transforms bind
        # the actual collision context instead of the moving arm state.
        payload = {key: scene[key] for key in ("world", "allowed_collision_matrix", "link_padding", "link_scale")}
        payload["attached"] = scene["robot_state"]["attached_collision_objects"]
        payload["transforms"] = [
            {"parent": t["header"]["frame_id"], "child": t["child_frame_id"], "transform": t["transform"]}
            for t in scene["fixed_frame_transforms"]
        ]
        # Octomap timestamps are transport metadata; its data and frame remain.
        if "octomap" in payload["world"]:
            octomap = payload["world"]["octomap"]["octomap"]
            octomap["header"].pop("stamp", None)
        payload["robot_description"] = self._description
        return digest(payload)

    def check_path(self, trajectory: AcceptedTrajectory, resolution: float) -> bool:
        duration = trajectory.snapshot.points[-1].time_from_start_ns / 1e9
        count = max(2, math.ceil(duration * self.max_velocity / resolution))
        if count > 10000:
            raise ValueError("collision sample budget exceeded")
        states = [
            dict(
                zip(
                    trajectory.snapshot.joint_names,
                    sample_trajectory(trajectory, duration * i / count).positions,
                    strict=True,
                )
            )
            for i in range(count + 1)
        ]
        return bool(self.probe.collision_check(states)["all_valid"])

    def send(self, trajectory: AcceptedTrajectory, *, start_time_s: float) -> str | None:
        from control_msgs.action import FollowJointTrajectory
        from trajectory_msgs.msg import JointTrajectory, JointTrajectoryPoint

        goal = FollowJointTrajectory.Goal()
        goal.trajectory = trajectory_message(trajectory, JointTrajectory, JointTrajectoryPoint)
        if not math.isfinite(start_time_s) or start_time_s <= 0:
            raise ValueError("a positive absolute simulation epoch is required")
        ns = round(start_time_s * 1e9)
        goal.trajectory.header.stamp.sec, goal.trajectory.header.stamp.nanosec = divmod(ns, 1_000_000_000)
        self.send_count += 1
        future = self.probe.arm_action.send_goal_async(goal)
        self._pending_send = future
        handle = self.probe._spin(future)
        if handle is None:
            # A late acceptance must not create an unowned moving goal. Keep
            # readiness false for the remainder of this ambiguous session.
            future.add_done_callback(self._cancel_late)
            raise TimeoutError("goal acceptance unknown; late acceptance will be canceled")
        self._pending_send = None
        if not handle.accepted:
            return None
        key = str(self.send_count)
        self._handles[key] = handle
        self._results[key] = handle.get_result_async()
        return key

    def status(self, handle: str) -> str | None:
        from action_msgs.msg import GoalStatus

        future = self._results[handle]
        if not future.done():
            return None
        result = future.result()
        if result is None:
            return "unknown"
        if result.status == GoalStatus.STATUS_SUCCEEDED and result.result.error_code == 0:
            return "succeeded"
        return {GoalStatus.STATUS_ABORTED: "aborted", GoalStatus.STATUS_CANCELED: "canceled"}.get(
            result.status, "unknown"
        )

    def cancel(self, handle: str) -> None:
        result = self.probe._spin(self._handles[handle].cancel_goal_async())
        if result is None:
            raise TimeoutError("cancellation unconfirmed")

    def _cancel_late(self, done):
        late = done.result()
        if late is None:
            return
        if late.accepted:
            self._late_result = late.get_result_async()
            late.cancel_goal_async()
        else:
            self._late_rejected = True

    def close(self, *, timeout_s: float = 5.0):
        """Service late acceptance/cancellation before destroying the client.

        A timeout is an explicit cleanup failure, never stopped evidence. The
        owning process must retain this error in its final result.
        """
        deadline = time.monotonic() + timeout_s
        if self._pending_send is not None:
            while not self._late_rejected and not (self._late_result and self._late_result.done()):
                if time.monotonic() >= deadline:
                    raise TimeoutError("shutdown blocked: goal acceptance/result still unconfirmed")
                self.spin()
        for key, future in self._results.items():
            if not future.done():
                self.cancel(key)
                while not future.done():
                    if time.monotonic() >= deadline:
                        raise TimeoutError("shutdown blocked: active goal result still unconfirmed")
                    self.spin()
        self.probe.close()
