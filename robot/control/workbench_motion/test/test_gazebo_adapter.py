from __future__ import annotations

from types import SimpleNamespace

import pytest
from workbench_motion.controller import ControllerMode, RobotCommand, RobotState
from workbench_motion.gazebo_adapter import (
    GazeboTrajectoryController,
    JointStateConversionError,
    JointStateReason,
    accepted_to_follow_joint_goal,
    joint_state_to_robot_state,
)
from workbench_motion.joint_limits import (
    AcceptedTrajectory,
    JointLimit,
    PreflightPolicy,
    build_preflight_context,
    preflight_trajectory,
)

JOINTS = ("j1", "j2")
LIMITS = {name: JointLimit(-2.0, 2.0, 2.0, 10.0) for name in JOINTS}
POLICY = PreflightPolicy("test-1", 1e-6, 30.0, 0.05)


class FakeDuration:
    sec = 0
    nanosec = 0


class FakePoint:
    def __init__(self) -> None:
        self.positions = []
        self.velocities = []
        self.accelerations = []
        self.effort = []
        self.time_from_start = FakeDuration()


class MalformedPoint:
    def __init__(self) -> None:
        self.positions = []
        self.velocities = []
        self.accelerations = []
        self.effort = []


class FakeGoal:
    def __init__(self) -> None:
        self.trajectory = SimpleNamespace(joint_names=[], points=[])


class FakeNode:
    def __init__(self) -> None:
        self.subscription_callback = None
        self._scheduled = []

    def create_subscription(self, *_args):
        self.subscription_callback = _args[2]
        return object()

    def spin_once(self, *, timeout_sec):
        if self._scheduled:
            self._scheduled.pop(0)()
        return None

    def schedule(self, callback):
        self._scheduled.append(callback)


class FakeActionClient:
    def __init__(self) -> None:
        self.sent = []

    def send_goal_async(self, goal, **_kwargs):
        self.sent.append(goal)
        return SimpleNamespace(done=lambda: True)


class FakeFuture:
    def __init__(self, value=None, *, done=True, error=None):
        self._value = value
        self._done = done
        self._error = error
        self.cancelled = False

    def done(self):
        return self._done

    def result(self):
        if self._error is not None:
            raise self._error
        return self._value

    def cancel(self):
        self.cancelled = True
        return True

    def set_done(self):
        self._done = True


class FakeGoalHandle:
    def __init__(self, *, accepted=True, result=None, cancel_result=None, cancel_error=None, feedback=None):
        self.accepted = accepted
        self._result = result
        self._cancel_result = cancel_result
        self._cancel_error = cancel_error
        self._feedback = feedback
        self._node = None
        self.cancel_calls = 0

    def get_result_async(self):
        if self._node is None:
            return FakeFuture(self._result)
        future = FakeFuture(self._result, done=False)

        def complete() -> None:
            if self._feedback is not None and self._node.subscription_callback is not None:
                self._node.subscription_callback(self._feedback)
            future.set_done()

        self._node.schedule(complete)
        return future

    def cancel_goal_async(self):
        self.cancel_calls += 1
        return FakeFuture(self._cancel_result, error=self._cancel_error)


class PendingResultGoalHandle(FakeGoalHandle):
    def get_result_async(self):
        return FakeFuture(done=False)


class ResultEnvelope:
    def __init__(self, *, status=4, error_code=0):
        self.status = status
        self.result = SimpleNamespace(error_code=error_code)


class GoalActionClient:
    def __init__(self, handle):
        self.handle = handle
        self.sent = []

    def send_goal_async(self, goal, **_kwargs):
        self.sent.append(goal)
        return FakeFuture(self.handle)


def context():
    return build_preflight_context(
        policy=POLICY,
        expected_joint_names=JOINTS,
        hard_limits=LIMITS,
        override_limits={},
    )


def accepted() -> AcceptedTrajectory:
    result = preflight_trajectory(
        {
            "joint_names": list(JOINTS),
            "points": [
                {"positions": [0.0, 0.0], "velocities": [0.3, -0.4], "time_from_start": 0.0},
                {"positions": [0.4, -0.5], "time_from_start": 1.25},
            ],
        },
        {name: 0.0 for name in JOINTS},
        context=context(),
    )
    assert isinstance(result, AcceptedTrajectory), result
    return result


