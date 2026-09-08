from __future__ import annotations

import math

import pytest
from workbench_motion.quintic_trajectory import JointMotionLimit, QuinticTrajectory, quintic_point_to_point

JOINTS = ("j1", "j2")
LIMITS = {
    "j1": JointMotionLimit(-2.0, 2.0, 1.875, 10.0 / math.sqrt(3.0), 60.0),
    "j2": JointMotionLimit(-2.0, 2.0, 4.0, 8.0, 80.0),
}


def generate(*, start=(0.0, 0.0), target=(1.0, -0.5), limits=LIMITS):
    return quintic_point_to_point(JOINTS, start, target, limits)


def test_profile_has_expected_endpoint_and_midpoint_derivatives():
    trajectory = generate()

    assert trajectory.duration_s == pytest.approx(1.0)
    assert trajectory.sample(0.0).positions == (0.0, 0.0)
    assert trajectory.sample(0.0).velocities == (0.0, 0.0)
    assert trajectory.sample(0.0).accelerations == (0.0, 0.0)
    assert trajectory.sample(1.0).positions == (1.0, -0.5)
    assert trajectory.sample(1.0).velocities == (0.0, 0.0)
    assert trajectory.sample(1.0).accelerations == (0.0, 0.0)

    midpoint = trajectory.sample(0.5)
    assert midpoint.positions == pytest.approx((0.5, -0.25))
    assert midpoint.velocities == pytest.approx((1.875, -0.9375))
    assert midpoint.accelerations == pytest.approx((0.0, 0.0))
    assert midpoint.jerks == pytest.approx((-30.0, 15.0))


def test_duration_is_selected_by_the_most_restrictive_joint_and_derivative_limit():
    limits = {
        "j1": JointMotionLimit(-2.0, 2.0, 100.0, 100.0, 100.0),
        "j2": JointMotionLimit(-2.0, 2.0, 1.875, 100.0, 100.0),
    }

    trajectory = quintic_point_to_point(JOINTS, (0.0, 0.0), (0.1, 1.0), limits)

    assert trajectory.duration_s == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("limit", "expected_duration"),
    [
        (JointMotionLimit(-2.0, 2.0, 100.0, 10.0 / math.sqrt(3.0), 100.0), 1.0),
        (JointMotionLimit(-2.0, 2.0, 100.0, 100.0, 60.0), 1.0),
    ],
)
def test_duration_respects_each_acceleration_and_jerk_bound(limit, expected_duration):
    trajectory = quintic_point_to_point(("j1",), (0.0,), (1.0,), {"j1": limit})

    assert trajectory.duration_s == pytest.approx(expected_duration)


def test_finite_inputs_that_overflow_duration_calculation_fail_closed():
    limit = JointMotionLimit(-1e308, 1e308, 1.0, 1.0, 1.0)

    with pytest.raises(ValueError):
        quintic_point_to_point(("j1",), (-1e308,), (1e308,), {"j1": limit})


def test_duration_calculation_avoids_intermediate_acceleration_overflow():
    limit = JointMotionLimit(0.0, 1e300, 1e308, 1e-308, 1e308)

    trajectory = quintic_point_to_point(("j1",), (0.0,), (1e300,), {"j1": limit})

    expected = (math.sqrt(10.0 / math.sqrt(3.0)) * math.sqrt(1e300)) / math.sqrt(1e-308)
    assert trajectory.duration_s == pytest.approx(expected)


def test_nonzero_subnormal_displacement_that_underflows_duration_fails_closed():
    limit = JointMotionLimit(-2.0, 2.0, 1e308, 1e308, 1e308)

    with pytest.raises(ValueError):
        quintic_point_to_point(("j1",), (0.0,), (1e-320,), {"j1": limit})


@pytest.mark.parametrize(
    "build_trajectory",
    [
        lambda: QuinticTrajectory(("j1",), (0.0,), (1.0,), 0.0),
        lambda: QuinticTrajectory(("j1",), (0.0,), (1.0,), math.inf),
        lambda: QuinticTrajectory(("j1",), (0.0,), (math.inf,), 1.0),
    ],
)
def test_direct_invalid_trajectory_construction_fails_closed(build_trajectory):
    with pytest.raises(ValueError):
        build_trajectory()


def test_direct_subnormal_duration_sampling_fails_closed():
    trajectory = QuinticTrajectory(("j1",), (0.0,), (1.0,), 5e-324)

    with pytest.raises(ValueError):
        trajectory.sample(0.0)


def test_zero_displacement_has_zero_duration_and_zero_derivatives():
    trajectory = generate(start=(0.2, -0.3), target=(0.2, -0.3))

    assert trajectory.duration_s == 0.0
    assert trajectory.sample(0.0).positions == pytest.approx((0.2, -0.3))
    assert trajectory.sample(0.0).velocities == (0.0, 0.0)
    assert trajectory.sample(0.0).accelerations == (0.0, 0.0)
    assert trajectory.sample(0.0).jerks == (0.0, 0.0)


@pytest.mark.parametrize(
    ("joint_names", "start", "target", "limits"),
    [
        (("j1", "j1"), (0.0, 0.0), (0.1, 0.1), LIMITS),
        (JOINTS, (0.0,), (0.1, 0.1), LIMITS),
        (JOINTS, (0.0, 0.0), (0.1, 0.1), {"j1": LIMITS["j1"]}),
        (JOINTS, (0.0, 0.0), (0.1, 0.1), {**LIMITS, "other": LIMITS["j1"]}),
        (JOINTS, (0.0, 0.0), (3.0, 0.1), LIMITS),
        (JOINTS, (math.nan, 0.0), (0.1, 0.1), LIMITS),
        (JOINTS, (10**10000, 0.0), (0.1, 0.1), LIMITS),
    ],
)
def test_invalid_joint_schema_or_positions_fail_closed(joint_names, start, target, limits):
    with pytest.raises(ValueError):
        quintic_point_to_point(joint_names, start, target, limits)


@pytest.mark.parametrize(
    "limit",
    [
        JointMotionLimit(-2.0, 2.0, 0.0, 1.0, 1.0),
        JointMotionLimit(-2.0, 2.0, 1.0, math.inf, 1.0),
        JointMotionLimit(2.0, -2.0, 1.0, 1.0, 1.0),
    ],
)
def test_invalid_motion_limits_fail_closed(limit):
    with pytest.raises(ValueError):
        quintic_point_to_point(JOINTS, (0.0, 0.0), (0.1, 0.1), {"j1": limit, "j2": LIMITS["j2"]})


def test_sampling_rejects_times_outside_the_closed_profile_interval():
    trajectory = generate()

    for time_s in (-0.001, 1.001, math.nan, math.inf):
        with pytest.raises(ValueError):
            trajectory.sample(time_s)


def test_profile_normalizes_its_inputs_and_is_deterministic():
    mutable_limits = dict(reversed(tuple(LIMITS.items())))
    trajectory = quintic_point_to_point(list(JOINTS), [0, 0], [1, -0.5], mutable_limits)
    mutable_limits["j1"] = JointMotionLimit(-2.0, 2.0, 100.0, 100.0, 100.0)

    assert isinstance(trajectory, QuinticTrajectory)
    assert trajectory.joint_names == JOINTS
    assert trajectory.sample(0.5).velocities == pytest.approx((1.875, -0.9375))
