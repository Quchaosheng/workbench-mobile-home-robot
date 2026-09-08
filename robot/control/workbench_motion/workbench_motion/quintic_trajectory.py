"""ROS-free quintic point-to-point joint-space trajectory generation.

This module is an internal planning primitive.  It validates explicit motion
limits and samples an analytic profile, but it neither creates commands nor
talks to a controller, simulator, or robot adapter.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class JointMotionLimit:
    """Explicit position, velocity, acceleration, and jerk bounds for one joint."""

    min_position: float
    max_position: float
    max_velocity: float
    max_acceleration: float
    max_jerk: float


@dataclass(frozen=True, slots=True)
class TrajectorySample:
    """Analytic joint-space state at one time in a quintic profile."""

    positions: tuple[float, ...]
    velocities: tuple[float, ...]
    accelerations: tuple[float, ...]
    jerks: tuple[float, ...]


def _finite_number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{label} must be a finite number")
    try:
        result = float(value)
    except OverflowError as exc:
        raise ValueError(f"{label} must be a finite number") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _joint_names(joint_names: Sequence[str]) -> tuple[str, ...]:
    if isinstance(joint_names, str | bytes | bytearray):
        raise ValueError("joint_names must be a sequence of names, not a string")
    try:
        normalized = tuple(joint_names)
    except TypeError as exc:
        raise ValueError("joint_names must be a sequence of names") from exc
    if not normalized or any(type(name) is not str or not name.strip() for name in normalized):
        raise ValueError("joint_names must contain non-empty strings")
    if len(set(normalized)) != len(normalized):
        raise ValueError("joint_names must be unique")
    return normalized


def _positions(values: Sequence[float], label: str, expected_length: int) -> tuple[float, ...]:
    if isinstance(values, str | bytes | bytearray):
        raise ValueError(f"{label} must be a sequence of finite numbers")
    try:
        normalized = tuple(_finite_number(value, label) for value in values)
    except TypeError as exc:
        raise ValueError(f"{label} must be a sequence of finite numbers") from exc
    if len(normalized) != expected_length:
        raise ValueError(f"{label} must have the same length as joint_names")
    return normalized


def _normalized_limits(
    joint_names: tuple[str, ...], limits: Mapping[str, JointMotionLimit]
) -> tuple[JointMotionLimit, ...]:
    if not isinstance(limits, Mapping):
        raise ValueError("limits must be a mapping keyed by joint name")
    if set(limits) != set(joint_names):
        raise ValueError("limits must contain exactly the requested joint names")

    normalized: list[JointMotionLimit] = []
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
        normalized.append(JointMotionLimit(min_position, max_position, max_velocity, max_acceleration, max_jerk))
    return tuple(normalized)


@dataclass(frozen=True, slots=True)
class QuinticTrajectory:
    """Shared-duration, rest-to-rest joint-space quintic trajectory."""

    joint_names: tuple[str, ...]
    start_positions: tuple[float, ...]
    target_positions: tuple[float, ...]
    duration_s: float
    deltas: tuple[float, ...] = field(init=False)

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        start = _positions(self.start_positions, "start_positions", len(names))
        target = _positions(self.target_positions, "target_positions", len(names))
        duration_s = _finite_number(self.duration_s, "duration_s")
        if duration_s < 0:
            raise ValueError("duration_s must be finite and non-negative")
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "start_positions", start)
        object.__setattr__(self, "target_positions", target)
        object.__setattr__(self, "duration_s", duration_s)
        object.__setattr__(
            self,
            "deltas",
            tuple(
                target_position - start_position
                for start_position, target_position in zip(start, target, strict=True)
            ),
        )
        if any(not math.isfinite(delta) for delta in self.deltas):
            raise ValueError("trajectory position deltas must be finite")
        if duration_s == 0 and any(self.deltas):
            raise ValueError("zero-duration trajectories require identical positions")

    def sample(self, time_s: float) -> TrajectorySample:
        """Sample the profile only within its closed, planned time interval."""
        time = _finite_number(time_s, "time_s")
        if time < 0 or time > self.duration_s:
            raise ValueError("time_s must be within the closed trajectory interval")
        if self.duration_s == 0:
            zero_values = (0.0,) * len(self.joint_names)
            return TrajectorySample(
                self.start_positions,
                zero_values,
                zero_values,
                zero_values,
            )

        duration_squared = self.duration_s**2
        duration_cubed = duration_squared * self.duration_s
        if duration_squared == 0.0 or duration_cubed == 0.0:
            raise ValueError("time scale is too small to sample finite derivatives")

        normalized_time = time / self.duration_s
        s2 = normalized_time * normalized_time
        s3 = s2 * normalized_time
        s4 = s3 * normalized_time
        s5 = s4 * normalized_time
        position_scale = 10.0 * s3 - 15.0 * s4 + 6.0 * s5
        velocity_scale = (30.0 * s2 - 60.0 * s3 + 30.0 * s4) / self.duration_s
        acceleration_scale = (60.0 * normalized_time - 180.0 * s2 + 120.0 * s3) / duration_squared
        jerk_scale = (60.0 - 360.0 * normalized_time + 360.0 * s2) / duration_cubed
        sample = TrajectorySample(
            tuple(
                start + delta * position_scale
                for start, delta in zip(self.start_positions, self.deltas, strict=True)
            ),
            tuple(delta * velocity_scale for delta in self.deltas),
            tuple(delta * acceleration_scale for delta in self.deltas),
            tuple(delta * jerk_scale for delta in self.deltas),
        )
        if any(
            not math.isfinite(value)
            for values in (sample.positions, sample.velocities, sample.accelerations, sample.jerks)
            for value in values
        ):
            raise ValueError("trajectory sample must contain finite values")
        return sample


def quintic_point_to_point(
    joint_names: Sequence[str],
    start_positions: Sequence[float],
    target_positions: Sequence[float],
    limits: Mapping[str, JointMotionLimit],
) -> QuinticTrajectory:
    """Create a shared-duration rest-to-rest profile that respects all limits.

    All limits are required explicitly; absent, malformed, or looser implicit
    bounds are never inferred.  The returned trajectory has no execution side
    effects and must be separately integrated with preflight and a controller.
    """
    names = _joint_names(joint_names)
    start = _positions(start_positions, "start_positions", len(names))
    target = _positions(target_positions, "target_positions", len(names))
    normalized_limits = _normalized_limits(names, limits)

    duration_s = 0.0
    for name, start_position, target_position, limit in zip(names, start, target, normalized_limits, strict=True):
        if not limit.min_position <= start_position <= limit.max_position:
            raise ValueError(f"{name} start position is outside its explicit limits")
        if not limit.min_position <= target_position <= limit.max_position:
            raise ValueError(f"{name} target position is outside its explicit limits")
        displacement = abs(target_position - start_position)
        if not math.isfinite(displacement):
            raise ValueError(f"{name} displacement must be finite")
        if displacement == 0:
            continue
        candidate_durations = (
            (15.0 / 8.0) * (displacement / limit.max_velocity),
            (math.sqrt(10.0 / math.sqrt(3.0)) * math.sqrt(displacement)) / math.sqrt(limit.max_acceleration),
            (math.cbrt(60.0) * math.cbrt(displacement)) / math.cbrt(limit.max_jerk),
        )
        if any(not math.isfinite(candidate) or candidate <= 0 for candidate in candidate_durations):
            raise ValueError(f"{name} trajectory duration must be finite")
        duration_s = max(duration_s, *candidate_durations)

    if duration_s == 0.0 and any(
        start_position != target_position
        for start_position, target_position in zip(start, target, strict=True)
    ):
        raise ValueError("nonzero displacement requires a positive duration")
    return QuinticTrajectory(names, start, target, duration_s)


__all__ = ["JointMotionLimit", "QuinticTrajectory", "TrajectorySample", "quintic_point_to_point"]
