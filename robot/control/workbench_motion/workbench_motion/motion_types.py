"""ROS-free values for the internal motion-control boundary.

These types deliberately represent controller-local data only. They are not a
Task or Agent Runtime contract and cannot claim robot, simulator, or verifier
execution. Hardware and simulator adapters must translate at this boundary.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum


class CommandMode(StrEnum):
    """Controller command representations permitted by this internal port."""

    POSITION = "position"
    VELOCITY = "velocity"


class ControllerLifecycle(StrEnum):
    """Controller-local lifecycle; adapters own their transport lifecycle."""

    READY = "ready"
    HOLDING = "holding"
    STOPPED = "stopped"
    FAULTED = "faulted"


class ReceiptStatus(StrEnum):
    """Whether a request crossed this port's local admission gate."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


class ReceiptReason(StrEnum):
    """Stable reasons for local admission outcomes; values are append-only."""

    ACCEPTED = "accepted"
    NO_STATE = "no_state"
    STALE_STATE = "stale_state"
    ROBOT_ID_MISMATCH = "robot_id_mismatch"
    JOINT_SCHEMA_MISMATCH = "joint_schema_mismatch"
    DUPLICATE_REQUEST = "duplicate_request"
    INVALID_LIFECYCLE = "invalid_lifecycle"
    INVALID_TRAJECTORY = "invalid_trajectory"
    START_STATE_DISCONTINUITY = "start_state_discontinuity"
    SAFE_STOP_FAILURE = "safe_stop_failure"


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
    """Finite named-joint feedback observed at one controller-clock instant."""

    robot_id: str
    joint_names: tuple[str, ...]
    positions: tuple[float, ...]
    velocities: tuple[float, ...]
    observed_at_s: float

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        object.__setattr__(self, "robot_id", _nonblank(self.robot_id, "robot_id"))
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "positions", _finite_values(self.positions, "positions", len(names)))
        object.__setattr__(self, "velocities", _finite_values(self.velocities, "velocities", len(names)))
        object.__setattr__(self, "observed_at_s", _finite_timestamp(self.observed_at_s, "observed_at_s"))

    @property
    def position_by_joint(self) -> dict[str, float]:
        """Return a fresh mapping; the ordered tuple remains canonical."""
        return dict(zip(self.joint_names, self.positions, strict=True))

    @property
    def velocity_by_joint(self) -> dict[str, float]:
        """Return a fresh mapping; the ordered tuple remains canonical."""
        return dict(zip(self.joint_names, self.velocities, strict=True))


@dataclass(frozen=True, slots=True)
class RobotCommand:
    """A bounded controller-local joint command, never a task-level action."""

    command_id: str
    robot_id: str
    joint_names: tuple[str, ...]
    mode: CommandMode
    values: tuple[float, ...]
    issued_at_s: float

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        object.__setattr__(self, "command_id", _nonblank(self.command_id, "command_id"))
        object.__setattr__(self, "robot_id", _nonblank(self.robot_id, "robot_id"))
        if not isinstance(self.mode, CommandMode):
            raise TypeError("mode must be CommandMode")
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "values", _finite_values(self.values, "values", len(names)))
        object.__setattr__(self, "issued_at_s", _finite_timestamp(self.issued_at_s, "issued_at_s"))


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    """Admission evidence, explicitly distinct from downstream execution facts."""

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
