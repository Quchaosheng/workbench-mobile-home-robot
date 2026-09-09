from __future__ import annotations

import math

import pytest
from workbench_motion.quintic_trajectory import JointMotionLimit
from workbench_motion.s_curve_trajectory import SCurveTrajectory, s_curve_point_to_point


def limit(*, velocity: float = 10.0, acceleration: float = 5.0, jerk: float = 1.0) -> JointMotionLimit:
    return JointMotionLimit(-200.0, 200.0, velocity, acceleration, jerk)


def generate(
    *,
    start: tuple[float, ...] = (0.0,),
    target: tuple[float, ...] = (120.0,),
    limits: dict[str, JointMotionLimit] | None = None,
) -> SCurveTrajectory:
    names = tuple(f"joint_{index}" for index in range(len(start)))
    configured_limits = {name: limit() for name in names} if limits is None else limits
    return s_curve_point_to_point(names, start, target, configured_limits)


def test_seven_slot_profile_has_exact_endpoints_and_right_continuous_boundary_jerk():
    trajectory = generate(limits={"joint_0": limit(acceleration=1.0)})

    assert trajectory.duration_s == pytest.approx(23.0)
    assert trajectory.phase_durations_s == ((1.0, 9.0, 1.0, 1.0, 1.0, 9.0, 1.0),)
    assert trajectory.sample(0.0).positions == pytest.approx((0.0,))
    assert trajectory.sample(0.0).velocities == pytest.approx((0.0,))
    assert trajectory.sample(0.0).accelerations == pytest.approx((0.0,))
    assert trajectory.sample(0.0).jerks == pytest.approx((1.0,))
    assert trajectory.sample(1.0).accelerations == pytest.approx((1.0,))
    assert trajectory.sample(1.0).jerks == pytest.approx((0.0,))
    assert trajectory.sample(10.0).jerks == pytest.approx((-1.0,))
    assert trajectory.sample(trajectory.duration_s).positions == pytest.approx((120.0,))
    assert trajectory.sample(trajectory.duration_s).velocities == pytest.approx((0.0,))
    assert trajectory.sample(trajectory.duration_s).accelerations == pytest.approx((0.0,))
    assert trajectory.sample(trajectory.duration_s).jerks == pytest.approx((0.0,))


def test_negative_displacement_reverses_jerk_direction_and_reaches_its_target():
    trajectory = generate(start=(3.0,), target=(-1.0,), limits={"joint_0": limit(acceleration=1.0)})

    assert trajectory.sample(0.0).jerks == pytest.approx((-1.0,))
    assert trajectory.sample(trajectory.duration_s).positions == pytest.approx((-1.0,))


@pytest.mark.parametrize(
    ("motion_limit", "target", "expected_active_slots"),
    [
        (limit(acceleration=1.0), 120.0, 7),
        (limit(velocity=10.0, acceleration=1.0), 4.0, 6),
        (limit(velocity=1.0, acceleration=5.0), 3.0, 5),
        (limit(), 2.0, 4),
    ],
)
def test_short_moves_collapse_only_unavailable_plateaus(motion_limit, target, expected_active_slots):
    trajectory = generate(target=(target,), limits={"joint_0": motion_limit})

    assert sum(duration > 0 for duration in trajectory.phase_durations_s[0]) == expected_active_slots
    assert len(trajectory.phase_durations_s[0]) == 7


def test_phase_boundaries_prove_velocity_acceleration_and_jerk_limits():
    motion_limit = limit(velocity=4.0, acceleration=2.0, jerk=3.0)
    trajectory = generate(target=(24.0,), limits={"joint_0": motion_limit})
    boundary_times = [0.0]
    for duration in trajectory.phase_durations_s[0]:
        boundary_times.append(boundary_times[-1] + duration)

    samples = [trajectory.sample(time_s) for time_s in boundary_times]

    assert max(abs(sample.velocities[0]) for sample in samples) <= motion_limit.max_velocity
    assert max(abs(sample.accelerations[0]) for sample in samples) <= motion_limit.max_acceleration
    assert max(abs(sample.jerks[0]) for sample in samples) <= motion_limit.max_jerk