def joint_state(*, names=JOINTS, positions=(0.1, -0.2), velocities=(0.0, 0.0), sec=3, nanosec=4):
    return SimpleNamespace(
        name=list(names),
        position=list(positions),
        velocity=list(velocities),
        header=SimpleNamespace(stamp=SimpleNamespace(sec=sec, nanosec=nanosec)),
    )


def test_accepted_snapshot_maps_to_follow_joint_goal_without_reordering() -> None:
    goal = accepted_to_follow_joint_goal(accepted(), goal_type=FakeGoal, point_type=FakePoint)
    assert goal.trajectory.joint_names == list(JOINTS)
    assert goal.trajectory.points[0].positions == [0.0, 0.0]
    assert goal.trajectory.points[0].velocities == [0.3, -0.4]
    assert goal.trajectory.points[0].time_from_start.sec == 0
    assert goal.trajectory.points[0].time_from_start.nanosec == 0
    assert goal.trajectory.points[1].time_from_start.sec == 1
    assert goal.trajectory.points[1].time_from_start.nanosec == 250_000_000


def test_goal_conversion_requires_mutable_duration_fields() -> None:
    with pytest.raises(ValueError, match="mutable time_from_start"):
        accepted_to_follow_joint_goal(accepted(), goal_type=FakeGoal, point_type=MalformedPoint)


def test_joint_state_conversion_orders_extra_joints_and_preserves_feedback() -> None:
    state = joint_state(names=("extra", "j2", "j1"), positions=(9.0, -0.2, 0.1), velocities=())
    converted = joint_state_to_robot_state(state, joint_names=JOINTS, sequence=7)
    assert converted.positions == (0.1, -0.2)
    assert converted.velocities == ()
    assert converted.timestamp_ns == 3_000_000_004
    assert converted.sequence == 7


@pytest.mark.parametrize(
    "message,reason",
    [
        (joint_state(names=("j1",), positions=(0.0,)), JointStateReason.MISSING_JOINT),
        (joint_state(positions=(float("nan"), 0.0)), JointStateReason.NON_FINITE),
        (joint_state(sec=-1), JointStateReason.INVALID_TIMESTAMP),
        (joint_state(velocities=(0.0,)), JointStateReason.INVALID_MESSAGE),
    ],
)
def test_invalid_joint_state_fails_closed(message, reason) -> None:
    with pytest.raises(JointStateConversionError) as raised:
        joint_state_to_robot_state(message, joint_names=JOINTS, sequence=1)
    assert raised.value.reason is reason


@pytest.mark.parametrize("positions", [(True, 0.0), ("0.0", 0.0)])
def test_non_numeric_joint_state_values_are_rejected(positions) -> None:
    with pytest.raises(JointStateConversionError) as raised:
        joint_state_to_robot_state(
            joint_state(positions=positions),
            joint_names=JOINTS,
            sequence=1,
        )
    assert raised.value.reason is JointStateReason.NON_FINITE


def test_duplicate_joint_names_are_rejected() -> None:
    with pytest.raises(JointStateConversionError) as raised:
        joint_state_to_robot_state(
            joint_state(names=("j1", "j1"), positions=(0.0, 0.1)),
            joint_names=JOINTS,
            sequence=1,
        )
    assert raised.value.reason is JointStateReason.INVALID_MESSAGE


def test_zero_timestamp_is_rejected() -> None:
    with pytest.raises(JointStateConversionError) as raised:
        joint_state_to_robot_state(joint_state(sec=0, nanosec=0), joint_names=JOINTS, sequence=1)
    assert raised.value.reason is JointStateReason.INVALID_TIMESTAMP


def test_controller_ignores_out_of_order_joint_state_timestamp() -> None:
    controller = make_controller(FakeGoalHandle(result=ResultEnvelope()))
    controller._on_joint_state(joint_state(sec=3, nanosec=4))
    before = controller.state
    controller._on_joint_state(joint_state(positions=(0.2, -0.3), sec=2))
    assert controller.state == before
    assert controller._last_joint_state_error is not None
    assert controller._last_joint_state_error.reason is JointStateReason.STALE_TIMESTAMP


