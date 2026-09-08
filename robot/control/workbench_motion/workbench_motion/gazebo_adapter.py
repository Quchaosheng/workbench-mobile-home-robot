"""Gazebo Harmonic/JTC adapter for the ROS-free motion control boundary.

ROS imports are deliberately lazy.  The conversion and freshness rules remain
unit-testable without ROS, while :class:`GazeboTrajectoryController` is the
runtime seam that talks to ``FollowJointTrajectory`` and ``/joint_states``.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .controller import Controller, ControllerMode, RobotCommand, RobotState
from .trajectory_executor import (
    AcceptedTrajectoryExecutor,
    ExecutionGateStatus,
    TrajectoryExecutionResult,
)

if TYPE_CHECKING:
    from .joint_limits import AcceptedTrajectory


def _accepted_type() -> type:
    from .joint_limits import AcceptedTrajectory

    return AcceptedTrajectory


class GazeboActionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    ABORTED = "aborted"
    CANCELED = "canceled"
    TIMEOUT = "timeout"
    NOT_CONVERGED = "not_converged"
    UNAVAILABLE = "unavailable"


class JointStateReason(StrEnum):
    INVALID_MESSAGE = "invalid_message"
    MISSING_JOINT = "missing_joint"
    NON_FINITE = "non_finite"
    INVALID_TIMESTAMP = "invalid_timestamp"
    STALE_TIMESTAMP = "stale_timestamp"


class JointStateConversionError(ValueError):
    """A JointState message cannot produce a complete, finite RobotState."""

    def __init__(self, reason: JointStateReason, message: str) -> None:
        self.reason = reason
        super().__init__(f"{reason.value}: {message}")


def _duration_fields(time_from_start_ns: int, duration: Any) -> None:
    if isinstance(time_from_start_ns, bool) or not isinstance(time_from_start_ns, int) or time_from_start_ns < 0:
        raise ValueError("trajectory timestamp must be a non-negative integer")
    if duration is None or not hasattr(duration, "sec") or not hasattr(duration, "nanosec"):
        raise ValueError("goal point must provide a mutable time_from_start duration")
    duration.sec = time_from_start_ns // 1_000_000_000
    duration.nanosec = time_from_start_ns % 1_000_000_000


def _strict_float(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("value must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise ValueError("value must be finite")
    return converted


def accepted_to_follow_joint_goal(
    accepted: AcceptedTrajectory,
    *,
    goal: Any | None = None,
    goal_type: type | None = None,
    point_type: type | None = None,
) -> Any:
    """Materialize an accepted snapshot into a FollowJointTrajectory goal.

    The optional message types make this conversion testable without ROS.  In
    production they are imported lazily from ``control_msgs`` and
    ``trajectory_msgs``.
    """
    if not isinstance(accepted, _accepted_type()):
        raise TypeError("accepted must be AcceptedTrajectory")
    if goal is None:
        if goal_type is None:
            from control_msgs.action import FollowJointTrajectory

            goal_type = FollowJointTrajectory.Goal
        goal = goal_type()
    if point_type is None:
        from trajectory_msgs.msg import JointTrajectoryPoint

        point_type = JointTrajectoryPoint

    snapshot = accepted.snapshot
    goal.trajectory.joint_names = list(snapshot.joint_names)
    points: list[Any] = []
    for normalized in snapshot.points:
        point = point_type()
        point.positions = list(normalized.positions)
        if normalized.velocities:
            point.velocities = list(normalized.velocities)
        if normalized.accelerations:
            point.accelerations = list(normalized.accelerations)
        if normalized.effort:
            point.effort = list(normalized.effort)
        _duration_fields(normalized.time_from_start_ns, getattr(point, "time_from_start", None))
        points.append(point)
    goal.trajectory.points = points
    return goal


def _stamp_ns(message: Any) -> int:
    try:
        stamp = message.header.stamp
        sec, nanosec = stamp.sec, stamp.nanosec
    except (AttributeError, TypeError) as exc:
        raise JointStateConversionError(JointStateReason.INVALID_TIMESTAMP, "header.stamp is missing") from exc
    if isinstance(sec, bool) or isinstance(nanosec, bool) or not isinstance(sec, int) or not isinstance(nanosec, int):
        raise JointStateConversionError(JointStateReason.INVALID_TIMESTAMP, "stamp fields must be integers")
    if sec < 0 or nanosec < 0 or nanosec >= 1_000_000_000 or (sec == 0 and nanosec == 0):
        raise JointStateConversionError(JointStateReason.INVALID_TIMESTAMP, "stamp is outside ROS time bounds")
    return sec * 1_000_000_000 + nanosec


def joint_state_to_robot_state(
    message: Any,
    *,
    joint_names: Sequence[str],
    sequence: int,
    mode: ControllerMode = ControllerMode.IDLE,
    active_command_id: str | None = None,
) -> RobotState:
    """Convert a complete finite JointState sample into the canonical state."""
    if isinstance(joint_names, str | bytes | bytearray):
        raise ValueError("joint_names must be a sequence of names, not a string")
    required = tuple(joint_names)
    if (
        not required
        or any(type(name) is not str or not name for name in required)
        or len(set(required)) != len(required)
    ):
        raise ValueError("joint_names must be a non-empty unique sequence")
    try:
        names = tuple(message.name)
        positions = tuple(message.position)
    except (AttributeError, TypeError) as exc:
        raise JointStateConversionError(JointStateReason.INVALID_MESSAGE, "name/position arrays are missing") from exc
    if (
        len(names) != len(positions)
        or any(type(name) is not str or not name for name in names)
        or len(set(names)) != len(names)
    ):
        raise JointStateConversionError(JointStateReason.INVALID_MESSAGE, "name and position arrays are malformed")
    index = {name: value for value, name in enumerate(names)}
    missing = tuple(name for name in required if name not in index)
    if missing:
        raise JointStateConversionError(JointStateReason.MISSING_JOINT, f"missing joints: {missing}")
    try:
        ordered_positions = tuple(_strict_float(positions[index[name]]) for name in required)
    except (OverflowError, TypeError, ValueError) as exc:
        raise JointStateConversionError(JointStateReason.NON_FINITE, "positions must be numeric") from exc
    if any(not math.isfinite(value) for value in ordered_positions):
        raise JointStateConversionError(JointStateReason.NON_FINITE, "positions must be finite")

    velocities: tuple[float, ...] = ()
    try:
        raw_velocities = tuple(getattr(message, "velocity", ()) or ())
    except TypeError as exc:
        raise JointStateConversionError(JointStateReason.INVALID_MESSAGE, "velocity array is malformed") from exc
    if raw_velocities and len(raw_velocities) == len(names):
        try:
            ordered_velocities = tuple(_strict_float(raw_velocities[index[name]]) for name in required)
        except (OverflowError, TypeError, ValueError) as exc:
            raise JointStateConversionError(JointStateReason.NON_FINITE, "velocities must be numeric") from exc
        if any(not math.isfinite(value) for value in ordered_velocities):
            raise JointStateConversionError(JointStateReason.NON_FINITE, "velocities must be finite")
        velocities = ordered_velocities
    elif raw_velocities:
        raise JointStateConversionError(JointStateReason.INVALID_MESSAGE, "velocity array must be empty or complete")

    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 0:
        raise JointStateConversionError(JointStateReason.INVALID_MESSAGE, "sequence must be a non-negative integer")
    try:
        return RobotState(
            required,
            ordered_positions,
            velocities,
            timestamp_ns=_stamp_ns(message),
            sequence=sequence,
            mode=mode,
            active_command_id=active_command_id,
        )
    except JointStateConversionError:
        raise
    except (TypeError, ValueError) as error:
        raise JointStateConversionError(JointStateReason.INVALID_MESSAGE, str(error)) from error


@dataclass(frozen=True, slots=True)
class GazeboExecutionResult:
    """Runtime result; action success is insufficient without state convergence."""

    gate: TrajectoryExecutionResult
    status: GazeboActionStatus
    action_error_code: int | None
    final_state: RobotState | None
    max_position_error: float | None
    max_velocity: float | None
    detail: str | None = None


class GazeboTrajectoryController(Controller):
    """Controller backed by a ROS 2 FollowJointTrajectory action in Gazebo."""

    def __init__(
        self,
        initial_state: RobotState,
        *,
        node: Any,
        action_name: str,
        joint_names: Sequence[str] | None = None,
        action_client: Any | None = None,
        joint_state_type: type | None = None,
        joint_state_topic: str = "/joint_states",
        subscribe_joint_state: bool = True,
        freshness_s: float = 0.25,
        convergence_tolerance: float = 0.02,
        stopped_velocity_tolerance: float = 0.01,
    ) -> None:
        super().__init__(initial_state)
        if node is None or not isinstance(action_name, str) or not action_name.strip():
            raise ValueError("node and action_name are required")
        self._node = node
        if isinstance(joint_names, str | bytes | bytearray):
            raise ValueError("joint_names must be a sequence of names, not a string")
        self._joint_names = tuple(initial_state.joint_names if joint_names is None else joint_names)
        if self._joint_names != initial_state.joint_names:
            raise ValueError("joint_names must match initial_state")
        for value, label in (
            (freshness_s, "freshness_s"),
            (convergence_tolerance, "convergence_tolerance"),
            (stopped_velocity_tolerance, "stopped_velocity_tolerance"),
        ):
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{label} must be a positive finite number")
        self._freshness_s = freshness_s
        self._convergence_tolerance = convergence_tolerance
        self._stopped_velocity_tolerance = stopped_velocity_tolerance
        self._goal_future: Any | None = None
        self._goal_handle: Any | None = None
        self._result_future: Any | None = None
        self._active_target: tuple[float, ...] | None = None
        self._last_joint_state_error: JointStateConversionError | None = None
        self._action_client = action_client if action_client is not None else self._make_action_client(action_name)
        self._subscription = (
            self._make_joint_state_subscription(joint_state_topic, joint_state_type) if subscribe_joint_state else None
        )

    def _make_action_client(self, action_name: str) -> Any:
        from control_msgs.action import FollowJointTrajectory
        from rclpy.action import ActionClient

        return ActionClient(self._node, FollowJointTrajectory, action_name)

    def _make_joint_state_subscription(self, topic: str, joint_state_type: type | None) -> Any:
        if joint_state_type is None:
            from sensor_msgs.msg import JointState

            joint_state_type = JointState

        return self._node.create_subscription(joint_state_type, topic, self._on_joint_state, 50)

    def _on_joint_state(self, message: Any) -> None:
        current = self.state
        try:
            state = joint_state_to_robot_state(
                message,
                joint_names=self._joint_names,
                sequence=current.sequence + 1,
                mode=current.mode,
                active_command_id=current.active_command_id,
            )
            if current.timestamp_ns and state.timestamp_ns <= current.timestamp_ns:
                raise JointStateConversionError(
                    JointStateReason.STALE_TIMESTAMP,
                    "joint state timestamp must increase",
                )
            self.update_state(state)
            self._last_joint_state_error = None
        except (JointStateConversionError, TypeError, ValueError) as error:
            self._last_joint_state_error = (
                error
                if isinstance(error, JointStateConversionError)
                else JointStateConversionError(JointStateReason.INVALID_MESSAGE, str(error))
            )

    def ingest_joint_state(self, message: Any) -> bool:
        """Ingest a sample when the owning ROS adapter provides the subscription."""
        sequence = self.state.sequence
        self._on_joint_state(message)
        return self.state.sequence > sequence

    def _dispatch_motion(self, command: RobotCommand) -> tuple[bool, str | None]:
        if command.accepted_trajectory is None:
            return False, "trajectory_required"
        try:
            goal = accepted_to_follow_joint_goal(command.accepted_trajectory)
            goal_future = self._action_client.send_goal_async(goal, feedback_callback=self._on_feedback)
            if goal_future is None or not callable(getattr(goal_future, "done", None)):
                return False, "send_failed:invalid_future"
            self._goal_future = goal_future
            self._goal_handle = None
            self._result_future = None
            self._active_target = tuple(command.accepted_trajectory.snapshot.points[-1].positions)
            return True, None
        except (AttributeError, IndexError, RuntimeError, TypeError, ValueError) as error:
            return False, f"send_failed:{type(error).__name__}"

    def _on_feedback(self, _feedback: Any) -> None:
        return None

    def _spin_once(self, timeout_s: float) -> None:
        spin_once = getattr(self._node, "spin_once", None)
        if callable(spin_once):
            spin_once(timeout_sec=timeout_s)
            return
        try:
            import rclpy

            rclpy.spin_once(self._node, timeout_sec=timeout_s)
        except ImportError as error:
            raise RuntimeError("rclpy is required for Gazebo execution") from error

    def _wait_future(self, future: Any, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while not future.done() and time.monotonic() < deadline:
            self._spin_once(min(0.05, max(0.0, deadline - time.monotonic())))
        return bool(future.done())

    @staticmethod
    def _action_status_code(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value
        enum_value = getattr(value, "value", None)
        if isinstance(enum_value, int) and not isinstance(enum_value, bool):
            return enum_value
        text = str(value).strip().lower().split(".")[-1]
        names = {
            "unknown": 0,
            "accepted": 1,
            "executing": 2,
            "canceling": 3,
            "cancelling": 3,
            "succeeded": 4,
            "canceled": 5,
            "cancelled": 5,
            "aborted": 6,
        }
        for prefix in ("status_", "goal_status_"):
            if text.startswith(prefix):
                text = text[len(prefix) :]
        return names.get(text)

    def _clear_goal(self) -> None:
        self._goal_future = None
        self._goal_handle = None
        self._result_future = None
        self._active_target = None

    def wait_for_completion(
        self,
        timeout_s: float,
        *,
        target: Sequence[float],
    ) -> tuple[GazeboActionStatus, int | None, str | None]:
        """Wait for action completion and return raw action classification."""
        if (
            isinstance(timeout_s, bool)
            or not isinstance(timeout_s, int | float)
            or not math.isfinite(timeout_s)
            or timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a positive finite number")
        try:
            target_values = tuple(_strict_float(value) for value in target)
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("target must contain finite joint positions") from error
        if len(target_values) != len(self._joint_names) or any(not math.isfinite(value) for value in target_values):
            raise ValueError("target must contain one finite position per controlled joint")
        if self._active_target is not None and target_values != self._active_target:
            raise ValueError("target must match the active trajectory endpoint")
        if self._goal_future is None:
            return GazeboActionStatus.UNAVAILABLE, None, "no_goal_pending"
        deadline = time.monotonic() + timeout_s
        try:
            if not self._wait_future(self._goal_future, timeout_s):
                self._cancel_motion(self.state.active_command_id or "")
                self._set_mode(ControllerMode.FAULTED, None)
                return GazeboActionStatus.TIMEOUT, None, "goal_acceptance_timeout"
            self._goal_handle = self._goal_future.result()
            if self._goal_handle is None or not bool(getattr(self._goal_handle, "accepted", False)):
                self._clear_goal()
                self._set_mode(ControllerMode.IDLE, None)
                return GazeboActionStatus.REJECTED, None, "goal_rejected"
            self._result_future = self._goal_handle.get_result_async()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not self._wait_future(self._result_future, remaining):
                canceled = self._cancel_motion(self.state.active_command_id or "")
                self._set_mode(ControllerMode.FAULTED, None)
                detail = "result_timeout_canceled" if canceled else "result_timeout_cancel_failed"
                return GazeboActionStatus.TIMEOUT, None, detail
            wrapped = self._result_future.result()
            result = getattr(wrapped, "result", wrapped)
            error_code = getattr(result, "error_code", None)
            action_status = self._action_status_code(getattr(wrapped, "status", None))
            if action_status == 5:
                self._clear_goal()
                self._set_mode(ControllerMode.IDLE, None)
                return GazeboActionStatus.CANCELED, error_code, "goal_canceled"
            if action_status == 6:
                self._clear_goal()
                self._set_mode(ControllerMode.IDLE, None)
                return GazeboActionStatus.ABORTED, error_code, "controller_aborted"
            if action_status is None:
                self._clear_goal()
                self._set_mode(ControllerMode.FAULTED, None)
                return GazeboActionStatus.UNAVAILABLE, error_code, "missing_goal_status"
            if action_status != 4:
                self._clear_goal()
                self._set_mode(ControllerMode.FAULTED, None)
                return GazeboActionStatus.UNAVAILABLE, error_code, "unexpected_goal_status"
            if error_code is None:
                self._clear_goal()
                self._set_mode(ControllerMode.FAULTED, None)
                return GazeboActionStatus.UNAVAILABLE, None, "missing_error_code"
            if error_code == 0 and action_status == 4:
                self._clear_goal()
                self._set_mode(ControllerMode.IDLE, None)
                return GazeboActionStatus.SUCCEEDED, 0, None
            self._clear_goal()
            self._set_mode(ControllerMode.IDLE, None)
            return GazeboActionStatus.ABORTED, error_code, "controller_aborted"
        except Exception as error:  # noqa: BLE001 - ROS action failures fail closed.
            self._clear_goal()
            self._set_mode(ControllerMode.FAULTED, None)
            return GazeboActionStatus.UNAVAILABLE, None, f"action_error:{type(error).__name__}"

    def _cancel_motion(self, _command_id: str) -> bool:
        if self._goal_handle is None and self._goal_future is not None:
            try:
                if not self._goal_future.done():
                    cancel = getattr(self._goal_future, "cancel", None)
                    if callable(cancel):
                        cancel()
                    return False
                self._goal_handle = self._goal_future.result()
            except Exception:  # noqa: BLE001 - an unresolved goal cannot be safely cancelled.
                return False
        if self._goal_handle is None:
            return self._goal_future is None
        if not bool(getattr(self._goal_handle, "accepted", False)):
            self._clear_goal()
            return True
        try:
            future = self._goal_handle.cancel_goal_async()
            if not self._wait_future(future, self._freshness_s):
                return False
            response = future.result()
            goals_canceling = getattr(response, "goals_canceling", None)
            if goals_canceling is not None and not goals_canceling:
                return False
            accepted = getattr(response, "accepted", None)
            if accepted is not None and not bool(accepted):
                return False
            return_code = getattr(response, "return_code", None)
            if return_code is not None and return_code != 0:
                return False
            self._clear_goal()
            return True
        except Exception:  # noqa: BLE001 - cancellation failures keep the controller faulted.
            return False

    def _hold_motion(self) -> bool:
        return False

    def _safe_stop_motion(self, _reason: str) -> bool:
        if self._goal_handle is None and self._goal_future is None:
            return True
        canceled = self._cancel_motion(self.state.active_command_id or "")
        if not canceled:
            self._set_mode(ControllerMode.FAULTED, None)
        return canceled

    def _reset_motion(self) -> bool:
        if self._goal_handle is not None or self._goal_future is not None:
            if not self._cancel_motion(self.state.active_command_id or ""):
                return False
        self._clear_goal()
        return True

    def execute_accepted(
        self,
        accepted: AcceptedTrajectory,
        *,
        expected_state: RobotState,
        context_provider: Callable[[], Any],
        timeout_s: float,
    ) -> GazeboExecutionResult:
        """Run freshness gates, dispatch once, then require real feedback convergence."""
        gate = AcceptedTrajectoryExecutor(self, context_provider).execute(accepted, expected_state=expected_state)
        if gate.status is not ExecutionGateStatus.ACCEPTED:
            return GazeboExecutionResult(gate, GazeboActionStatus.REJECTED, None, None, None, None, "preflight_gate")

        try:
            target = tuple(accepted.snapshot.points[-1].positions)
        except (AttributeError, IndexError, TypeError) as error:
            self._set_mode(ControllerMode.FAULTED, None)
            return GazeboExecutionResult(
                gate,
                GazeboActionStatus.UNAVAILABLE,
                None,
                self.state,
                None,
                None,
                f"invalid_trajectory_endpoint:{type(error).__name__}",
            )
        action_status, error_code, detail = self.wait_for_completion(timeout_s, target=target)
        final_state = self.state
        if action_status is GazeboActionStatus.SUCCEEDED:
            if final_state.timestamp_ns <= 0 or final_state.sequence <= expected_state.sequence:
                self._set_mode(ControllerMode.FAULTED, None)
                return GazeboExecutionResult(
                    gate,
                    GazeboActionStatus.NOT_CONVERGED,
                    error_code,
                    final_state,
                    None,
                    None,
                    "no_fresh_feedback",
                )
            position_errors = [
                abs(actual - desired) for actual, desired in zip(final_state.positions, target, strict=True)
            ]
            max_position_error = max(position_errors, default=math.inf)
            max_velocity = max((abs(value) for value in final_state.velocities), default=math.inf)
            if max_position_error > self._convergence_tolerance or max_velocity > self._stopped_velocity_tolerance:
                self._set_mode(ControllerMode.FAULTED, None)
                return GazeboExecutionResult(
                    gate,
                    GazeboActionStatus.NOT_CONVERGED,
                    error_code,
                    final_state,
                    max_position_error,
                    max_velocity,
                    "feedback_outside_convergence_tolerance",
                )
            return GazeboExecutionResult(
                gate,
                action_status,
                error_code,
                final_state,
                max_position_error,
                max_velocity,
                detail,
            )
        return GazeboExecutionResult(gate, action_status, error_code, final_state, None, None, detail)


__all__ = [
    "GazeboActionStatus",
    "GazeboExecutionResult",
    "GazeboTrajectoryController",
    "JointStateConversionError",
    "JointStateReason",
    "accepted_to_follow_joint_goal",
    "joint_state_to_robot_state",
]
