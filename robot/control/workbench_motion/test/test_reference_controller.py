from __future__ import annotations

import pytest
from workbench_motion.joint_limits import (
    AcceptedTrajectory,
    JointLimit,
    PreflightPolicy,
    build_preflight_context,
    preflight_trajectory,
)
from workbench_motion.motion_types import (
    CommandMode,
    ControllerLifecycle,
    ReceiptReason,
    ReceiptStatus,
    RobotCommand,
    RobotState,
)
from workbench_motion.reference_controller import InMemoryController

ROBOT_ID = "ur5e-left"
JOINTS = ("shoulder", "elbow")
LIMITS = {joint: JointLimit(-2.0, 2.0, 10.0, 10.0) for joint in JOINTS}
PREFLIGHT_CONTEXT = build_preflight_context(
    policy=PreflightPolicy("test-1", 1e-6, 30.0, 0.05),
    expected_joint_names=JOINTS,
    hard_limits=LIMITS,
    override_limits={},
)


def state(*, observed_at_s: float = 10.0, positions: tuple[float, float] = (0.0, 0.0)) -> RobotState:
    return RobotState(ROBOT_ID, JOINTS, positions, (0.0, 0.0), observed_at_s)


def command(
    *,
    command_id: str = "cmd-1",
    robot_id: str = ROBOT_ID,
    joint_names: tuple[str, ...] = JOINTS,
) -> RobotCommand:
    return RobotCommand(command_id, robot_id, joint_names, CommandMode.POSITION, (0.2, -0.1), 10.0)


def controller(**kwargs) -> InMemoryController:
    return InMemoryController(ROBOT_ID, JOINTS, max_state_age_s=0.5, **kwargs)


def accepted_trajectory(*, start: tuple[float, float]) -> AcceptedTrajectory:
    result = preflight_trajectory(
        {
            "joint_names": list(JOINTS),
            "points": [
                {
                    "positions": list(start),
                    "velocities": [],
                    "accelerations": [],
                    "effort": [],
                    "time_from_start": 0.0,
                }
            ],
        },
        dict(zip(JOINTS, start, strict=True)),
        context=PREFLIGHT_CONTEXT,
    )
    assert isinstance(result, AcceptedTrajectory), result
    return result


def test_command_requires_fresh_matching_feedback_and_never_claims_dispatch():
    port = controller()
    assert port.submit_command(command(), now_s=10.0).reason is ReceiptReason.NO_STATE

    port.update_state(state())
    accepted = port.submit_command(command(), now_s=10.2)

    assert accepted.status is ReceiptStatus.ACCEPTED
    assert accepted.dispatch_attempted is False
    assert port.get_state(now_s=10.2) == state()

    stale = port.submit_command(command(command_id="cmd-2"), now_s=10.6)
    assert stale.status is ReceiptStatus.REJECTED
    assert stale.reason is ReceiptReason.STALE_STATE
    assert port.get_state(now_s=10.6) is None


def test_future_dated_feedback_is_rejected_fail_closed():
    port = controller()
    port.update_state(state(observed_at_s=10.2))

    receipt = port.submit_command(command(), now_s=10.1)

    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason is ReceiptReason.FUTURE_STATE
    assert receipt.dispatch_attempted is False
    assert port.get_state(now_s=10.1) is None


@pytest.mark.parametrize(
    "candidate,reason",
    [
        (command(robot_id="ur5e-right"), ReceiptReason.ROBOT_ID_MISMATCH),
        (command(joint_names=("elbow", "shoulder")), ReceiptReason.JOINT_SCHEMA_MISMATCH),
    ],
)
def test_command_identity_and_joint_schema_mismatches_fail_closed(candidate, reason):
    port = controller()
    port.update_state(state())

    receipt = port.submit_command(candidate, now_s=10.1)

    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason is reason
    assert receipt.dispatch_attempted is False


def test_duplicate_request_is_rejected_without_a_second_dispatch_attempt():
    port = controller()
    port.update_state(state())
    assert port.submit_command(command(), now_s=10.1).status is ReceiptStatus.ACCEPTED

    duplicate = port.submit_command(command(), now_s=10.2)

    assert duplicate.status is ReceiptStatus.REJECTED
    assert duplicate.reason is ReceiptReason.DUPLICATE_REQUEST
    assert duplicate.dispatch_attempted is False


def test_preflighted_trajectory_requires_a_continuous_fresh_start_state():
    port = controller()
    port.update_state(state())

    accepted = port.submit_trajectory("traj-1", accepted_trajectory(start=(0.0, 0.0)), now_s=10.1)
    discontinuous = port.submit_trajectory("traj-2", accepted_trajectory(start=(0.1, 0.0)), now_s=10.2)

    assert accepted.status is ReceiptStatus.ACCEPTED
    assert accepted.dispatch_attempted is False
    assert discontinuous.status is ReceiptStatus.REJECTED
    assert discontinuous.reason is ReceiptReason.START_STATE_DISCONTINUITY
    assert discontinuous.dispatch_attempted is False


def test_non_preflight_trajectory_is_rejected_before_admission():
    port = controller()
    port.update_state(state())

    receipt = port.submit_trajectory("traj-1", object(), now_s=10.1)  # type: ignore[arg-type]

    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason is ReceiptReason.INVALID_TRAJECTORY
    assert receipt.dispatch_attempted is False


def test_hold_stop_and_reset_follow_explicit_lifecycle_rules():
    port = controller()
    port.update_state(state())

    hold = port.hold("hold-1", now_s=10.1)
    assert hold.status is ReceiptStatus.ACCEPTED
    assert port.lifecycle is ControllerLifecycle.HOLDING
    blocked = port.submit_command(command(), now_s=10.2)
    assert blocked.reason is ReceiptReason.INVALID_LIFECYCLE

    assert port.reset("reset-1", now_s=10.3).status is ReceiptStatus.ACCEPTED
    assert port.lifecycle is ControllerLifecycle.READY
    assert port.stop("stop-1", now_s=10.4).status is ReceiptStatus.ACCEPTED
    assert port.lifecycle is ControllerLifecycle.STOPPED
    assert port.reset("reset-2", now_s=10.45).status is ReceiptStatus.ACCEPTED
    assert port.lifecycle is ControllerLifecycle.READY


def test_stop_failure_enters_faulted_state_and_never_reports_success():
    port = controller(stop_failure_reason="test-injected stop failure")

    receipt = port.stop("stop-1", now_s=10.0)

    assert receipt.status is ReceiptStatus.REJECTED
    assert receipt.reason is ReceiptReason.SAFE_STOP_FAILURE
    assert receipt.dispatch_attempted is False
    assert port.lifecycle is ControllerLifecycle.FAULTED
