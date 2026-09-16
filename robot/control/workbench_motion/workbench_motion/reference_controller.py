"""Deterministic local admission controller; it never dispatches to hardware."""

from __future__ import annotations

import math

from workbench_motion.controller import Controller
from workbench_motion.joint_limits import AcceptedTrajectory, PreflightContext
from workbench_motion.motion_types import (
    CommandMode,
    ControllerLifecycle,
    ExecutionReceipt,
    ReceiptReason,
    ReceiptStatus,
    RobotCommand,
    RobotState,
)


def _nonblank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-blank string")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


class InMemoryController(Controller):
    """Fail-closed controller seam used for deterministic port tests only."""

    def __init__(
        self,
        robot_id: str,
        joint_names: tuple[str, ...],
        *,
        preflight_context: PreflightContext,
        max_state_age_s: float,
        max_command_age_s: float = 0.5,
        max_future_skew_s: float = 0.0,
        start_state_tolerance: float | None = None,
        stop_failure_reason: str | None = None,
        clock_id: str = "monotonic",
    ) -> None:
        if not isinstance(preflight_context, PreflightContext):
            raise TypeError("preflight_context must be PreflightContext")
        if preflight_context.expected_joint_names != tuple(joint_names):
            raise ValueError("preflight_context joint schema must match controller")
        self._robot_id = _nonblank(robot_id, "robot_id")
        self._joint_names = preflight_context.expected_joint_names
        self._context = preflight_context
        self._clock_id = _nonblank(clock_id, "clock_id")
        self._max_state_age_s = _finite(max_state_age_s, "max_state_age_s")
        self._max_command_age_s = _finite(max_command_age_s, "max_command_age_s")
        self._max_future_skew_s = _finite(max_future_skew_s, "max_future_skew_s")
        self._start_state_tolerance = (
            preflight_context.policy.max_start_state_delta_rad
            if start_state_tolerance is None
            else _finite(start_state_tolerance, "start_state_tolerance")
        )
        if (
            min(self._max_state_age_s, self._max_command_age_s, self._max_future_skew_s, self._start_state_tolerance)
            < 0
        ):
            raise ValueError("controller timing and tolerance values must be non-negative")
        if stop_failure_reason is not None:
            stop_failure_reason = _nonblank(stop_failure_reason, "stop_failure_reason")
        self._stop_failure_reason = stop_failure_reason
        self._lifecycle = ControllerLifecycle.READY
        self._state: RobotState | None = None
        self._accepted_request_ids: set[str] = set()

    @property
    def lifecycle(self) -> ControllerLifecycle:
        return self._lifecycle

    @property
    def clock_id(self) -> str:
        return self._clock_id

    def update_state(self, state: RobotState) -> None:
        if not isinstance(state, RobotState):
            raise TypeError("state must be RobotState")
        if state.robot_id != self._robot_id or state.joint_names != self._joint_names:
            raise ValueError("state robot identity and joint schema must match controller")
        if state.clock_id != self._clock_id:
            raise ValueError("state clock_id must match controller")
        if self._state is not None and state.observed_at_s < self._state.observed_at_s:
            raise ValueError("state feedback timestamp must not move backwards")
        self._state = state

    def get_state(self, *, now_s: float) -> RobotState | None:
        now = _finite(now_s, "now_s")
        if self._state is None:
            return None
        age = now - self._state.observed_at_s
        if age < -self._max_future_skew_s:
            return None
        if age > self._max_state_age_s:
            return None
        return self._state

    def submit_command(self, command: RobotCommand, *, now_s: float) -> ExecutionReceipt:
        now = _finite(now_s, "now_s")
        if not isinstance(command, RobotCommand):
            raise TypeError("command must be RobotCommand")
        if command.robot_id != self._robot_id:
            return self._reject(command.command_id, ReceiptReason.ROBOT_ID_MISMATCH)
        if command.joint_names != self._joint_names:
            return self._reject(command.command_id, ReceiptReason.JOINT_SCHEMA_MISMATCH)
        if command.clock_id != self._clock_id:
            return self._reject(command.command_id, ReceiptReason.CLOCK_MISMATCH)
        command_age = now - command.issued_at_s
        if command_age < 0:
            return self._reject(command.command_id, ReceiptReason.FUTURE_COMMAND)
        if command_age > self._max_command_age_s:
            return self._reject(command.command_id, ReceiptReason.STALE_COMMAND)
        for joint, value in zip(command.joint_names, command.values, strict=True):
            limit = dict(self._context.effective_limits)[joint]
            epsilon = self._context.policy.limit_epsilon
            if command.mode is CommandMode.POSITION and not (
                limit.min_position + epsilon <= value <= limit.max_position - epsilon
            ):
                return self._reject(command.command_id, ReceiptReason.POSITION_LIMIT)
            if command.mode is CommandMode.VELOCITY and abs(value) > max(0.0, limit.max_velocity - epsilon):
                return self._reject(command.command_id, ReceiptReason.VELOCITY_LIMIT)
        return self._admit(command.command_id, now)

    def submit_trajectory(
        self,
        request_id: str,
        trajectory: AcceptedTrajectory,
        *,
        now_s: float,
    ) -> ExecutionReceipt:
        request = _nonblank(request_id, "request_id")
        now = _finite(now_s, "now_s")
        if not isinstance(trajectory, AcceptedTrajectory):
            return self._reject(request, ReceiptReason.INVALID_TRAJECTORY)
        if trajectory.snapshot.joint_names != self._joint_names:
            return self._reject(request, ReceiptReason.JOINT_SCHEMA_MISMATCH)
        if trajectory.context_sha256 != self._context.context_sha256:
            return self._reject(request, ReceiptReason.TRAJECTORY_CONTEXT_MISMATCH)
        state = self.get_state(now_s=now)
        if state is None:
            return self._reject(request, self._state_reason(now))
        start_positions = trajectory.snapshot.points[0].positions
        if any(
            abs(start - observed) > self._start_state_tolerance
            for start, observed in zip(start_positions, state.positions, strict=True)
        ):
            return self._reject(request, ReceiptReason.START_STATE_DISCONTINUITY)
        return self._admit(request, now)

    def hold(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        request = _nonblank(request_id, "request_id")
        now = _finite(now_s, "now_s")
        if self._lifecycle is not ControllerLifecycle.READY:
            return self._reject(request, ReceiptReason.INVALID_LIFECYCLE)
        if self.get_state(now_s=now) is None:
            return self._reject(request, self._state_reason(now))
        self._accepted_request_ids.add(request)
        return self._transition(request, ControllerLifecycle.HOLDING)

    def stop(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        request = _nonblank(request_id, "request_id")
        _finite(now_s, "now_s")
        if request in self._accepted_request_ids:
            return self._reject(request, ReceiptReason.DUPLICATE_REQUEST)
        if self._lifecycle is ControllerLifecycle.FAULTED:
            return self._reject(request, ReceiptReason.INVALID_LIFECYCLE)
        if self._stop_failure_reason is not None:
            before = self._lifecycle
            self._lifecycle = ControllerLifecycle.FAULTED
            return ExecutionReceipt(
                request,
                ReceiptStatus.REJECTED,
                ReceiptReason.SAFE_STOP_FAILURE,
                before,
                self._lifecycle,
                False,
                self._stop_failure_reason,
            )
        self._accepted_request_ids.add(request)
        return self._transition(request, ControllerLifecycle.STOPPED)

    def reset(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        request = _nonblank(request_id, "request_id")
        _finite(now_s, "now_s")
        if request in self._accepted_request_ids:
            return self._reject(request, ReceiptReason.DUPLICATE_REQUEST)
        if self._lifecycle not in {
            ControllerLifecycle.HOLDING,
            ControllerLifecycle.STOPPED,
            ControllerLifecycle.FAULTED,
        }:
            return self._reject(request, ReceiptReason.INVALID_LIFECYCLE)
        self._accepted_request_ids.add(request)
        return self._transition(request, ControllerLifecycle.READY)

    def _admit(self, request_id: str, now_s: float) -> ExecutionReceipt:
        if request_id in self._accepted_request_ids:
            return self._reject(request_id, ReceiptReason.DUPLICATE_REQUEST)
        if self._lifecycle is not ControllerLifecycle.READY:
            return self._reject(request_id, ReceiptReason.INVALID_LIFECYCLE)
        if self.get_state(now_s=now_s) is None:
            return self._reject(request_id, self._state_reason(now_s))
        self._accepted_request_ids.add(request_id)
        return ExecutionReceipt(
            request_id,
            ReceiptStatus.ACCEPTED,
            ReceiptReason.ACCEPTED,
            self._lifecycle,
            self._lifecycle,
            False,
        )

    def _state_reason(self, now_s: float) -> ReceiptReason:
        if self._state is None:
            return ReceiptReason.NO_STATE
        age = now_s - self._state.observed_at_s
        if age < -self._max_future_skew_s:
            return ReceiptReason.FUTURE_STATE
        if age > self._max_state_age_s:
            return ReceiptReason.STALE_STATE
        return ReceiptReason.NO_STATE

    def _transition(self, request_id: str, target: ControllerLifecycle) -> ExecutionReceipt:
        before = self._lifecycle
        self._lifecycle = target
        return ExecutionReceipt(request_id, ReceiptStatus.ACCEPTED, ReceiptReason.ACCEPTED, before, target, False)

    def _reject(self, request_id: str, reason: ReceiptReason) -> ExecutionReceipt:
        return ExecutionReceipt(request_id, ReceiptStatus.REJECTED, reason, self._lifecycle, self._lifecycle, False)


__all__ = ["InMemoryController"]
