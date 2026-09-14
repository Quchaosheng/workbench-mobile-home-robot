"""ROS-free immutable values for the internal motion-control boundary."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class CommandMode(StrEnum):
    POSITION = "position"
    VELOCITY = "velocity"


class ControllerLifecycle(StrEnum):
    READY = "ready"
    EXECUTING = "executing"
    STOPPING = "stopping"
    HOLDING = "holding"
    STOPPED = "stopped"
    FAULTED = "faulted"


class ReceiptStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReceiptReason(StrEnum):
    ACCEPTED = "accepted"
    NO_STATE = "no_state"
    STALE_STATE = "stale_state"
    FUTURE_STATE = "future_state"
    STALE_COMMAND = "stale_command"
    FUTURE_COMMAND = "future_command"
    CLOCK_MISMATCH = "clock_mismatch"
    ROBOT_ID_MISMATCH = "robot_id_mismatch"
    JOINT_SCHEMA_MISMATCH = "joint_schema_mismatch"
    POSITION_LIMIT = "position_limit"
    VELOCITY_LIMIT = "velocity_limit"
    DUPLICATE_REQUEST = "duplicate_request"
    INVALID_LIFECYCLE = "invalid_lifecycle"
    INVALID_TRAJECTORY = "invalid_trajectory"
    TRAJECTORY_CONTEXT_MISMATCH = "trajectory_context_mismatch"
    START_STATE_DISCONTINUITY = "start_state_discontinuity"
    SAFE_STOP_FAILURE = "safe_stop_failure"
    UNSUPPORTED_MODE = "unsupported_mode"
    NOT_READY = "not_ready"
    COLLISION = "collision"
    SCENE_CHANGED = "scene_changed"
    BACKEND_REJECTED = "backend_rejected"
    BACKEND_FAILURE = "backend_failure"
    TRACKING_ERROR = "tracking_error"
    TIMEOUT = "timeout"
    FEEDBACK_LIMIT = "feedback_limit"


def _nonblank(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-blank string")
    return value


def _joint_names(value: object) -> tuple[str, ...]:
    if not isinstance(value, tuple) or not value:
        raise ValueError("joint_names must be a non-empty tuple")
    names = tuple(_nonblank(name, "joint name") for name in value)
    if len(names) != len(set(names)):
        raise ValueError("joint_names must be unique")
    return names


def _finite_values(value: object, label: str, size: int) -> tuple[float, ...]:
    if not isinstance(value, tuple) or len(value) != size:
        raise ValueError(f"{label} must be a tuple matching joint_names")
    normalized: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int | float):
            raise ValueError(f"{label} must contain finite numbers")
        number = float(item)
        if not math.isfinite(number):
            raise ValueError(f"{label} must contain finite numbers")
        normalized.append(number)
    return tuple(normalized)


def _finite_timestamp(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{label} must be a finite number")
    return normalized


@dataclass(frozen=True, slots=True)
class RobotState:
    robot_id: str
    joint_names: tuple[str, ...]
    positions: tuple[float, ...]
    velocities: tuple[float, ...]
    observed_at_s: float
    clock_id: str = "monotonic"
    accelerations: tuple[float, ...] | None = None

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        object.__setattr__(self, "robot_id", _nonblank(self.robot_id, "robot_id"))
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "positions", _finite_values(self.positions, "positions", len(names)))
        object.__setattr__(self, "velocities", _finite_values(self.velocities, "velocities", len(names)))
        object.__setattr__(self, "observed_at_s", _finite_timestamp(self.observed_at_s, "observed_at_s"))
        object.__setattr__(self, "clock_id", _nonblank(self.clock_id, "clock_id"))
        if self.accelerations is not None:
            object.__setattr__(self, "accelerations", _finite_values(self.accelerations, "accelerations", len(names)))

    @property
    def position_by_joint(self) -> dict[str, float]:
        return dict(zip(self.joint_names, self.positions, strict=True))

    @property
    def velocity_by_joint(self) -> dict[str, float]:
        return dict(zip(self.joint_names, self.velocities, strict=True))


@dataclass(frozen=True, slots=True)
class RobotCommand:
    command_id: str
    robot_id: str
    joint_names: tuple[str, ...]
    mode: CommandMode
    values: tuple[float, ...]
    issued_at_s: float
    clock_id: str = "monotonic"

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        object.__setattr__(self, "command_id", _nonblank(self.command_id, "command_id"))
        object.__setattr__(self, "robot_id", _nonblank(self.robot_id, "robot_id"))
        if not isinstance(self.mode, CommandMode):
            raise TypeError("mode must be CommandMode")
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "values", _finite_values(self.values, "values", len(names)))
        object.__setattr__(self, "issued_at_s", _finite_timestamp(self.issued_at_s, "issued_at_s"))
        object.__setattr__(self, "clock_id", _nonblank(self.clock_id, "clock_id"))


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    request_id: str
    status: ReceiptStatus
    reason: ReceiptReason
    lifecycle_before: ControllerLifecycle
    lifecycle_after: ControllerLifecycle
    dispatch_attempted: bool
    detail: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _nonblank(self.request_id, "request_id"))
        if not isinstance(self.status, ReceiptStatus):
            raise TypeError("status must be ReceiptStatus")
        if not isinstance(self.reason, ReceiptReason):
            raise TypeError("reason must be ReceiptReason")
        if not isinstance(self.lifecycle_before, ControllerLifecycle):
            raise TypeError("lifecycle_before must be ControllerLifecycle")
        if not isinstance(self.lifecycle_after, ControllerLifecycle):
            raise TypeError("lifecycle_after must be ControllerLifecycle")
        if not isinstance(self.dispatch_attempted, bool):
            raise TypeError("dispatch_attempted must be bool")
        if self.detail is not None and (not isinstance(self.detail, str) or not self.detail):
            raise ValueError("detail must be a non-empty string when present")


__all__ = [
    "CommandMode",
    "ControllerLifecycle",
    "ExecutionReceipt",
    "ReceiptReason",
    "ReceiptStatus",
    "RobotCommand",
    "RobotState",
]
