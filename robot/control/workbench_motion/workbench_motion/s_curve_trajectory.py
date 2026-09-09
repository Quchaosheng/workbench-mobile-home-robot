"""ROS-free rest-to-rest jerk-limited S-curve trajectory generation.

This module is an internal planning primitive. It proves a shared-duration
joint-space profile from explicit limits, but does not command a controller or
communicate with a simulator or robot adapter.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from workbench_motion.quintic_trajectory import JointMotionLimit, TrajectorySample

_PHASE_COUNT = 7
_JERK_SIGNS = (1.0, 0.0, -1.0, 0.0, -1.0, 0.0, 1.0)


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


def _finite_positive(value: float, label: str) -> float:
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label} must be finite and positive")
    return value


def _minimum_phases(displacement: float, limit: JointMotionLimit) -> tuple[tuple[float, ...], float]:
    """Return a minimum-time unsigned profile as seven phase durations."""
    if displacement == 0.0:
        return (0.0,) * _PHASE_COUNT, 0.0

    velocity = limit.max_velocity
    acceleration = limit.max_acceleration
    jerk = limit.max_jerk
    acceleration_ramp = _finite_positive(acceleration / jerk, "acceleration ramp duration")
    velocity_ramp = _finite_positive(math.sqrt(velocity / jerk), "velocity ramp duration")

    if velocity_ramp <= acceleration_ramp:
        jerk_duration_at_velocity = velocity_ramp
        acceleration_plateau_at_velocity = 0.0
    else:
        jerk_duration_at_velocity = acceleration_ramp
        acceleration_plateau_at_velocity = _finite_positive(
            velocity / acceleration - acceleration_ramp,
            "velocity acceleration plateau duration",
        )

    peak_acceleration_at_velocity = jerk * jerk_duration_at_velocity
    distance_at_velocity = (
        peak_acceleration_at_velocity
        * (jerk_duration_at_velocity + acceleration_plateau_at_velocity)
        * (2.0 * jerk_duration_at_velocity + acceleration_plateau_at_velocity)
    )
    if not math.isfinite(distance_at_velocity):
        distance_at_velocity = math.inf
    if displacement > distance_at_velocity:
        velocity_plateau = _finite_positive(
            (displacement - distance_at_velocity) / velocity,
            "velocity plateau duration",
        )
        phases = (
            jerk_duration_at_velocity,
            acceleration_plateau_at_velocity,
            jerk_duration_at_velocity,
            velocity_plateau,
            jerk_duration_at_velocity,
            acceleration_plateau_at_velocity,
            jerk_duration_at_velocity,
        )
    else:
        distance_at_acceleration = 2.0 * acceleration * acceleration_ramp * acceleration_ramp
        if not math.isfinite(distance_at_acceleration):
            distance_at_acceleration = math.inf
        if displacement <= distance_at_acceleration:
            jerk_duration = _finite_positive(math.cbrt(displacement / (2.0 * jerk)), "jerk duration")
            phases = (jerk_duration, 0.0, jerk_duration, 0.0, jerk_duration, 0.0, jerk_duration)
        else:
            discriminant = acceleration_ramp * acceleration_ramp + 4.0 * displacement / acceleration
            acceleration_plateau = 0.5 * (math.sqrt(discriminant) - 3.0 * acceleration_ramp)
            acceleration_plateau = _finite_positive(acceleration_plateau, "acceleration plateau duration")
            phases = (
                acceleration_ramp,
                acceleration_plateau,
                acceleration_ramp,
                0.0,
                acceleration_ramp,
                acceleration_plateau,
                acceleration_ramp,
            )

    duration = math.fsum(phases)
    _finite_positive(duration, "trajectory duration")
    if any(not math.isfinite(phase) or phase < 0 for phase in phases):
        raise ValueError("phase durations must be finite and non-negative")
    return phases, duration


def _phase_rows(values: Sequence[Sequence[float]], label: str, expected_length: int) -> tuple[tuple[float, ...], ...]:
    if isinstance(values, str | bytes | bytearray):
        raise ValueError(f"{label} must be a sequence of phase rows")
    try:
        rows = tuple(tuple(_finite_number(value, label) for value in row) for row in values)
    except TypeError as exc:
        raise ValueError(f"{label} must be a sequence of phase rows") from exc
    if len(rows) != expected_length or any(len(row) != _PHASE_COUNT for row in rows):
        raise ValueError(f"{label} must contain one seven-phase row per joint")
    return rows


@dataclass(frozen=True, slots=True)
class SCurveTrajectory:
    """Shared-duration rest-to-rest joint-space S-curve trajectory."""

    joint_names: tuple[str, ...]
    start_positions: tuple[float, ...]
    target_positions: tuple[float, ...]
    duration_s: float
    phase_durations_s: tuple[tuple[float, ...], ...]
    phase_jerks: tuple[tuple[float, ...], ...]
    deltas: tuple[float, ...] = field(init=False)

    def __post_init__(self) -> None:
        names = _joint_names(self.joint_names)
        start = _positions(self.start_positions, "start_positions", len(names))
        target = _positions(self.target_positions, "target_positions", len(names))
        duration_s = _finite_number(self.duration_s, "duration_s")
        if duration_s < 0:
            raise ValueError("duration_s must be finite and non-negative")
        phases = _phase_rows(self.phase_durations_s, "phase_durations_s", len(names))
        jerks = _phase_rows(self.phase_jerks, "phase_jerks", len(names))
        if any(phase < 0 for row in phases for phase in row):
            raise ValueError("phase durations must be non-negative")
        if any(not math.isclose(math.fsum(row), duration_s, rel_tol=1e-12, abs_tol=1e-12) for row in phases):
            raise ValueError("every phase row must span duration_s")
        deltas = tuple(
            target_position - start_position for start_position, target_position in zip(start, target, strict=True)
        )
        if any(not math.isfinite(delta) for delta in deltas):
            raise ValueError("trajectory position deltas must be finite")
        if duration_s == 0.0 and (any(deltas) or any(phase for row in phases for phase in row)):
            raise ValueError("zero-duration trajectories require identical positions and zero phases")
        for start_position, target_position, durations, axis_jerks in zip(start, target, phases, jerks, strict=True):
            position, velocity, acceleration, _ = _sample_axis(start_position, duration_s, durations, axis_jerks)
            if not (
                math.isclose(position, target_position, rel_tol=1e-12, abs_tol=1e-12)
                and math.isclose(velocity, 0.0, rel_tol=0.0, abs_tol=1e-12)
                and math.isclose(acceleration, 0.0, rel_tol=0.0, abs_tol=1e-12)
            ):
                raise ValueError("phase profile must reach the target at rest")
        object.__setattr__(self, "joint_names", names)
        object.__setattr__(self, "start_positions", start)
        object.__setattr__(self, "target_positions", target)
        object.__setattr__(self, "duration_s", duration_s)
        object.__setattr__(self, "phase_durations_s", phases)
        object.__setattr__(self, "phase_jerks", jerks)
        object.__setattr__(self, "deltas", deltas)

    def sample(self, time_s: float) -> TrajectorySample:
        """Sample only within the closed planned interval without extrapolation."""
        time = _finite_number(time_s, "time_s")
        if time > self.duration_s:
            endpoint_tolerance = 8.0 * math.ulp(max(1.0, abs(time), self.duration_s))
            if time - self.duration_s <= endpoint_tolerance:
                time = self.duration_s
            else:
                raise ValueError("time_s must be within the closed trajectory interval")
        if time < 0:
            raise ValueError("time_s must be within the closed trajectory interval")
        if self.duration_s == 0.0:
            zero_values = (0.0,) * len(self.joint_names)
            return TrajectorySample(self.start_positions, zero_values, zero_values, zero_values)
        if time == self.duration_s:
            zero_values = (0.0,) * len(self.joint_names)
            return TrajectorySample(self.target_positions, zero_values, zero_values, zero_values)

        samples = tuple(
            _sample_axis(start, time, durations, jerks)
            for start, durations, jerks in zip(
                self.start_positions, self.phase_durations_s, self.phase_jerks, strict=True
            )
        )
        return TrajectorySample(
            tuple(sample[0] for sample in samples),
            tuple(sample[1] for sample in samples),
            tuple(sample[2] for sample in samples),
            tuple(sample[3] for sample in samples),
        )


def _sample_axis(
    start_position: float, time_s: float, phase_durations_s: tuple[float, ...], phase_jerks: tuple[float, ...]
) -> tuple[float, float, float, float]:
    """Integrate constant-jerk slots, selecting the next jerk at boundaries."""
    remaining = time_s
    position = start_position
    velocity = 0.0
    acceleration = 0.0
    for duration, jerk in zip(phase_durations_s, phase_jerks, strict=True):
        if remaining < duration:
            position += velocity * remaining + 0.5 * acceleration * remaining**2 + jerk * remaining**3 / 6.0
            velocity += acceleration * remaining + 0.5 * jerk * remaining**2
            acceleration += jerk * remaining
            return position, velocity, acceleration, jerk
        position += velocity * duration + 0.5 * acceleration * duration**2 + jerk * duration**3 / 6.0
        velocity += acceleration * duration + 0.5 * jerk * duration**2
        acceleration += jerk * duration
        remaining -= duration
    return position, velocity, acceleration, 0.0


def s_curve_point_to_point(
    joint_names: Sequence[str],
    start_positions: Sequence[float],
    target_positions: Sequence[float],
    limits: Mapping[str, JointMotionLimit],
) -> SCurveTrajectory:
    """Create a synchronized minimum-time rest-to-rest S-curve trajectory."""
    names = _joint_names(joint_names)
    start = _positions(start_positions, "start_positions", len(names))
    target = _positions(target_positions, "target_positions", len(names))
    normalized_limits = _normalized_limits(names, limits)

    minimum_profiles: list[tuple[tuple[float, ...], float, float]] = []
    for name, start_position, target_position, limit in zip(names, start, target, normalized_limits, strict=True):
        if not limit.min_position <= start_position <= limit.max_position:
            raise ValueError(f"{name} start position is outside its explicit limits")
        if not limit.min_position <= target_position <= limit.max_position:
            raise ValueError(f"{name} target position is outside its explicit limits")
        delta = target_position - start_position
        displacement = abs(delta)
        if not math.isfinite(displacement):
            raise ValueError(f"{name} displacement must be finite")
        phases, duration = _minimum_phases(displacement, limit)
        minimum_profiles.append((phases, duration, math.copysign(1.0, delta) if delta else 0.0))

    duration_s = max(duration for _, duration, _ in minimum_profiles)
    synchronized_phases: list[tuple[float, ...]] = []
    synchronized_jerks: list[tuple[float, ...]] = []
    for (phases, minimum_duration, direction), limit in zip(minimum_profiles, normalized_limits, strict=True):
        if minimum_duration == 0.0:
            synchronized_phases.append((0.0, 0.0, 0.0, duration_s, 0.0, 0.0, 0.0))
            synchronized_jerks.append((0.0,) * _PHASE_COUNT)
            continue
        scale = _finite_positive(duration_s / minimum_duration, "temporal scale")
        if scale < 1.0:
            raise ValueError("temporal scale must not shorten a minimum-time profile")
        scaled_phases = tuple(phase * scale for phase in phases)
        if any(not math.isfinite(phase) for phase in scaled_phases):
            raise ValueError("scaled phase duration must be finite")
        scaled_jerk = direction * limit.max_jerk / scale**3
        _finite_positive(abs(scaled_jerk), "scaled jerk")
        synchronized_phases.append(scaled_phases)
        synchronized_jerks.append(tuple(scaled_jerk * sign for sign in _JERK_SIGNS))

    return SCurveTrajectory(
        names,
        start,
        target,
        duration_s,
        tuple(synchronized_phases),
        tuple(synchronized_jerks),
    )


__all__ = ["SCurveTrajectory", "s_curve_point_to_point"]
