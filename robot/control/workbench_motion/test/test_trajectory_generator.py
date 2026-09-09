from __future__ import annotations

import pytest
from workbench_motion.quintic_trajectory import JointMotionLimit, QuinticTrajectory, quintic_point_to_point
from workbench_motion.s_curve_trajectory import SCurveTrajectory, s_curve_point_to_point
from workbench_motion.trajectory_generator import TrajectoryAlgorithm, generate_point_to_point

JOINTS = ("joint_1", "joint_2")
LIMITS = {
    "joint_1": JointMotionLimit(-2.0, 2.0, 2.0, 4.0, 8.0),
    "joint_2": JointMotionLimit(-2.0, 2.0, 3.0, 6.0, 12.0),
}
START = (0.0, 0.2)
TARGET = (1.0, -0.4)


def test_omitted_strategy_matches_the_direct_quintic_primitive():
    trajectory = generate_point_to_point(JOINTS, START, TARGET, LIMITS)
    expected = quintic_point_to_point(JOINTS, START, TARGET, LIMITS)

    assert isinstance(trajectory, QuinticTrajectory)
    assert trajectory == expected


def test_exact_quintic_strategy_matches_the_direct_quintic_primitive():
    trajectory = generate_point_to_point(JOINTS, START, TARGET, LIMITS, strategy="quintic")
    expected = quintic_point_to_point(JOINTS, START, TARGET, LIMITS)

    assert trajectory == expected


def test_exact_s_curve_strategy_matches_the_direct_s_curve_primitive():
    trajectory = generate_point_to_point(JOINTS, START, TARGET, LIMITS, strategy="s_curve")
    expected = s_curve_point_to_point(JOINTS, START, TARGET, LIMITS)

    assert isinstance(trajectory, SCurveTrajectory)
    assert trajectory == expected


@pytest.mark.parametrize("strategy", list(TrajectoryAlgorithm))
def test_selected_trajectory_has_the_shared_analytic_sampling_surface(strategy):
    trajectory = generate_point_to_point(JOINTS, START, TARGET, LIMITS, strategy=strategy)
    sample = trajectory.sample(trajectory.duration_s / 2.0)

    assert trajectory.joint_names == JOINTS
    assert trajectory.start_positions == START
    assert trajectory.target_positions == TARGET
    assert len(sample.positions) == len(JOINTS)
    assert len(sample.velocities) == len(JOINTS)
    assert len(sample.accelerations) == len(JOINTS)
    assert len(sample.jerks) == len(JOINTS)


@pytest.mark.parametrize("strategy", list(TrajectoryAlgorithm))
def test_identical_inputs_select_a_deterministic_trajectory(strategy):
    first = generate_point_to_point(JOINTS, START, TARGET, LIMITS, strategy=strategy)
    second = generate_point_to_point(JOINTS, START, TARGET, LIMITS, strategy=strategy)

    assert first == second
    assert first.sample(first.duration_s / 2.0) == second.sample(second.duration_s / 2.0)


@pytest.mark.parametrize("strategy", ("", "QUINTIC", "s-curve", "linear", None, 1, object()))
def test_unknown_or_noncanonical_strategy_fails_closed_without_a_fallback(strategy):
    with pytest.raises(ValueError):
        generate_point_to_point(JOINTS, START, TARGET, LIMITS, strategy=strategy)


@pytest.mark.parametrize("strategy", list(TrajectoryAlgorithm))
def test_selected_primitive_validation_propagates_without_implicit_limits(strategy):
    invalid_limits = {**LIMITS, "joint_1": JointMotionLimit(-2.0, 2.0, 0.0, 4.0, 8.0)}

    with pytest.raises(ValueError):
        generate_point_to_point(JOINTS, START, TARGET, invalid_limits, strategy=strategy)


def test_strategy_enum_has_only_the_documented_canonical_values():
    assert tuple(algorithm.value for algorithm in TrajectoryAlgorithm) == ("quintic", "s_curve")
