from __future__ import annotations

import math

import pytest
from workbench_motion.joint_controller import JointController, TrackingSample, summarize_tracking
from workbench_motion.motion_types import CommandMode, RobotState
from workbench_motion.quintic_trajectory import JointMotionLimit

ROBOT_ID = "ur5e-left"
JOINTS = ("shoulder_pan", "shoulder_lift", "elbow", "wrist_1", "wrist_2", "wrist_3")
LIMITS = {name: JointMotionLimit(-2.0, 2.0, 1.875, 10.0 / math.sqrt(3.0), 60.0) for name in JOINTS}


def state(
    *,
    positions: tuple[float, ...] | None = None,
    velocities: tuple[float, ...] | None = None,
    observed_at_s: float = 10.0,
) -> RobotState:
    values = (0.0,) * len(JOINTS) if positions is None else positions
    measured_velocities = (0.0,) * len(JOINTS) if velocities is None else velocities
    return RobotState(ROBOT_ID, JOINTS, values, measured_velocities, observed_at_s)


def controller() -> JointController:
    return JointController(ROBOT_ID, JOINTS, LIMITS)


def test_position_plan_reuses_quintic_limits_in_configured_joint_order():
    trajectory = controller().plan_position(state(), (1.0,) + (0.0,) * 5)

    assert trajectory.joint_names == JOINTS
    assert trajectory.duration_s == pytest.approx(1.0)
    assert trajectory.sample(0.5).velocities[0] == pytest.approx(1.875)


def test_velocity_command_is_internal_and_bounded_by_explicit_limits():
    command = controller().velocity_command("vel-1", state(), (1.875,) + (0.0,) * 5, issued_at_s=10.1)

    assert command.mode is CommandMode.VELOCITY
    assert command.values[0] == pytest.approx(1.875)


@pytest.mark.parametrize(
    "target",
    [
        (2.1,) + (0.0,) * 5,
        (math.nan,) + (0.0,) * 5,
        (0.0,) * 5,
    ],
)
def test_invalid_position_targets_fail_closed(target):
    with pytest.raises(ValueError):
        controller().plan_position(state(), target)


def test_rest_to_rest_position_plan_rejects_moving_feedback():
    with pytest.raises(ValueError, match="zero feedback velocities"):
        controller().plan_position(state(velocities=(0.1,) + (0.0,) * 5), (1.0,) + (0.0,) * 5)


def test_over_limit_velocity_fails_closed():
    with pytest.raises(ValueError, match="velocity limit"):
        controller().velocity_command("vel-1", state(), (1.876,) + (0.0,) * 5, issued_at_s=10.1)


@pytest.mark.parametrize(
    "configured_limits",
    [
        {name: limit for name, limit in LIMITS.items() if name != JOINTS[-1]},
        {**LIMITS, "unexpected_joint": LIMITS[JOINTS[0]]},
        {**LIMITS, JOINTS[0]: JointMotionLimit(-2.0, 2.0, math.nan, 1.0, 1.0)},
    ],
)
def test_controller_requires_exact_finite_per_joint_limits(configured_limits):
    with pytest.raises(ValueError):
        JointController(ROBOT_ID, JOINTS, configured_limits)


@pytest.mark.parametrize(
    "feedback",
    [
        RobotState("ur5e-right", JOINTS, (0.0,) * 6, (0.0,) * 6, 10.0),
        RobotState(ROBOT_ID, tuple(reversed(JOINTS)), (0.0,) * 6, (0.0,) * 6, 10.0),
        RobotState(ROBOT_ID, JOINTS, (2.1,) + (0.0,) * 5, (0.0,) * 6, 10.0),
    ],
)
def test_planning_rejects_mismatched_or_out_of_bounds_feedback(feedback):
    with pytest.raises(ValueError):
        controller().plan_position(feedback, (0.0,) * 6)


def test_tracking_summary_has_deterministic_errors_smoothness_and_settling_time():
    desired = (1.0,) + (0.0,) * 5
    samples = (
        TrackingSample(
            0.0,
            state(positions=(0.0,) * 6),
            desired,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
        ),
        TrackingSample(
            1.0,
            state(positions=desired),
            desired,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (2.0,) + (0.0,) * 5,
            (3.0,) + (0.0,) * 5,
        ),
        TrackingSample(
            2.0,
            state(positions=desired),
            desired,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (1.0,) + (0.0,) * 5,
            (1.0,) + (0.0,) * 5,
        ),
    )

    report = summarize_tracking(samples, position_tolerance=0.01, velocity_tolerance=0.01)

    assert report.final_position_errors["shoulder_pan"] == pytest.approx(0.0)
    assert report.rms_position_error == pytest.approx(math.sqrt(1.0 / 18.0))
    assert report.max_position_error == pytest.approx(1.0)
    assert report.peak_acceleration == pytest.approx(2.0)
    assert report.peak_jerk == pytest.approx(3.0)
    assert report.integrated_squared_jerk == pytest.approx(10.0)
    assert report.settling_time_s == pytest.approx(1.0)


def test_tracking_rejects_non_monotonic_samples_and_reports_unsettled_motion():
    desired = (0.0,) * 6
    sample = TrackingSample(
        1.0,
        state(positions=(1.0,) + (0.0,) * 5),
        desired,
        (0.0,) * 6,
        (0.0,) * 6,
        (0.0,) * 6,
        (0.0,) * 6,
        (0.0,) * 6,
    )

    with pytest.raises(ValueError, match="strictly increasing"):
        summarize_tracking((sample, sample), position_tolerance=0.01, velocity_tolerance=0.01)

    report = summarize_tracking((sample,), position_tolerance=0.01, velocity_tolerance=0.01)
    assert report.settling_time_s is None


def test_tracking_settles_after_the_last_out_of_tolerance_sample():
    desired = (0.0,) * 6
    within = TrackingSample(
        0.0,
        state(),
        desired,
        desired,
        desired,
        desired,
        desired,
        desired,
    )
    outside = TrackingSample(
        1.0,
        state(positions=(0.02,) + (0.0,) * 5),
        desired,
        desired,
        desired,
        desired,
        desired,
        desired,
    )
    settled = TrackingSample(
        2.0,
        state(),
        desired,
        desired,
        desired,
        desired,
        desired,
        desired,
    )

    report = summarize_tracking((within, outside, settled), position_tolerance=0.01, velocity_tolerance=0.01)

    assert report.settling_time_s == pytest.approx(2.0)


def test_tracking_sample_rejects_non_finite_or_wrong_length_measurements():
    with pytest.raises(ValueError):
        TrackingSample(
            0.0,
            state(),
            (0.0,) * 5,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
        )
    with pytest.raises(ValueError):
        TrackingSample(
            0.0,
            state(),
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (0.0,) * 6,
            (math.nan,) + (0.0,) * 5,
            (0.0,) * 6,
        )


def test_zero_duration_hold_plan_has_zero_derivatives():
    trajectory = controller().plan_position(state(), (0.0,) * 6)

    assert trajectory.duration_s == 0.0
    assert trajectory.sample(0.0).jerks == (0.0,) * 6
