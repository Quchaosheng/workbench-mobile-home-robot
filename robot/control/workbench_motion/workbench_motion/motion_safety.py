"""ROS-free full-derivative interpolation gate and bounded braking references.

The existing preflight owns normalization and immutable AcceptedTrajectory.
This additional execution gate checks every quintic interval, including its
interior extrema. It never dispatches and never infers missing motion limits.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from itertools import pairwise
from pathlib import Path

import yaml

from workbench_motion.joint_limits import AcceptedTrajectory, PreflightContext, Violation, preflight_trajectory
from workbench_motion.motion_types import RobotState
from workbench_motion.quintic_trajectory import JointMotionLimit, TrajectorySample, quintic_point_to_point


@dataclass(frozen=True)
class MotionPolicy:
    max_state_age_s: float = 0.2
    max_feedback_wall_age_s: float = 0.5
    max_command_age_s: float = 0.5
    start_position_tolerance_rad: float = 0.005
    start_velocity_tolerance_rad_s: float = 0.01
    tracking_tolerance_rad: float = 0.05
    goal_tolerance_rad: float = 0.02
    goal_time_s: float = 0.5
    stopped_velocity_rad_s: float = 0.01
    stopped_dwell_s: float = 0.2
    max_braking_time_s: float = 5.0
    collision_resolution_rad: float = 0.05
    dispatch_lead_s: float = 0.25
    dispatch_margin_s: float = 0.02

    def __post_init__(self):
        for name, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.dispatch_margin_s >= self.dispatch_lead_s:
            raise ValueError("dispatch margin must be smaller than lead time")


def load_motion_config(path: Path, context: PreflightContext) -> tuple[MotionPolicy, dict[str, JointMotionLimit]]:
    payload = yaml.safe_load(path.read_text())
    if (
        not isinstance(payload, dict)
        or set(payload) != {"version", "policy", "joint_limits"}
        or payload["version"] != 1
    ):
        raise ValueError("invalid motion configuration")
    policy = MotionPolicy(**payload["policy"])
    limits = {name: JointMotionLimit(**fields) for name, fields in payload["joint_limits"].items()}
    validate_limits(context, limits)
    return policy, limits


def validate_limits(context: PreflightContext, limits: Mapping[str, JointMotionLimit]) -> None:
    if set(limits) != set(context.expected_joint_names):
        raise ValueError("motion limits must exactly match controlled joints")
    hard = dict(context.effective_limits)
    for name, limit in limits.items():
        # Reuse the existing generator's strict finite/type/interval validation.
        quintic_point_to_point((name,), (limit.min_position,), (limit.min_position,), {name: limit})
        if (
            limit.min_position < hard[name].min_position
            or limit.max_position > hard[name].max_position
            or limit.max_velocity > hard[name].max_velocity
        ):
            raise ValueError("motion limits cannot relax effective hardware limits")


def _value(coefficients: tuple[float, ...], t: float) -> float:
    result = 0.0
    for coefficient in reversed(coefficients):
        result = result * t + coefficient
    return result


def _derivative(coefficients: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(i * c for i, c in enumerate(coefficients) if i) or (0.0,)


def _roots(coefficients: tuple[float, ...]) -> list[float]:
    """Isolate real polynomial roots on [0,1] using derivative partitions."""
    while len(coefficients) > 1 and coefficients[-1] == 0:
        coefficients = coefficients[:-1]
    if len(coefficients) == 1:
        return []
    partitions = [0.0, *_roots(_derivative(coefficients)), 1.0]
    result = [t for t in partitions if _value(coefficients, t) == 0]
    for left, right in pairwise(partitions):
        a, b = _value(coefficients, left), _value(coefficients, right)
        if (a < 0 < b) or (b < 0 < a):
            for _ in range(64):
                middle = (left + right) / 2
                c = _value(coefficients, middle)
                if c == 0:
                    left = right = middle
                    break
                if (a < 0 < c) or (c < 0 < a):
                    right = middle
                else:
                    left, a = middle, c
            result.append((left + right) / 2)
    return sorted(set(result))


def _coefficients(start, end, index: int, duration: float) -> tuple[float, ...]:
    q0, q1 = start.positions[index], end.positions[index]
    v0, v1 = start.velocities[index] * duration, end.velocities[index] * duration
    a0, a1 = start.accelerations[index] * duration**2, end.accelerations[index] * duration**2
    delta = q1 - q0
    return (
        q0,
        v0,
        a0 / 2,
        10 * delta - 6 * v0 - 4 * v1 - 1.5 * a0 + 0.5 * a1,
        -15 * delta + 8 * v0 + 7 * v1 + 1.5 * a0 - a1,
        6 * delta - 3 * v0 - 3 * v1 - 0.5 * a0 + 0.5 * a1,
    )


def _raw(points, names):
    return {
        "joint_names": list(names),
        "points": [
            {
                "positions": list(point.positions),
                "velocities": list(point.velocities),
                "accelerations": list(point.accelerations),
                "effort": list(point.effort),
                "time_from_start": {
                    "sec": point.time_from_start_ns // 1_000_000_000,
                    "nanosec": point.time_from_start_ns % 1_000_000_000,
                },
            }
            for point in points
        ],
    }


def accept_motion(
    raw, state: RobotState, context: PreflightContext, limits: Mapping[str, JointMotionLimit]
) -> AcceptedTrajectory:
    validate_limits(context, limits)
    if isinstance(raw, AcceptedTrajectory):
        original = raw
        raw = _raw(raw.snapshot.points, raw.snapshot.joint_names)
    else:
        original = None
    accepted = preflight_trajectory(raw, state.position_by_joint, context=context)
    if isinstance(accepted, Violation):
        raise ValueError(f"{accepted.kind}: {accepted.message}")
    if original is not None and (
        original.canonical_bytes != accepted.canonical_bytes
        or original.trajectory_sha256 != accepted.trajectory_sha256
        or original.context_sha256 != accepted.context_sha256
    ):
        raise ValueError("accepted trajectory evidence changed")
    points = accepted.snapshot.points
    if len(points) < 2 or points[0].time_from_start_ns != 0:
        raise ValueError("motion needs a t=0 anchor and a positive interval")
    size = len(state.joint_names)
    if any(len(p.velocities) != size or len(p.accelerations) != size or p.effort for p in points):
        raise ValueError("complete velocity/acceleration derivatives and no effort are required")
    if any(v != 0 for v in (*points[-1].velocities, *points[-1].accelerations)):
        raise ValueError("M1 trajectories must end at rest")
    for start, end in pairwise(points):
        duration = (end.time_from_start_ns - start.time_from_start_ns) / 1e9
        for index, name in enumerate(state.joint_names):
            limit = limits[name]
            coefficients = _coefficients(start, end, index, duration)
            bounds = (
                (limit.min_position, limit.max_position),
                (-limit.max_velocity, limit.max_velocity),
                (-limit.max_acceleration, limit.max_acceleration),
                (-limit.max_jerk, limit.max_jerk),
            )
            for derivative, (low, high) in enumerate(bounds):
                values = [
                    _value(coefficients, t) / duration**derivative
                    for t in (0.0, *_roots(_derivative(coefficients)), 1.0)
                ]
                if any(not math.isfinite(v) or v < low or v > high for v in values):
                    label = ("position", "velocity", "acceleration", "jerk")[derivative]
                    raise ValueError(f"continuous {label} limit: {name}")
                coefficients = _derivative(coefficients)
    return accepted


def _point(q, v, a, ns):
    return {
        "positions": list(q),
        "velocities": list(v),
        "accelerations": list(a),
        "time_from_start": {"sec": ns // 1_000_000_000, "nanosec": ns % 1_000_000_000},
    }


def point_to_point(
    state: RobotState, target: tuple[float, ...], context: PreflightContext, limits: Mapping[str, JointMotionLimit]
) -> AcceptedTrajectory:
    validate_limits(context, limits)
    profile = quintic_point_to_point(state.joint_names, state.positions, target, limits)
    # Round upward to ROS nanoseconds with small arithmetic headroom. Generate
    # the exact same rest-to-rest polynomial JTC reconstructs from q/v/a.
    ns = max(100_000_000, math.ceil(profile.duration_s * 1.000001 * 1e9))
    zero = (0.0,) * len(state.joint_names)
    raw = {
        "joint_names": state.joint_names,
        "points": [_point(state.positions, zero, zero, 0), _point(target, zero, zero, ns)],
    }
    return accept_motion(raw, state, context, limits)


def braking_trajectory(
    state: RobotState, context: PreflightContext, limits: Mapping[str, JointMotionLimit], policy: MotionPolicy
) -> AcceptedTrajectory:
    if state.accelerations is None:
        raise ValueError("braking requires an observed acceleration estimate")
    for points in _brake_candidates(state, 0, policy):
        try:
            return accept_motion({"joint_names": state.joint_names, "points": points}, state, context, limits)
        except ValueError:
            continue
    raise ValueError("no feasible bounded braking trajectory")


def _brake_candidates(start, offset_ns, policy):
    duration = 0.05
    zero = (0.0,) * len(start.positions)
    while duration <= policy.max_braking_time_s:
        ns = math.ceil(duration * 1e9)
        duration = ns / 1e9
        target = tuple(
            q + v * duration / 2 + a * duration**2 / 12
            for q, v, a in zip(start.positions, start.velocities, start.accelerations, strict=True)
        )
        yield [
            _point(start.positions, start.velocities, start.accelerations, offset_ns),
            _point(target, zero, zero, offset_ns + ns),
        ]
        duration *= 1.25


def with_start_hold(trajectory, state, context, limits, lead_s):
    """Reserve dispatch time without relying on an implicit transport epoch."""
    if not math.isfinite(lead_s) or lead_s <= 0:
        raise ValueError("lead time must be finite and positive")
    first = trajectory.snapshot.points[0]
    if any(v != 0 for v in (*first.velocities, *first.accelerations)):
        raise ValueError("scheduled motion must start at rest")
    ns = math.ceil(lead_s * 1e9)
    points = (first, *(replace(p, time_from_start_ns=p.time_from_start_ns + ns) for p in trajectory.snapshot.points))
    return accept_motion(_raw(points, trajectory.snapshot.joint_names), state, context, limits)


def splice_braking_trajectory(active, splice_s, state, context, limits, policy):
    """Keep the accepted prefix and join a C2 brake at a future reference knot.

    The sampled q/v/a are reference data, never a measured RobotState. State
    remains the independent feedback used by the executor's tracking gate.
    """
    if not math.isfinite(splice_s) or splice_s <= 0:
        raise ValueError("splice time must be finite and positive")
    ns = math.ceil(splice_s * 1e9)
    reference = sample_trajectory(active, ns / 1e9)
    prefix = [p for p in active.snapshot.points if p.time_from_start_ns < ns]
    # Preflight's t=0 evidence is the original accepted anchor, not a new
    # observation at the historic time. No synthetic state is emitted.
    anchor = replace(state, positions=active.snapshot.points[0].positions)
    raw_prefix = _raw(prefix, active.snapshot.joint_names)["points"]
    for tail in _brake_candidates(reference, ns, policy):
        raw = {"joint_names": active.snapshot.joint_names, "points": raw_prefix + tail}
        try:
            return accept_motion(raw, anchor, context, limits)
        except ValueError:
            continue
    raise ValueError("no feasible bounded braking trajectory")


def sample_trajectory(trajectory: AcceptedTrajectory, time_s: float) -> TrajectorySample:
    if not math.isfinite(time_s) or time_s < 0:
        raise ValueError("sample time must be finite and nonnegative")
    points = trajectory.snapshot.points
    if time_s > points[-1].time_from_start_ns / 1e9:
        last = points[-1]
        return TrajectorySample(last.positions, last.velocities, last.accelerations, (0.0,) * len(last.positions))
    for start, end in pairwise(points):
        if time_s <= end.time_from_start_ns / 1e9:
            duration = (end.time_from_start_ns - start.time_from_start_ns) / 1e9
            t = (time_s - start.time_from_start_ns / 1e9) / duration
            columns = [[] for _ in range(4)]
            for index in range(len(start.positions)):
                coefficients = _coefficients(start, end, index, duration)
                for derivative, values in enumerate(columns):
                    values.append(_value(coefficients, t) / duration**derivative)
                    coefficients = _derivative(coefficients)
            return TrajectorySample(*(tuple(values) for values in columns))
    raise ValueError("trajectory has no sampleable interval")
