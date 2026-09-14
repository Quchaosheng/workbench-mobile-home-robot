"""Supervisory control lifecycle with one immutable trajectory dispatch port.

This module has no ROS or simulator imports. It does not certify physical
stopping: only fresh feedback can confirm a stopped device. Cancellation is a
best-effort containment action after a fault, never a safe-stop receipt.
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import asdict, replace
from types import MappingProxyType
from typing import Protocol

from workbench_motion.joint_limits import AcceptedTrajectory, PreflightContext
from workbench_motion.motion_safety import (
    MotionPolicy,
    accept_motion,
    point_to_point,
    sample_trajectory,
    splice_braking_trajectory,
    validate_limits,
    with_start_hold,
)
from workbench_motion.motion_types import (
    CommandMode,
    ControllerLifecycle,
    ExecutionReceipt,
    ReceiptReason,
    ReceiptStatus,
    RobotCommand,
    RobotState,
)
from workbench_motion.quintic_trajectory import JointMotionLimit


class FeedbackUnavailable(ValueError):
    """Fresh feedback or watchdog continuity is missing."""


class MotionTransport(Protocol):
    def now_s(self) -> float: ...
    def read_state(self) -> RobotState | None: ...
    def readiness(self) -> bool: ...
    def scene_revision(self) -> str: ...
    def check_path(self, trajectory: AcceptedTrajectory, resolution: float) -> bool: ...
    def send(self, trajectory: AcceptedTrajectory, *, start_time_s: float) -> str | None: ...
    def status(self, handle: str) -> str | None: ...
    def cancel(self, handle: str) -> None: ...


def digest(value) -> str:
    return (
        "sha256:"
        + hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    )


class TrajectoryExecutor:
    def __init__(
        self,
        robot_id: str,
        transport: MotionTransport,
        context: PreflightContext,
        limits: Mapping[str, JointMotionLimit],
        policy: MotionPolicy,
        *,
        emit: Callable[[dict], None],
        configuration_current: Callable[[], bool] = lambda: True,
    ):
        validate_limits(context, limits)
        if not robot_id.strip():
            raise ValueError("robot_id must be nonblank")
        self.robot_id = robot_id
        self.transport = transport
        self.context = context
        self.limits = MappingProxyType(dict(limits))
        self.policy = policy
        self.emit = emit
        self.configuration_current = configuration_current
        self.configuration_sha256 = digest(
            {
                "context": context.context_sha256,
                "limits": {k: asdict(v) for k, v in limits.items()},
                "policy": asdict(policy),
            }
        )
        self.lifecycle = ControllerLifecycle.READY
        self._state: RobotState | None = None
        self._seen: set[str] = set()
        self._handle: str | None = None
        self._trajectory: AcceptedTrajectory | None = None
        self._request = ""
        self._scene = ""
        self._start_s = 0.0
        self._last_feedback_wall = 0.0
        self._last_stamp = -math.inf
        self._last_now = -math.inf
        self._dwell_start: float | None = None
        self._stop_target = ControllerLifecycle.STOPPED

    def execution_progress(self, *, now_s: float) -> tuple[float, float]:
        """Return elapsed simulation seconds and accepted reference duration."""
        if self._trajectory is None:
            raise ValueError("no trajectory has been dispatched")
        return now_s - self._start_s, self._trajectory.snapshot.points[-1].time_from_start_ns / 1e9

    def abort(self, detail: str) -> None:
        """Latch an active experiment fault; cancellation does not prove rest."""
        if self.lifecycle in {ControllerLifecycle.EXECUTING, ControllerLifecycle.STOPPING}:
            self._fault(ReceiptReason.BACKEND_FAILURE, detail)

    def update_state(self, state: RobotState) -> None:
        if state.robot_id != self.robot_id or state.joint_names != self.context.expected_joint_names:
            raise ValueError("feedback identity or joint schema mismatch")
        if self._state and (state.clock_id != self._state.clock_id or state.observed_at_s < self._state.observed_at_s):
            raise ValueError("feedback clock changed or moved backwards")
        self._state = state

    def get_state(self, *, now_s: float) -> RobotState | None:
        if not math.isfinite(now_s):
            raise ValueError("now_s must be finite")
        state = self.transport.read_state()
        if state is not None:
            self.update_state(state)
        if self._state is None or not 0 <= now_s - self._state.observed_at_s <= self.policy.max_state_age_s:
            return None
        return self._state

    def _receipt(
        self,
        request: str,
        reason: ReceiptReason,
        before: ControllerLifecycle,
        *,
        attempted=False,
        accepted=False,
        detail=None,
    ):
        return ExecutionReceipt(
            request,
            ReceiptStatus.ACCEPTED if accepted else ReceiptStatus.REJECTED,
            reason,
            before,
            self.lifecycle,
            attempted,
            detail,
        )

    def _reject(self, request, reason, detail=None):
        receipt = self._receipt(request, reason, self.lifecycle, detail=detail)
        self.emit({"event": "rejected", "receipt": asdict(receipt), "device_state": "unconfirmed"})
        return receipt

    def _fresh(self, now_s):
        state = self.get_state(now_s=now_s)
        if state is None:
            raise FeedbackUnavailable("fresh feedback unavailable")
        for name, q, v in zip(state.joint_names, state.positions, state.velocities, strict=True):
            limit = self.limits[name]
            if not limit.min_position <= q <= limit.max_position or abs(v) > limit.max_velocity:
                raise ValueError("measured position or velocity outside motion limits")
        return state

    def submit_command(self, command: RobotCommand, *, now_s: float, wall_s: float | None = None) -> ExecutionReceipt:
        if command.mode is not CommandMode.POSITION:
            return self._reject(command.command_id, ReceiptReason.UNSUPPORTED_MODE)
        if command.robot_id != self.robot_id or command.joint_names != self.context.expected_joint_names:
            return self._reject(command.command_id, ReceiptReason.JOINT_SCHEMA_MISMATCH)
        if not 0 <= now_s - command.issued_at_s <= self.policy.max_command_age_s:
            return self._reject(command.command_id, ReceiptReason.STALE_COMMAND)
        try:
            state = self._fresh(now_s)
            if command.clock_id != state.clock_id:
                return self._reject(command.command_id, ReceiptReason.CLOCK_MISMATCH)
            trajectory = point_to_point(state, command.values, self.context, self.limits)
        except ValueError as error:
            return self._reject(command.command_id, ReceiptReason.INVALID_TRAJECTORY, str(error))
        return self._dispatch(
            command.command_id, trajectory, now_s, wall_s, braking=False, issued_at_s=command.issued_at_s
        )

    def submit_trajectory(
        self, request_id: str, trajectory: AcceptedTrajectory, *, now_s: float, wall_s: float | None = None
    ) -> ExecutionReceipt:
        return self._dispatch(request_id, trajectory, now_s, wall_s, braking=False)

    def _dispatch(self, request, trajectory, now_s, wall_s, *, braking, issued_at_s=None, splice_s=None):
        before = self.lifecycle
        if not isinstance(request, str) or not request.strip():
            raise ValueError("request_id must be nonblank")
        if request in self._seen:
            return self._reject(request, ReceiptReason.DUPLICATE_REQUEST)
        allowed = {ControllerLifecycle.READY, ControllerLifecycle.EXECUTING} if braking else {ControllerLifecycle.READY}
        if self.lifecycle not in allowed:
            return self._reject(request, ReceiptReason.INVALID_LIFECYCLE)
        if not isinstance(trajectory, AcceptedTrajectory):
            return self._reject(request, ReceiptReason.INVALID_TRAJECTORY)
        if trajectory.context_sha256 != self.context.context_sha256:
            return self._reject(request, ReceiptReason.TRAJECTORY_CONTEXT_MISMATCH)
        self._seen.add(request)
        try:
            if not self.configuration_current() or not self.transport.readiness():
                return self._reject(request, ReceiptReason.NOT_READY)
            state = self._fresh(self.transport.now_s())
            first = trajectory.snapshot.points[0]
            if not braking and max(map(abs, state.velocities)) > self.policy.stopped_velocity_rad_s:
                return self._reject(request, ReceiptReason.START_STATE_DISCONTINUITY)
            # Preflight's exact t=0 anchor remains intact. Fresh measured state
            # is separately compared with bounded start tolerances below.
            anchor = replace(state, positions=first.positions)
            trajectory = accept_motion(trajectory, anchor, self.context, self.limits)
            if splice_s is None:
                trajectory = with_start_hold(trajectory, anchor, self.context, self.limits, self.policy.dispatch_lead_s)
            scene = self.transport.scene_revision()
            if not scene:
                return self._reject(request, ReceiptReason.NOT_READY)
            if splice_s is not None and scene != self._scene:
                return self._reject(request, ReceiptReason.SCENE_CHANGED)
            if not self.transport.check_path(trajectory, self.policy.collision_resolution_rad):
                return self._reject(request, ReceiptReason.COLLISION)
            if self.transport.scene_revision() != scene:
                return self._reject(request, ReceiptReason.SCENE_CHANGED)
            if not self.configuration_current():
                return self._reject(request, ReceiptReason.NOT_READY)
            now_s = self.transport.now_s()
            state = self._fresh(now_s)
            if issued_at_s is not None and not 0 <= now_s - issued_at_s <= self.policy.max_command_age_s:
                return self._reject(request, ReceiptReason.STALE_COMMAND)
            comparison = (
                first
                if splice_s is None
                else sample_trajectory(self._trajectory, max(0.0, state.observed_at_s - self._start_s))
            )
            if any(
                abs(a - b) > self.policy.start_position_tolerance_rad
                for a, b in zip(comparison.positions, state.positions, strict=True)
            ) or any(
                abs(a - b) > self.policy.start_velocity_tolerance_rad_s
                for a, b in zip(comparison.velocities, state.velocities, strict=True)
            ):
                return self._reject(request, ReceiptReason.START_STATE_DISCONTINUITY)
            epoch = self._start_s if splice_s is not None else round(now_s * 1e9) / 1e9
            deadline = (
                epoch
                + (splice_s if splice_s is not None else self.policy.dispatch_lead_s)
                - self.policy.dispatch_margin_s
            )
            if epoch <= 0 or now_s >= deadline:
                return self._reject(request, ReceiptReason.TIMEOUT, "dispatch continuity deadline expired")
        except (ValueError, RuntimeError, TimeoutError) as error:
            return self._reject(request, ReceiptReason.INVALID_TRAJECTORY, str(error))
        self.emit(
            {
                "event": "dispatch_intent",
                "request_id": request,
                "trajectory_sha256": trajectory.trajectory_sha256,
                "context_sha256": trajectory.context_sha256,
                "configuration_sha256": self.configuration_sha256,
                "scene_sha256": scene,
                "state": asdict(state),
                "state_sha256": digest(asdict(state)),
                "trajectory": {
                    "joint_names": trajectory.snapshot.joint_names,
                    "points": [asdict(p) for p in trajectory.snapshot.points],
                },
                "braking": braking,
                "sim_time_s": now_s,
                "start_time_s": epoch,
                "accept_before_s": deadline,
                "splice_time_from_start_s": splice_s,
            }
        )
        old_request = self._request if self._handle else None
        # The intent journal may fsync. Never transmit an expired prefix after
        # that blocking operation, even when preflight completed in time.
        try:
            if self.transport.now_s() >= deadline:
                return self._reject(request, ReceiptReason.TIMEOUT, "dispatch journal exhausted continuity deadline")
        except (ValueError, RuntimeError, TimeoutError) as error:
            return self._reject(request, ReceiptReason.NOT_READY, str(error))
        try:
            handle = self.transport.send(trajectory, start_time_s=epoch)
        except Exception as error:  # noqa: BLE001 - transport faults must latch unconfirmed
            self._fault(ReceiptReason.BACKEND_FAILURE, str(error), request=request)
            return self._receipt(request, ReceiptReason.BACKEND_FAILURE, before, attempted=True, detail=str(error))
        if handle is None:
            self._fault(ReceiptReason.BACKEND_REJECTED, "goal rejected", request=request)
            return self._receipt(request, ReceiptReason.BACKEND_REJECTED, before, attempted=True)
        self._handle, self._trajectory, self._request, self._scene = handle, trajectory, request, scene
        self._start_s = epoch
        if self.transport.now_s() >= deadline:
            self._fault(ReceiptReason.TIMEOUT, "acceptance missed continuity deadline")
            return self._receipt(request, ReceiptReason.TIMEOUT, before, attempted=True)
        self._last_now = now_s
        self._last_feedback_wall = time.monotonic() if wall_s is None else wall_s
        self._last_stamp = state.observed_at_s
        self._dwell_start = None
        self.lifecycle = ControllerLifecycle.STOPPING if braking else ControllerLifecycle.EXECUTING
        receipt = self._receipt(request, ReceiptReason.ACCEPTED, before, attempted=True, accepted=True)
        try:
            self.emit(
                {
                    "event": "dispatched",
                    "receipt": asdict(receipt),
                    "preempted_request_id": old_request,
                    "device_state": "unconfirmed",
                    "sim_time_s": now_s,
                }
            )
        except Exception:
            self.lifecycle = ControllerLifecycle.FAULTED
            self.transport.cancel(handle)
            raise
        return receipt

    def _fault(self, reason, detail, *, request=None):
        self.lifecycle = ControllerLifecycle.FAULTED
        if self._handle is not None:
            try:
                self.transport.cancel(self._handle)
            except Exception as error:  # noqa: BLE001 - transport faults must latch unconfirmed
                detail += f"; cancellation unconfirmed: {error}"
        self.emit(
            {
                "event": "fault",
                "request_id": request or self._request,
                "reason": reason.value,
                "detail": detail,
                "device_state": "unconfirmed",
            }
        )

    def poll(self, *, now_s: float, wall_s: float | None = None) -> RobotState | None:
        wall_s = time.monotonic() if wall_s is None else wall_s
        if self.lifecycle not in {
            ControllerLifecycle.EXECUTING,
            ControllerLifecycle.STOPPING,
            ControllerLifecycle.HOLDING,
            ControllerLifecycle.STOPPED,
        }:
            return self.get_state(now_s=now_s)
        try:
            if not math.isfinite(wall_s):
                raise ValueError("wall time must be finite")
            state = self._fresh(now_s)
            if now_s < self._last_now:
                raise ValueError("simulation clock moved backwards")
            self._last_now = now_s
            if (
                wall_s < self._last_feedback_wall
                or wall_s - self._last_feedback_wall > self.policy.max_feedback_wall_age_s
            ):
                raise FeedbackUnavailable("feedback wall watchdog expired or moved backwards")
            if state.observed_at_s > self._last_stamp:
                self._last_feedback_wall, self._last_stamp = wall_s, state.observed_at_s
            if not self.configuration_current() or self.transport.scene_revision() != self._scene:
                raise ValueError("scene or control configuration changed during execution")
            trajectory = self._trajectory
            if self.lifecycle in {ControllerLifecycle.HOLDING, ControllerLifecycle.STOPPED}:
                target = trajectory.snapshot.points[-1].positions
                if max(map(abs, state.velocities)) > self.policy.stopped_velocity_rad_s or any(
                    abs(q - goal) > self.policy.goal_tolerance_rad
                    for q, goal in zip(state.positions, target, strict=True)
                ):
                    raise ValueError("stopped confirmation revoked by movement")
                return state
            elapsed = max(0.0, state.observed_at_s - self._start_s)
            desired = sample_trajectory(trajectory, elapsed)
            error = max(abs(a - b) for a, b in zip(state.positions, desired.positions, strict=True))
            self.emit(
                {
                    "event": "sample",
                    "request_id": self._request,
                    "state": asdict(state),
                    "desired": asdict(desired),
                    "elapsed_s": elapsed,
                }
            )
            if error > self.policy.tracking_tolerance_rad:
                self._fault(ReceiptReason.TRACKING_ERROR, f"position tracking error {error}")
                return state
            result = self.transport.status(self._handle)
            if result not in {None, "succeeded"}:
                self._fault(ReceiptReason.BACKEND_FAILURE, f"action ended {result}")
                return state
            duration = trajectory.snapshot.points[-1].time_from_start_ns / 1e9
            if now_s - self._start_s > duration + self.policy.goal_time_s + self.policy.stopped_dwell_s:
                self._fault(ReceiptReason.TIMEOUT, "goal or stopping confirmation timed out")
                return state
            target = trajectory.snapshot.points[-1].positions
            stationary = max(map(abs, state.velocities)) <= self.policy.stopped_velocity_rad_s
            converged = all(
                abs(a - b) <= self.policy.goal_tolerance_rad for a, b in zip(state.positions, target, strict=True)
            )
            if result == "succeeded" and elapsed >= duration and stationary and converged:
                if self._dwell_start is None:
                    self._dwell_start = state.observed_at_s
                if state.observed_at_s - self._dwell_start >= self.policy.stopped_dwell_s:
                    stopping = self.lifecycle is ControllerLifecycle.STOPPING
                    self.lifecycle = self._stop_target if stopping else ControllerLifecycle.READY
                    self.emit(
                        {
                            "event": "completed",
                            "request_id": self._request,
                            "device_state": "stopped" if stopping else "confirmed",
                            "lifecycle": self.lifecycle.value,
                            "state": asdict(state),
                            "elapsed_s": elapsed,
                        }
                    )
                    self._handle = None
            else:
                self._dwell_start = None
            return state
        except FeedbackUnavailable as error:
            self._fault(ReceiptReason.STALE_STATE, str(error))
            return None
        except (ValueError, RuntimeError, TimeoutError, OSError) as error:
            self._fault(ReceiptReason.BACKEND_FAILURE, str(error))
            return None

    def _brake(self, request, now_s, wall_s, target):
        before = self.lifecycle
        if request in self._seen:
            return self._reject(request, ReceiptReason.DUPLICATE_REQUEST)
        if before not in {ControllerLifecycle.READY, ControllerLifecycle.EXECUTING}:
            return self._reject(request, ReceiptReason.INVALID_LIFECYCLE)
        try:
            state = self._fresh(now_s)
            splice_s = None
            if before is ControllerLifecycle.EXECUTING:
                splice_s = math.ceil((now_s - self._start_s + self.policy.dispatch_lead_s) * 1e9) / 1e9
                trajectory = splice_braking_trajectory(
                    self._trajectory, splice_s, state, self.context, self.limits, self.policy
                )
            else:
                if max(map(abs, state.velocities)) > self.policy.stopped_velocity_rad_s:
                    raise ValueError("no accepted active reference for moving device")
                trajectory = point_to_point(state, state.positions, self.context, self.limits)
            self._stop_target = target
            receipt = self._dispatch(request, trajectory, now_s, wall_s, braking=True, splice_s=splice_s)
            if receipt.status is ReceiptStatus.REJECTED:
                self._fault(ReceiptReason.SAFE_STOP_FAILURE, receipt.reason.value, request=request)
                receipt = replace(receipt, lifecycle_after=self.lifecycle)
            return receipt
        except (ValueError, RuntimeError, TimeoutError, OSError) as error:
            self._seen.add(request)
            self._fault(ReceiptReason.SAFE_STOP_FAILURE, str(error), request=request)
            return self._receipt(request, ReceiptReason.SAFE_STOP_FAILURE, before, detail=str(error))

    def stop(self, request_id: str, *, now_s: float, wall_s: float | None = None) -> ExecutionReceipt:
        return self._brake(request_id, now_s, wall_s, ControllerLifecycle.STOPPED)

    def hold(self, request_id: str, *, now_s: float, wall_s: float | None = None) -> ExecutionReceipt:
        return self._brake(request_id, now_s, wall_s, ControllerLifecycle.HOLDING)

    def reset(self, request_id: str, *, now_s: float) -> ExecutionReceipt:
        before = self.lifecycle
        if request_id in self._seen:
            return self._reject(request_id, ReceiptReason.DUPLICATE_REQUEST)
        if before not in {ControllerLifecycle.STOPPED, ControllerLifecycle.HOLDING, ControllerLifecycle.FAULTED}:
            return self._reject(request_id, ReceiptReason.INVALID_LIFECYCLE)
        try:
            state = self._fresh(now_s)
            if (
                max(map(abs, state.velocities)) > self.policy.stopped_velocity_rad_s
                or not self.transport.readiness()
                or not self.configuration_current()
            ):
                return self._reject(request_id, ReceiptReason.NOT_READY)
            if self._handle and self.transport.status(self._handle) is None:
                return self._reject(request_id, ReceiptReason.NOT_READY)
            state = self._fresh(self.transport.now_s())
            if max(map(abs, state.velocities)) > self.policy.stopped_velocity_rad_s:
                return self._reject(request_id, ReceiptReason.NOT_READY)
        except (ValueError, RuntimeError, TimeoutError) as error:
            return self._reject(request_id, ReceiptReason.NOT_READY, str(error))
        self._seen.add(request_id)
        self._handle = None
        self.lifecycle = ControllerLifecycle.READY
        receipt = self._receipt(request_id, ReceiptReason.ACCEPTED, before, accepted=True)
        self.emit({"event": "reset", "receipt": asdict(receipt), "state": asdict(state)})
        return receipt