def test_controller_can_ingest_feedback_from_external_subscription() -> None:
    node = FakeNode()
    controller = GazeboTrajectoryController(
        RobotState(JOINTS, (0.0, 0.0), timestamp_ns=1),
        node=node,
        action_name="/arm/follow_joint_trajectory",
        action_client=FakeActionClient(),
        subscribe_joint_state=False,
    )
    assert node.subscription_callback is None
    assert controller.ingest_joint_state(joint_state(positions=(0.2, -0.3), sec=3))
    assert controller.state.positions == (0.2, -0.3)
    assert controller.state.sequence == 1


def test_external_subscription_rejects_malformed_feedback() -> None:
    controller = GazeboTrajectoryController(
        RobotState(JOINTS, (0.0, 0.0), timestamp_ns=1),
        node=FakeNode(),
        action_name="/arm/follow_joint_trajectory",
        action_client=FakeActionClient(),
        subscribe_joint_state=False,
    )
    assert not controller.ingest_joint_state(joint_state(velocities=(0.0,)))
    assert controller.state.sequence == 0


def test_gazebo_controller_dispatches_only_after_base_controller_schema_checks() -> None:
    action_client = FakeActionClient()
    controller = GazeboTrajectoryController(
        RobotState(JOINTS, (0.0, 0.0)),
        node=FakeNode(),
        action_name="/arm_trajectory_controller/follow_joint_trajectory",
        action_client=action_client,
        joint_state_type=object,
    )
    command = RobotCommand.trajectory("trajectory-1", accepted())
    receipt = controller.dispatch(command)
    assert receipt.status.value == "accepted"
    assert controller.state.mode is ControllerMode.EXECUTING
    assert len(action_client.sent) == 1


def test_invalid_action_future_rejects_without_entering_executing() -> None:
    action_client = SimpleNamespace(send_goal_async=lambda *_args, **_kwargs: None)
    controller = GazeboTrajectoryController(
        RobotState(JOINTS, (0.0, 0.0)),
        node=FakeNode(),
        action_name="/arm_trajectory_controller/follow_joint_trajectory",
        action_client=action_client,
        joint_state_type=object,
    )
    receipt = controller.dispatch(RobotCommand.trajectory("trajectory-1", accepted()))
    assert receipt.status.value == "rejected"
    assert receipt.reason == "send_failed:invalid_future"
    assert controller.state.mode is ControllerMode.IDLE


def test_explicit_empty_joint_schema_is_rejected() -> None:
    with pytest.raises(ValueError, match="joint_names must match"):
        GazeboTrajectoryController(
            RobotState(JOINTS, (0.0, 0.0)),
            node=FakeNode(),
            action_name="/arm_trajectory_controller/follow_joint_trajectory",
            joint_names=(),
            action_client=FakeActionClient(),
            joint_state_type=object,
        )


def make_controller(handle, *, initial_state=None):
    node = FakeNode()
    handle._node = node
    return GazeboTrajectoryController(
        initial_state or RobotState(JOINTS, (0.0, 0.0), timestamp_ns=1),
        node=node,
        action_name="/arm_trajectory_controller/follow_joint_trajectory",
        action_client=GoalActionClient(handle),
        joint_state_type=object,
    )


def dispatch_trajectory(controller):
    receipt = controller.dispatch(RobotCommand.trajectory("trajectory-1", accepted()))
    assert receipt.status.value == "accepted"


@pytest.mark.parametrize(
    ("status", "error_code", "expected"),
    [
        (4, 0, "succeeded"),
        (5, -1, "canceled"),
        (6, -2, "aborted"),
    ],
)
def test_wait_for_completion_classifies_ros_goal_status(status, error_code, expected) -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(status=status, error_code=error_code))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    actual, actual_code, _ = controller.wait_for_completion(0.1, target=(0.4, -0.5))
    assert actual.value == expected
    assert actual_code == error_code
    assert controller.state.mode is ControllerMode.IDLE


