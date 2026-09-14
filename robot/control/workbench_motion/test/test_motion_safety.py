import math
from itertools import pairwise

import pytest
from workbench_motion.joint_limits import JointLimit, Violation, build_preflight_context
from workbench_motion.motion_safety import (
    MotionPolicy,
    accept_motion,
    braking_trajectory,
    point_to_point,
    sample_trajectory,
    splice_braking_trajectory,
    with_start_hold,
)
from workbench_motion.motion_types import RobotState
from workbench_motion.quintic_trajectory import JointMotionLimit

NAMES = ("j1", "j2")
LIMITS = {name: JointMotionLimit(-2, 2, 0.2, 0.5, 2.0) for name in NAMES}
CONTEXT = build_preflight_context(
    expected_joint_names=NAMES,
    hard_limits={name: JointLimit(-2, 2, 0.3, 20) for name in NAMES},
    override_limits={},
)


def state(q=(0.0, 0.0), v=(0.0, 0.0), a=(0.0, 0.0)):
    return RobotState("test", NAMES, q, v, 1.0, accelerations=a)


def test_quintic_materialization_preserves_derivatives_and_nanosecond_order():
    accepted = point_to_point(state(), (0.1, -0.05), CONTEXT, LIMITS)
    assert not isinstance(accepted, Violation)
    points = accepted.snapshot.points
    assert points[0].time_from_start_ns == 0
    assert all(a.time_from_start_ns < b.time_from_start_ns for a, b in pairwise(points))
    duration = points[-1].time_from_start_ns / 1e9
    assert sample_trajectory(accepted, duration).positions == pytest.approx((0.1, -0.05))
    for i in range(1001):
        sample = sample_trajectory(accepted, duration * i / 1000)
        assert max(map(abs, sample.velocities)) <= 0.2
        assert max(map(abs, sample.accelerations)) <= 0.5
        assert max(map(abs, sample.jerks)) <= 2.0


def test_position_only_and_between_waypoint_overshoot_are_rejected():
    raw = {
        "joint_names": NAMES,
        "points": [
            {"positions": (0.0, 0.0), "time_from_start": 0.0},
            {"positions": (0.1, 0.0), "time_from_start": 1.0},
        ],
    }
    with pytest.raises(ValueError, match="derivatives"):
        accept_motion(raw, state(), CONTEXT, LIMITS)
    for point in raw["points"]:
        point.update(velocities=(0.0, 0.0), accelerations=(0.0, 0.0))
    # Endpoints have zero derivatives, but interior jerk is 6 rad/s^3.
    with pytest.raises(ValueError, match=r"continuous (acceleration|jerk)"):
        accept_motion(raw, state(), CONTEXT, LIMITS)


def test_braking_starts_at_measured_velocity_and_acceleration():
    initial = state(v=(0.1, -0.08), a=(0.02, -0.03))
    accepted = braking_trajectory(initial, CONTEXT, LIMITS, MotionPolicy())
    first, last = accepted.snapshot.points
    assert first.positions == initial.positions
    assert first.velocities == initial.velocities
    assert first.accelerations == initial.accelerations
    assert last.velocities == (0.0, 0.0)
    assert last.accelerations == (0.0, 0.0)
    assert last.positions[0] > initial.positions[0]
    assert last.positions[1] < initial.positions[1]


def test_braking_refuses_missing_acceleration_and_insufficient_stopping_distance():
    with pytest.raises(ValueError, match="acceleration"):
        braking_trajectory(RobotState("test", NAMES, (0.0, 0.0), (0.1, 0.0), 1), CONTEXT, LIMITS, MotionPolicy())
    with pytest.raises(ValueError, match="braking"):
        braking_trajectory(state(q=(1.9999, 0.0), v=(0.19, 0.0)), CONTEXT, LIMITS, MotionPolicy())


def test_zero_motion_still_has_a_positive_hold_interval():
    accepted = point_to_point(state(), (0.0, 0.0), CONTEXT, LIMITS)
    assert accepted.snapshot.points[-1].time_from_start_ns > 0
    assert sample_trajectory(accepted, 0.0).velocities == (0.0, 0.0)


@pytest.mark.parametrize("splice_s", [0.1, 0.25, 0.65, 3.0])
def test_brake_splice_preserves_c2_prefix_and_all_continuous_limits(splice_s):
    trajectory = with_start_hold(point_to_point(state(), (0.1, -0.05), CONTEXT, LIMITS), state(), CONTEXT, LIMITS, 0.25)
    stopped = splice_braking_trajectory(trajectory, splice_s, state(), CONTEXT, LIMITS, MotionPolicy())
    for i in range(101):
        t = splice_s * i / 100
        expected, actual = sample_trajectory(trajectory, t), sample_trajectory(stopped, t)
        for field in ("positions", "velocities", "accelerations"):
            assert getattr(actual, field) == pytest.approx(getattr(expected, field), abs=1e-11)
    assert accept_motion(stopped, state(), CONTEXT, LIMITS) == stopped
    assert stopped.snapshot.points[-1].velocities == (0, 0)
    assert stopped.snapshot.points[-1].accelerations == (0, 0)


@pytest.mark.parametrize("value", [math.nan, math.inf, -1.0, 0.0, True])
def test_invalid_policy_fails_closed(value):
    with pytest.raises(ValueError):
        MotionPolicy(max_state_age_s=value)


def test_motion_limits_must_cover_all_joints_and_cannot_relax_preflight():
    with pytest.raises(ValueError):
        point_to_point(state(), (0.1, 0.0), CONTEXT, {"j1": LIMITS["j1"]})
    bad = {name: JointMotionLimit(-3, 3, 1, 1, 1) for name in NAMES}
    with pytest.raises(ValueError):
        point_to_point(state(), (0.1, 0.0), CONTEXT, bad)


@pytest.mark.parametrize("field", ["velocities", "accelerations"])
def test_nonstationary_terminal_derivatives_are_rejected(field):
    raw = {
        "joint_names": NAMES,
        "points": [
            {"positions": (0, 0), "velocities": (0, 0), "accelerations": (0, 0), "time_from_start": 0},
            {"positions": (0.1, 0), "velocities": (0, 0), "accelerations": (0, 0), "time_from_start": 2},
        ],
    }
    raw["points"][-1][field] = (0.05, 0)
    with pytest.raises(ValueError, match="end at rest"):
        accept_motion(raw, state(), CONTEXT, LIMITS)
