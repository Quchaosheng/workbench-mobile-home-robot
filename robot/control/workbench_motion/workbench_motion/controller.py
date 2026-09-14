"""ROS-free control boundary for joint state, commands, and lifecycle safety.

This module deliberately stops at the controller seam.  It does not import a
simulator or ROS client and does not claim that accepting a command means the
robot moved.  Concrete adapters implement the protected hooks and report real
feedback through :meth:`Controller.update_state`.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from .joint_limits import AcceptedTrajectory


class ControllerMode(StrEnum):
    IDLE = "idle"
    EXECUTING = "executing"
    HOLDING = "holding"
    STOPPED = "stopped"
    FAULTED = "faulted"


class CommandType(StrEnum):
    POSITION = "position"
    VELOCITY = "velocity"
    TRAJECTORY = "trajectory"
    HOLD = "hold"
    STOP = "stop"
    RESET = "reset"


class DispatchStatus(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"


def _finite_vector(values: Sequence[float], label: str) -> tuple[float, ...]:
    normalized: list[float] = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{label} must contain finite numbers")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError(f"{label} must contain finite numbers")
        normalized.append(converted)
    return tuple(normalized)


def _joint_names(names: Sequence[str]) -> tuple[str, ...]:
    if isinstance(names, str | bytes | bytearray):
        raise ValueError("joint_names must be a sequence of names, not a string")
    normalized = tuple(names)
    if not normalized or any(type(name) is not str or not name.strip() for name in normalized):
        raise ValueError("joint_names must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError("joint_names must be unique")
    return normalized


@dataclass(frozen=True, slots=True)
class RobotState:
    """Immutable feedback snapshot owned by a controller adapter."""

    joint_names: tuple[str, ...]
    positions: tuple[float, ...]
    velocities: tuple[float, ...] = ()
    accelerations: tuple[float, ...] = ()
    timestamp_ns: int = 0
    sequence: int = 0
    mode: ControllerMode = ControllerMode.IDLE
    active_command_id: str | None = None

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        positions = _finite_vector(self.positions, "positions")
        velocities = _finite_vector(self.velocities, "velocities")
        accelerations = _finite_vector(self.accelerations, "accelerations")
        if len(positions) != len(names):
            raise ValueError("positions must have the same length as joint_names")
        if any(len(values) not in (0, len(names)) for values in (velocities, accelerations)):
            raise ValueError("velocities and accelerations must be empty or match joint_names length")
        if isinstance(self.timestamp_ns, bool) or not isinstance(self.timestamp_ns, int) or self.timestamp_ns < 0:
            raise ValueError("timestamp_ns must be a non-negative integer")
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 0:
            raise ValueError("sequence must be a non-negative integer")
        if not isinstance(self.mode, ControllerMode):
            raise ValueError("mode must be ControllerMode")
        executing_or_holding = self.mode in {ControllerMode.EXECUTING, ControllerMode.HOLDING}
        if executing_or_holding != (self.active_command_id is not None):
            raise ValueError("EXECUTING or HOLDING state requires exactly one active_command_id")
        if self.active_command_id is not None and (
            type(self.active_command_id) is not str or not self.active_command_id.strip()
        ):
            raise ValueError("active_command_id must be a non-empty string or None")
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "positions", positions)
        object.__setattr__(self, "velocities", velocities)
        object.__setattr__(self, "accelerations", accelerations)


@dataclass(frozen=True, slots=True)
class RobotCommand:
    """Immutable low-level command created below the semantic-action boundary."""

    command_id: str
    command_type: CommandType
    joint_names: tuple[str, ...] = ()
    values: tuple[float, ...] = ()
    accepted_trajectory: AcceptedTrajectory | None = None

    def __post_init__(self) -> None:
        if type(self.command_id) is not str or not self.command_id.strip():
            raise ValueError("command_id must be a non-empty string")
        if not isinstance(self.command_type, CommandType):
            raise ValueError("command_type must be CommandType")
        names = () if not self.joint_names else _joint_names(self.joint_names)
        values = _finite_vector(self.values, "values")
        if self.command_type in {CommandType.POSITION, CommandType.VELOCITY}:
            if not names or len(values) != len(names):
                raise ValueError("joint_names and values must have the same length")
            if self.accepted_trajectory is not None:
                raise ValueError("position and velocity commands cannot contain a trajectory")
        elif self.command_type is CommandType.TRAJECTORY:
            from .joint_limits import AcceptedTrajectory

            if not isinstance(self.accepted_trajectory, AcceptedTrajectory):
                raise ValueError("trajectory command requires AcceptedTrajectory")
            trajectory_names = self.accepted_trajectory.snapshot.joint_names
            if names and names != trajectory_names:
                raise ValueError("trajectory joint_names must match AcceptedTrajectory")
            if values:
                raise ValueError("trajectory command cannot contain values")
            names = trajectory_names
        elif names or values or self.accepted_trajectory is not None:
            raise ValueError(f"{self.command_type.value} command cannot contain joint targets")
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "values", values)

    @classmethod
    def position(cls, command_id: str, joint_names: Sequence[str], positions: Sequence[float]) -> RobotCommand:
        return cls(command_id, CommandType.POSITION, joint_names, positions)

    @classmethod
    def velocity(cls, command_id: str, joint_names: Sequence[str], velocities: Sequence[float]) -> RobotCommand:
        return cls(command_id, CommandType.VELOCITY, joint_names, velocities)

    @classmethod
    def trajectory(cls, command_id: str, accepted: AcceptedTrajectory) -> RobotCommand:
        return cls(command_id, CommandType.TRAJECTORY, accepted_trajectory=accepted)

    @classmethod
    def hold(cls, command_id: str) -> RobotCommand:
        return cls(command_id, CommandType.HOLD)

    @classmethod
    def stop(cls, command_id: str) -> RobotCommand:
        return cls(command_id, CommandType.STOP)

    @classmethod
    def reset(cls, command_id: str) -> RobotCommand:
        return cls(command_id, CommandType.RESET)


@dataclass(frozen=True, slots=True)
class DispatchReceipt:
    """Boundary acknowledgement; it is not an execution or verification result."""

    command_id: str
    status: DispatchStatus
    reason: str | None
    state: RobotState


_MOTION_TYPES: Final = frozenset({CommandType.POSITION, CommandType.VELOCITY, CommandType.TRAJECTORY})


class Controller(ABC):
    """Lifecycle-safe controller port with replaceable hardware/simulator hooks."""

    def __init__(self, initial_state: RobotState) -> None:
        if not isinstance(initial_state, RobotState):
            raise TypeError("initial_state must be RobotState")
        self._state = initial_state

    @property
    def state(self) -> RobotState:
        return self._state

    def dispatch(self, command: RobotCommand) -> DispatchReceipt:
        if not isinstance(command, RobotCommand):
            return self._receipt("", DispatchStatus.REJECTED, "invalid_command")
        if command.command_type in _MOTION_TYPES:
            if command.joint_names != self._state.joint_names:
                return self._receipt(command.command_id, DispatchStatus.REJECTED, "joint_names")
            if self._state.mode is ControllerMode.STOPPED:
                return self._receipt(command.command_id, DispatchStatus.REJECTED, "stopped")
            if self._state.mode is ControllerMode.FAULTED:
                return self._receipt(command.command_id, DispatchStatus.REJECTED, "faulted")
            if self._state.mode is ControllerMode.EXECUTING:
                return self._receipt(command.command_id, DispatchStatus.REJECTED, "active_command")
            if self._state.mode is ControllerMode.HOLDING:
                return self._receipt(command.command_id, DispatchStatus.REJECTED, "holding")
            accepted, reason = self._dispatch_motion(command)
            if accepted:
                self._set_mode(ControllerMode.EXECUTING, command.command_id)
                return self._receipt(command.command_id, DispatchStatus.ACCEPTED, None)
            return self._receipt(command.command_id, DispatchStatus.REJECTED, reason or "dispatch_rejected")
        if command.command_type is CommandType.HOLD:
            return self._lifecycle_receipt(command.command_id, self.hold(), "hold_rejected")
        if command.command_type is CommandType.STOP:
            return self._lifecycle_receipt(command.command_id, self.safe_stop("commanded_stop"), "stop_rejected")
        if command.command_type is CommandType.RESET:
            return self._lifecycle_receipt(command.command_id, self.reset(), "reset_rejected")
        return self._receipt(command.command_id, DispatchStatus.REJECTED, "unsupported_command")

    def cancel(self, command_id: str) -> bool:
        if self._state.mode not in {ControllerMode.EXECUTING, ControllerMode.HOLDING}:
            return False
        if command_id != self._state.active_command_id:
            return False
        if not self._cancel_motion(command_id):
            return False
        self._set_mode(ControllerMode.IDLE, None)
        return True

    def hold(self) -> bool:
        """Pause the backend while retaining the active goal for cancel/resume."""
        if self._state.mode is not ControllerMode.EXECUTING:
            return False
        if not self._hold_motion():
            return False
        self._set_mode(ControllerMode.HOLDING, self._state.active_command_id)
        return True

    def resume(self) -> bool:
        if self._state.mode is not ControllerMode.HOLDING or self._state.active_command_id is None:
            return False
        if not self._resume_motion(self._state.active_command_id):
            return False
        self._set_mode(ControllerMode.EXECUTING, self._state.active_command_id)
        return True

    def safe_stop(self, reason: str) -> bool:
        if type(reason) is not str or not reason.strip():
            raise ValueError("safe-stop reason must be a non-empty string")
        if self._state.mode is ControllerMode.STOPPED:
            return True
        if not self._safe_stop_motion(reason):
            return False
        self._set_mode(ControllerMode.STOPPED, None)
        return True

    def reset(self) -> bool:
        if self._state.mode not in {ControllerMode.STOPPED, ControllerMode.FAULTED, ControllerMode.HOLDING}:
            return False
        if not self._reset_motion():
            return False
        self._set_mode(ControllerMode.IDLE, None)
        return True

    def update_state(self, state: RobotState) -> None:
        """Accept fresh feedback only when its joint schema and sequence advance."""
        if not isinstance(state, RobotState):
            raise TypeError("state must be RobotState")
        if state.joint_names != self._state.joint_names:
            raise ValueError("feedback joint_names do not match controller state")
        if state.sequence <= self._state.sequence:
            raise ValueError("feedback sequence must increase")
        if self._state.timestamp_ns and state.timestamp_ns < self._state.timestamp_ns:
            raise ValueError("feedback timestamp must not move backwards")
        if self._state.mode in {ControllerMode.EXECUTING, ControllerMode.HOLDING}:
            if state.mode is ControllerMode.IDLE:
                raise ValueError("feedback cannot clear an active command by reporting IDLE")
            if state.active_command_id != self._state.active_command_id:
                raise ValueError("feedback cannot replace the active command")
            if state.mode is not self._state.mode:
                raise ValueError("feedback cannot change the active lifecycle mode")
        elif state.mode is not self._state.mode or state.active_command_id != self._state.active_command_id:
            raise ValueError("feedback cannot change the controller lifecycle")
        self._state = state

    def _set_mode(self, mode: ControllerMode, active_command_id: str | None) -> None:
        self._state = replace(self._state, mode=mode, active_command_id=active_command_id)

    def _receipt(self, command_id: str, status: DispatchStatus, reason: str | None) -> DispatchReceipt:
        return DispatchReceipt(command_id, status, reason, self._state)

    def _lifecycle_receipt(self, command_id: str, accepted: bool, reason: str) -> DispatchReceipt:
        status = DispatchStatus.ACCEPTED if accepted else DispatchStatus.REJECTED
        return self._receipt(command_id, status, None if accepted else reason)

    @abstractmethod
    def _dispatch_motion(self, command: RobotCommand) -> tuple[bool, str | None]:
        """Send a position, velocity, or accepted trajectory to a backend."""

    def _cancel_motion(self, command_id: str) -> bool:
        return False

    def _hold_motion(self) -> bool:
        return False

    def _resume_motion(self, command_id: str) -> bool:
        return False

    def _safe_stop_motion(self, reason: str) -> bool:
        return False

    def _reset_motion(self) -> bool:
        return False


__all__ = [
    "CommandType",
    "Controller",
    "ControllerMode",
    "DispatchReceipt",
    "DispatchStatus",
    "RobotCommand",
    "RobotState",
]