def test_missing_action_status_cannot_claim_success() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(status=None, error_code=0))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    status, _, detail = controller.wait_for_completion(0.1, target=(0.4, -0.5))
    assert status.value == "unavailable"
    assert detail == "missing_goal_status"
    assert controller.state.mode is ControllerMode.FAULTED


@pytest.mark.parametrize("status", [0, 1, 2, 3, 99])
def test_non_terminal_action_status_cannot_claim_abort_or_success(status) -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(status=status, error_code=0))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    actual, actual_code, detail = controller.wait_for_completion(0.1, target=(0.4, -0.5))
    assert actual.value == "unavailable"
    assert actual_code == 0
    assert detail == "unexpected_goal_status"
    assert controller.state.mode is ControllerMode.FAULTED


def test_succeeded_action_without_error_code_is_unavailable() -> None:
    handle = FakeGoalHandle(result=SimpleNamespace(status=4, result=SimpleNamespace()))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    actual, actual_code, detail = controller.wait_for_completion(0.1, target=(0.4, -0.5))
    assert actual.value == "unavailable"
    assert actual_code is None
    assert detail == "missing_error_code"
    assert controller.state.mode is ControllerMode.FAULTED


def test_rejected_goal_does_not_remain_executing() -> None:
    controller = make_controller(FakeGoalHandle(accepted=False))
    dispatch_trajectory(controller)
    status, _, detail = controller.wait_for_completion(0.1, target=(0.4, -0.5))
    assert status.value == "rejected"
    assert detail == "goal_rejected"
    assert controller.state.mode is ControllerMode.IDLE


def test_completion_target_must_match_controlled_joint_schema() -> None:
    controller = make_controller(FakeGoalHandle(result=ResultEnvelope()))
    dispatch_trajectory(controller)
    with pytest.raises(ValueError, match="one finite position per controlled joint"):
        controller.wait_for_completion(0.1, target=(0.4,))


def test_completion_target_must_match_active_trajectory_endpoint() -> None:
    controller = make_controller(FakeGoalHandle(result=ResultEnvelope()))
    dispatch_trajectory(controller)
    with pytest.raises(ValueError, match="active trajectory endpoint"):
        controller.wait_for_completion(0.1, target=(0.3, -0.5))


def test_goal_acceptance_timeout_preserves_pending_goal_and_does_not_claim_stop() -> None:
    pending = FakeFuture(done=False)
    client = SimpleNamespace(send_goal_async=lambda *_args, **_kwargs: pending)
    controller = GazeboTrajectoryController(
        RobotState(JOINTS, (0.0, 0.0), timestamp_ns=1),
        node=FakeNode(),
        action_name="/arm_trajectory_controller/follow_joint_trajectory",
        action_client=client,
        joint_state_type=object,
    )
    dispatch_trajectory(controller)
    status, _, detail = controller.wait_for_completion(0.001, target=(0.4, -0.5))
    assert status.value == "timeout"
    assert detail == "goal_acceptance_timeout"
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_future is pending
    assert controller.safe_stop("operator") is False
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_future is pending


def test_result_timeout_with_failed_cancel_preserves_active_goal() -> None:
    handle = PendingResultGoalHandle(
        result=ResultEnvelope(),
        cancel_result=SimpleNamespace(goals_canceling=[]),
    )
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    status, _, detail = controller.wait_for_completion(0.001, target=(0.4, -0.5))
    assert status.value == "timeout"
    assert detail == "result_timeout_cancel_failed"
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_handle is handle
    assert controller.safe_stop("operator") is False
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_handle is handle


def test_result_timeout_with_confirmed_cancel_releases_goal() -> None:
    handle = PendingResultGoalHandle(
        result=ResultEnvelope(),
        cancel_result=SimpleNamespace(goals_canceling=["goal"]),
    )
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    status, _, detail = controller.wait_for_completion(0.001, target=(0.4, -0.5))
    assert status.value == "timeout"
    assert detail == "result_timeout_canceled"
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_handle is None
    assert controller.safe_stop("operator") is True
    assert controller.state.mode is ControllerMode.STOPPED