def test_axes_with_different_minimum_durations_are_temporally_synchronized():
    limits = {"joint_0": limit(), "joint_1": limit()}
    trajectory = generate(start=(0.0, 0.0), target=(2.0, 16.0), limits=limits)

    assert trajectory.duration_s == pytest.approx(8.0)
    assert tuple(sum(phases) for phases in trajectory.phase_durations_s) == pytest.approx((8.0, 8.0))
    assert trajectory.sample(trajectory.duration_s).positions == pytest.approx((2.0, 16.0))
    assert trajectory.sample(trajectory.duration_s).velocities == pytest.approx((0.0, 0.0))
    assert trajectory.sample(0.0).jerks == pytest.approx((0.125, 1.0))


def test_zero_displacement_axis_waits_without_derivatives_until_common_completion():
    trajectory = generate(start=(0.2, 0.0), target=(0.2, 2.0))

    assert trajectory.phase_durations_s[0] == pytest.approx((0.0, 0.0, 0.0, trajectory.duration_s, 0.0, 0.0, 0.0))
    midpoint = trajectory.sample(trajectory.duration_s / 2.0)
    assert midpoint.positions[0] == pytest.approx(0.2)
    assert midpoint.velocities[0] == pytest.approx(0.0)
    assert midpoint.accelerations[0] == pytest.approx(0.0)
    assert midpoint.jerks[0] == pytest.approx(0.0)


def test_all_zero_displacement_has_zero_duration_and_zero_derivatives():
    trajectory = generate(start=(0.2,), target=(0.2,))

    assert trajectory.duration_s == 0.0
    assert trajectory.phase_durations_s == ((0.0,) * 7,)
    assert trajectory.sample(0.0).positions == pytest.approx((0.2,))
    assert trajectory.sample(0.0).velocities == pytest.approx((0.0,))
    assert trajectory.sample(0.0).accelerations == pytest.approx((0.0,))
    assert trajectory.sample(0.0).jerks == pytest.approx((0.0,))


def test_direct_constructor_rejects_phase_profile_that_cannot_reach_its_target_at_rest():
    with pytest.raises(ValueError, match="reach the target at rest"):
        SCurveTrajectory(
            ("joint_0",),
            (0.0,),
            (1.0,),
            1.0,
            ((0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0),),
            ((0.0,) * 7,),
        )


def test_generated_phase_boundaries_are_sampleable_despite_rounding():
    trajectory = s_curve_point_to_point(
        ("joint_0",),
        (0.0,),
        (1.0,),
        {"joint_0": JointMotionLimit(-1_000_000.0, 1_000_000.0, 0.01, 0.01, 1.0)},
    )

    boundary_s = 0.0
    for duration_s in trajectory.phase_durations_s[0]:
        boundary_s += duration_s
        trajectory.sample(boundary_s)


@pytest.mark.parametrize(
    ("joint_names", "start", "target", "limits"),
    [
        (("joint_0", "joint_0"), (0.0, 0.0), (1.0, 1.0), {"joint_0": limit()}),
        (("joint_0",), (0.0, 0.0), (1.0,), {"joint_0": limit()}),
        (("joint_0",), (0.0,), (201.0,), {"joint_0": limit()}),
        (("joint_0",), (0.0,), (math.nan,), {"joint_0": limit()}),
        (("joint_0",), (0.0,), (1.0,), {"other": limit()}),
        (("joint_0",), (0.0,), (1.0,), {"joint_0": limit(jerk=0.0)}),
    ],
)
def test_malformed_or_out_of_bounds_inputs_fail_closed(joint_names, start, target, limits):
    with pytest.raises(ValueError):
        s_curve_point_to_point(joint_names, start, target, limits)


@pytest.mark.parametrize("time_s", (-0.1, math.nan, math.inf, 24.0))
def test_sampling_rejects_times_outside_the_closed_interval(time_s):
    with pytest.raises(ValueError):
        generate().sample(time_s)
