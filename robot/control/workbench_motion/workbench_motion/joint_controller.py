"""ROS-free joint-space planning and tracking measurement primitives.

This module is deliberately local to the motion-control boundary.  It neither
dispatches a command nor observes a simulator or robot; adapters own those
effects and must translate through the existing internal motion ports.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

from workbench_motion.motion_types import CommandMode, RobotCommand, RobotState
from workbench_motion.quintic_trajectory import (
    JointMotionLimit,
    QuinticTrajectory,
    quintic_point_to_point,
)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    try:
        number = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


def _joint_values(value: object, label: str, joint_count: int) -> tuple[float, ...]:
    if not isinstance(value, tuple) or len(value) != joint_count:
        raise ValueError(f"{label} must be a tuple matching joint_names")
    return tuple(_finite_number(item, label) for item in value)


def _copy_limits(
    joint_names: tuple[str, ...], limits: Mapping[str, JointMotionLimit]
) -> MappingProxyType[str, JointMotionLimit]:
    if not isinstance(limits, Mapping) or set(limits) != set(joint_names):
        raise ValueError("limits must contain exactly the configured joint names")

    copied: dict[str, JointMotionLimit] = {}
    for joint_name in joint_names:
        limit = limits[joint_name]
        if not isinstance(limit, JointMotionLimit):
            raise ValueError(f"{joint_name} limit must be JointMotionLimit")
        min_position = _finite_number(limit.min_position, f"{joint_name}.min_position")
        max_position = _finite_number(limit.max_position, f"{joint_name}.max_position")
        max_velocity = _finite_number(limit.max_velocity, f"{joint_name}.max_velocity")
        max_acceleration = _finite_number(limit.max_acceleration, f"{joint_name}.max_acceleration")
        max_jerk = _finite_number(limit.max_jerk, f"{joint_name}.max_jerk")
        if min_position > max_position:
            raise ValueError(f"{joint_name} min_position must not exceed max_position")
        if max_velocity <= 0 or max_acceleration <= 0 or max_jerk <= 0:
            raise ValueError(f"{joint_name} motion limits must be positive")
        copied[joint_name] = JointMotionLimit(
            min_position,
            max_position,
            max_velocity,
            max_acceleration,
            max_jerk,
        )
    return MappingProxyType(copied)


def _validate_state(
    state: RobotState,
    robot_id: str,
    joint_names: tuple[str, ...],
    limits: Mapping[str, JointMotionLimit],
) -> None:
    if not isinstance(state, RobotState):
        raise TypeError("state must be RobotState")
    if state.robot_id != robot_id:
        raise ValueError("state robot_id must match controller")
    if state.joint_names != joint_names:
        raise ValueError("state joint_names must match controller order")
    for joint_name, position, velocity in zip(joint_names, state.positions, state.velocities, strict=True):
        limit = limits[joint_name]
        if not limit.min_position <= position <= limit.max_position:
            raise ValueError(f"{joint_name} feedback position is outside its explicit limits")
        if abs(velocity) > limit.max_velocity:
            raise ValueError(f"{joint_name} feedback velocity is outside its explicit velocity limit")


@dataclass(frozen=True, slots=True)
class TrackingSample:
    """Desired setpoints and finite observed joint measurements at one sample."""

    relative_time_s: float
    observed_state: RobotState
    desired_positions: tuple[float, ...]
    desired_velocities: tuple[float, ...]
    desired_accelerations: tuple[float, ...]
    desired_jerks: tuple[float, ...]
    observed_accelerations: tuple[float, ...]
    observed_jerks: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.observed_state, RobotState):
            raise TypeError("observed_state must be RobotState")
        relative_time_s = _finite_number(self.relative_time_s, "relative_time_s")
        if relative_time_s < 0:
            raise ValueError("relative_time_s must be non-negative")
        joint_count = len(self.observed_state.joint_names)
        object.__setattr__(self, "relative_time_s", relative_time_s)
        object.__setattr__(
            self,
            "desired_positions",
            _joint_values(self.desired_positions, "desired_positions", joint_count),
        )
        object.__setattr__(
            self,
            "desired_velocities",
            _joint_values(self.desired_velocities, "desired_velocities", joint_count),
        )
        object.__setattr__(
            self,
            "desired_accelerations",
            _joint_values(self.desired_accelerations, "desired_accelerations", joint_count),
        )
        object.__setattr__(
            self,
            "desired_jerks",
            _joint_values(self.desired_jerks, "desired_jerks", joint_count),
        )
        object.__setattr__(
            self,
            "observed_accelerations",
            _joint_values(self.observed_accelerations, "observed_accelerations", joint_count),
        )
        object.__setattr__(
            self,
            "observed_jerks",
            _joint_values(self.observed_jerks, "observed_jerks", joint_count),
        )


@dataclass(frozen=True, slots=True)
class TrackingSummary:
    """Deterministic offline metrics from observed tracking samples."""

    robot_id: str
    joint_names: tuple[str, ...]
    final_position_errors: Mapping[str, float]
    final_velocity_errors: Mapping[str, float]
    rms_position_error: float
    max_position_error: float
    rms_velocity_error: float
    max_velocity_error: float
    peak_acceleration: float
    peak_jerk: float
    integrated_squared_jerk: float
    settling_time_s: float | None


class JointController:
    """Validate bounded joint requests without dispatching or executing them."""

    def __init__(self, robot_id: str, joint_names: tuple[str, ...], limits: Mapping[str, JointMotionLimit]) -> None:
        identity = RobotState(
            robot_id,
            joint_names,
            (0.0,) * len(joint_names),
            (0.0,) * len(joint_names),
            0.0,
        )
        self._robot_id = identity.robot_id
        self._joint_names = identity.joint_names
        self._limits = _copy_limits(self._joint_names, limits)

    @property
    def robot_id(self) -> str:
        return self._robot_id

    @property
    def joint_names(self) -> tuple[str, ...]:
        return self._joint_names

    def plan_position(self, state: RobotState, target_positions: tuple[float, ...]) -> QuinticTrajectory:
        """Build a limit-proven quintic profile; it does not submit the profile."""
        _validate_state(state, self._robot_id, self._joint_names, self._limits)
        if any(state.velocities):
            raise ValueError("rest-to-rest position planning requires zero feedback velocities")
        target = _joint_values(target_positions, "target_positions", len(self._joint_names))
        return quintic_point_to_point(self._joint_names, state.positions, target, self._limits)

    def velocity_command(
        self,
        command_id: str,
        state: RobotState,
        values: tuple[float, ...],
        *,
        issued_at_s: float,
    ) -> RobotCommand:
        """Create a bounded internal velocity command without dispatching it."""
        _validate_state(state, self._robot_id, self._joint_names, self._limits)
        velocities = _joint_values(values, "values", len(self._joint_names))
        for joint_name, velocity in zip(self._joint_names, velocities, strict=True):
            if abs(velocity) > self._limits[joint_name].max_velocity:
                raise ValueError(f"{joint_name} velocity limit exceeded")
        return RobotCommand(
            command_id,
            self._robot_id,
            self._joint_names,
            CommandMode.VELOCITY,
            velocities,
            issued_at_s,
        )


def summarize_tracking(
    samples: Sequence[TrackingSample], *, position_tolerance: float, velocity_tolerance: float
) -> TrackingSummary:
    """Summarize finite tracking data using a right-endpoint jerk-energy sum.

    Each interval contributes ``dt * sum(jerk_end**2)``.  This causal,
    deterministic convention does not infer an unobserved jerk before the
    first sample and is intentionally distinct from trapezoidal integration.
    """
    if isinstance(samples, str | bytes | bytearray):
        raise ValueError("samples must be a non-empty sequence")
    normalized = tuple(samples)
    if not normalized or any(not isinstance(sample, TrackingSample) for sample in normalized):
        raise ValueError("samples must be a non-empty sequence of TrackingSample")
    position_bound = _finite_number(position_tolerance, "position_tolerance")
    velocity_bound = _finite_number(velocity_tolerance, "velocity_tolerance")
    if position_bound < 0 or velocity_bound < 0:
        raise ValueError("tracking tolerances must be non-negative")

    first_state = normalized[0].observed_state
    robot_id = first_state.robot_id
    joint_names = first_state.joint_names
    position_square_sum = 0.0
    velocity_square_sum = 0.0
    max_position_error = 0.0
    max_velocity_error = 0.0
    peak_acceleration = 0.0
    peak_jerk = 0.0
    integrated_squared_jerk = 0.0
    final_position_values: tuple[float, ...] = ()
    final_velocity_values: tuple[float, ...] = ()
    latest_out_of_tolerance_index: int | None = None

    for index, sample in enumerate(normalized):
        if index:
            prior = normalized[index - 1]
            if sample.relative_time_s <= prior.relative_time_s:
                raise ValueError("sample times must be strictly increasing")
            integrated_squared_jerk += (sample.relative_time_s - prior.relative_time_s) * sum(
                jerk * jerk for jerk in sample.observed_jerks
            )
        if sample.observed_state.robot_id != robot_id or sample.observed_state.joint_names != joint_names:
            raise ValueError("tracking samples must share robot identity and joint order")

        position_errors = tuple(
            desired - observed
            for desired, observed in zip(sample.desired_positions, sample.observed_state.positions, strict=True)
        )
        velocity_errors = tuple(
            desired - observed
            for desired, observed in zip(sample.desired_velocities, sample.observed_state.velocities, strict=True)
        )
        final_position_values = position_errors
        final_velocity_values = velocity_errors
        for position_error, velocity_error in zip(position_errors, velocity_errors, strict=True):
            position_square_sum += position_error * position_error
            velocity_square_sum += velocity_error * velocity_error
            max_position_error = max(max_position_error, abs(position_error))
            max_velocity_error = max(max_velocity_error, abs(velocity_error))
            if abs(position_error) > position_bound or abs(velocity_error) > velocity_bound:
                latest_out_of_tolerance_index = index
        peak_acceleration = max(peak_acceleration, *(abs(value) for value in sample.observed_accelerations))
        peak_jerk = max(peak_jerk, *(abs(value) for value in sample.observed_jerks))

    final_position_errors = MappingProxyType(dict(zip(joint_names, final_position_values, strict=True)))
    final_velocity_errors = MappingProxyType(dict(zip(joint_names, final_velocity_values, strict=True)))
    measurement_count = len(normalized) * len(joint_names)
    settling_index = 0 if latest_out_of_tolerance_index is None else latest_out_of_tolerance_index + 1
    settling_time_s = None if settling_index == len(normalized) else normalized[settling_index].relative_time_s

    return TrackingSummary(
        robot_id=robot_id,
        joint_names=joint_names,
        final_position_errors=final_position_errors,
        final_velocity_errors=final_velocity_errors,
        rms_position_error=math.sqrt(position_square_sum / measurement_count),
        max_position_error=max_position_error,
        rms_velocity_error=math.sqrt(velocity_square_sum / measurement_count),
        max_velocity_error=max_velocity_error,
        peak_acceleration=peak_acceleration,
        peak_jerk=peak_jerk,
        integrated_squared_jerk=integrated_squared_jerk,
        settling_time_s=settling_time_s,
    )


__all__ = ["JointController", "TrackingSample", "TrackingSummary", "summarize_tracking"]