def test_cancel_response_failure_keeps_active_goal() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(), cancel_result=SimpleNamespace(goals_canceling=[]))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    assert controller.cancel("trajectory-1") is False
    assert handle.cancel_calls == 1
    assert controller.state.mode is ControllerMode.EXECUTING


def test_safe_stop_cancel_failure_keeps_active_goal_and_faults() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(), cancel_result=SimpleNamespace(goals_canceling=[]))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    controller._goal_handle = handle
    assert controller.safe_stop("operator") is False
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_handle is handle


def test_cancel_response_success_returns_to_idle_and_releases_goal() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(), cancel_result=SimpleNamespace(goals_canceling=["goal"]))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    assert controller.cancel("trajectory-1") is True
    assert handle.cancel_calls == 1
    assert controller.state.mode is ControllerMode.IDLE
    assert controller._goal_handle is None


def test_result_timeout_attempts_cancel_and_faults() -> None:
    handle = PendingResultGoalHandle()
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    status, _, detail = controller.wait_for_completion(0.001, target=(0.4, -0.5))
    assert status.value == "timeout"
    assert detail == "result_timeout_canceled"
    assert handle.cancel_calls == 1
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_handle is None
    assert controller._result_future is None


def test_safe_stop_with_pending_goal_fails_closed_in_faulted_mode() -> None:
    pending = FakeFuture(done=False)
    client = SimpleNamespace(send_goal_async=lambda *_args, **_kwargs: pending)
    controller = GazeboTrajectoryController(
        RobotState(JOINTS, (0.0, 0.0), timestamp_ns=1),
        node=FakeNode(),
        action_name="/arm_trajectory_controller/follow_joint_trajectory",
        action_client=client,
        joint_state_type=object,
    )
    dispatch_trajectory(controller)
    assert controller.safe_stop("operator request") is False
    assert controller.state.mode is ControllerMode.FAULTED
    assert controller._goal_future is pending


def test_safe_stop_requires_cancel_ack_and_reset_clears_stopped_goal() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope(), cancel_result=SimpleNamespace(goals_canceling=["goal"]))
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    assert controller.safe_stop("operator request") is True
    assert controller.state.mode is ControllerMode.STOPPED
    assert controller.reset() is True
    assert controller.state.mode is ControllerMode.IDLE


def test_hold_is_explicitly_unsupported_for_jtc() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope())
    controller = make_controller(handle)
    dispatch_trajectory(controller)
    assert controller.hold() is False
    assert controller.state.mode is ControllerMode.EXECUTING


def test_action_success_without_fresh_feedback_is_not_converged() -> None:
    handle = FakeGoalHandle(result=ResultEnvelope())
    controller = make_controller(handle)
    candidate = accepted()
    result = controller.execute_accepted(
        candidate,
        expected_state=controller.state,
        context_provider=context,
        timeout_s=0.1,
    )
    assert result.status.value == "not_converged"
    assert result.detail == "no_fresh_feedback"
    assert controller.state.mode is ControllerMode.FAULTED


@pytest.mark.parametrize(
    ("positions", "velocities", "expected_status", "expected_detail"),
    [
        ((0.4, -0.5), (0.0, 0.0), "succeeded", None),
        ((0.8, -0.5), (0.0, 0.0), "not_converged", "feedback_outside_convergence_tolerance"),
        ((0.4, -0.5), (0.1, 0.0), "not_converged", "feedback_outside_convergence_tolerance"),
    ],
)
def test_action_success_requires_position_and_stop_velocity_convergence(
    positions,
    velocities,
    expected_status,
    expected_detail,
) -> None:
    handle = FakeGoalHandle(
        result=ResultEnvelope(),
        feedback=joint_state(positions=positions, velocities=velocities, sec=3),
    )
    controller = make_controller(handle)
    result = controller.execute_accepted(
        accepted(),
        expected_state=controller.state,
        context_provider=context,
        timeout_s=0.1,
    )
    assert result.status.value == expected_status
    assert result.detail == expected_detail
