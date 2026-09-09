"""Deterministic in-memory controller for port-level tests.

This class deliberately never sends a command. It validates admission and
lifecycle behavior so simulator and hardware adapters can prove their own
transport and execution paths later.
"""

from __future__ import annotations

import math

from workbench_motion.controller import Controller
from workbench_motion.joint_limits import AcceptedTrajectory
from workbench_motion.motion_types import (
    ControllerLifecycle,
    ExecutionReceipt,
    ReceiptReason,
    ReceiptStatus,
    RobotCommand,
    RobotState,
)


def _nonblank_request_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("request_id must be a non-blank string")
    return value


def _finite_now(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("now_s must be a finite number")
    now_s = float(value)
    if not math.isfinite(now_s):
        raise ValueError("now_s must be a finite number")
    return now_s


class InMemoryController(Controller):
    """Fail-closed local admission model with no simulator or transport side effect."""

    def __init__(
        self,
        robot_id: str,
        joint_names: tuple[str, ...],
        *,
        max_state_age_s: float,
        start_state_tolerance: float = 1e-6,
        stop_failure_reason: str | None = None,
    ) -> None:
        identity = RobotState(robot_id, joint_names, (0.0,) * len(joint_names), (0.0,) * len(joint_names), 0.0)
        self._robot_id = identity.robot_id
        self._joint_names = identity.joint_names
        self._max_state_age_s = _finite_now(max_state_age_s)
        self._start_state_tolerance = _finite_now(start_state_tolerance)
        if self._max_state_age_s < 0:
            raise ValueError("max_state_age_s must be non-negative")
        if self._start_state_tolerance < 0:
            raise ValueError("start_state_tolerance must be non-negative")
        if stop_failure_reason is not None and (not isinstance(stop_failure_reason, str) or not stop_failure_reason):
            raise ValueError("stop_failure_reason must be a non-empty string when present")
        self._stop_failure_reason = stop_failure_reason
        self._lifecycle = ControllerLifecycle.READY
        self._state: RobotState | None = None
        self._accepted_request_ids: set[str] = set()

    @property
    def lifecycle(self) -> ControllerLifecycle:
        return self._lifecycle

    def update_state(self, state: RobotState) -> None:
        """Inject feedback for deterministic tests; adapters own real feedback."""
        if not isinstance(state, RobotState):
            raise TypeError("state must be RobotState")
        if state.robot_id != self._robot_id or state.joint_names != self._joint_names:
            raise ValueError("state robot identity and joint schema must match controller")
        if self._state is not None and state.observed_at_s < self._state.observed_at_s:
            raise ValueError("state feedback timestamp must not move backwards")
        self._state = state

    def get_state(self, *, now_s: float) -> RobotState | None:
        now = _finite_now(now_s)
        if self._state is None or now - self._state.observed_at_s > self._max_state_age_s:
            return None
        return self._state

    def submit_command(self, command: RobotCommand, *, now_s: float) -> ExecutionReceipt:
        now = _finite_now(now_s)
        if not isinstance(command, RobotCommand):
            raise TypeError("command must be RobotCommand")
        if command.robot_id != self._robot_id:
            return self._reject(command.command_id, ReceiptReason.ROBOT_ID_MISMATCH)
        if command.joint_names != self._joint_names:
            return self._reject(command.command_id, ReceiptReason.JOINT_SCHEMA_MISMATCH)
        return self._admit(command.command_id, now)

    def submit_trajectory(
        self,
        request_id: str,
        trajectory: AcceptedTrajectory,
        *,
        now_s: float,
    ) -> ExecutionReceipt:
        request = _nonblank_request_id(request_id)
        now = _finite_now(now_s)
        if not isinstance(trajectory, AcceptedTrajectory):
            return self._reject(request, ReceiptReason.INVALID_TRAJECTORY)
        if trajectory.snapshot.joint_names != self._joint_names:
            return self._reject(request, ReceiptReason.JOINT_SCHEMA_MISMATCH)
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
        request = _nonblank_request_id(request_id)
        now = _finite_now(now_s)
        if self._lifecycle is not ControllerLifecycle.READY:
            return self._reject(request, ReceiptReason.INVALID_LIFECYCLE)
        if self.get_state(now_s=now) is None:
            return self._reject(request, self._state_reason(now))
        self._accepted_request_ids.add(request)
        return self._transition(request, ControllerLifecycle.HOLDING)

    def stop(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        request = _nonblank_request_id(request_id)
        _finite_now(now_s)
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
        request = _nonblank_request_id(request_id)
        _finite_now(now_s)
        if request in self._accepted_request_ids:
            return self._reject(request, ReceiptReason.DUPLICATE_REQUEST)
        resettable = {ControllerLifecycle.HOLDING, ControllerLifecycle.STOPPED, ControllerLifecycle.FAULTED}
        if self._lifecycle not in resettable:
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
        if now_s - self._state.observed_at_s > self._max_state_age_s:
            return ReceiptReason.STALE_STATE
        return ReceiptReason.NO_STATE

    def _transition(self, request_id: str, target: ControllerLifecycle) -> ExecutionReceipt:
        before = self._lifecycle
        self._lifecycle = target
        return ExecutionReceipt(request_id, ReceiptStatus.ACCEPTED, ReceiptReason.ACCEPTED, before, target, False)

    def _reject(self, request_id: str, reason: ReceiptReason) -> ExecutionReceipt:
        return ExecutionReceipt(
            request_id,
            ReceiptStatus.REJECTED,
            reason,
            self._lifecycle,
            self._lifecycle,
            False,
        )


__all__ = ["InMemoryController"]
