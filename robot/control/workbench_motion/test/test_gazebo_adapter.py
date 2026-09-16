from types import SimpleNamespace as NS

import pytest
from test_motion_safety import CONTEXT, LIMITS, NAMES, state
from workbench_motion.gazebo_adapter import decode_joint_state, trajectory_message
from workbench_motion.motion_safety import point_to_point


def message(names=NAMES, q=(0.1, 0.2), v=(0.2, 0.1), sec=1, ns=40_000_000):
    return NS(name=names, position=q, velocity=v, header=NS(stamp=NS(sec=sec, nanosec=ns)))


def test_feedback_reorders_by_joint_identity_and_estimates_acceleration():
    decoded = decode_joint_state(message(names=("j2", "j1")), robot_id="test", joint_names=NAMES, previous=state())
    assert decoded.positions == (0.2, 0.1)
    assert decoded.velocities == (0.1, 0.2)
    assert decoded.accelerations == pytest.approx((2.5, 5.0))
    assert decoded.clock_id == "ros_sim"


@pytest.mark.parametrize(
    "sample",
    [
        message(v=()),
        message(names=("j1", "j1")),
        message(q=(float("nan"), 0.2)),
        message(ns=-1),
        message(names=("j1", "missing")),
    ],
)
def test_incomplete_or_invalid_feedback_is_rejected(sample):
    with pytest.raises(ValueError):
        decode_joint_state(sample, robot_id="test", joint_names=NAMES)


def test_materialization_copies_only_the_frozen_snapshot_with_full_derivatives():
    trajectory = point_to_point(state(), (0.1, 0), CONTEXT, LIMITS)
    result = trajectory_message(trajectory, lambda: NS(points=[]), lambda: NS(time_from_start=NS()))
    assert result.joint_names == list(NAMES)
    assert result.points[-1].velocities == [0, 0]
    assert result.points[-1].accelerations == [0, 0]
    assert (
        result.points[-1].time_from_start.sec * 1_000_000_000 + result.points[-1].time_from_start.nanosec
        == trajectory.snapshot.points[-1].time_from_start_ns
    )
    result.points[-1].positions[0] = 99
    assert trajectory.snapshot.points[-1].positions[0] == 0.1
    with pytest.raises(TypeError):
        trajectory_message({}, lambda: NS(points=[]), lambda: NS())


@pytest.fixture
def adapter(monkeypatch):
    """ROS-client boundary doubles only; these tests supply no physics evidence."""
    import sys

    from workbench_motion.arm_config import load_arm_config
    from workbench_motion.gazebo_adapter import GazeboAdapter

    adapter = GazeboAdapter.__new__(GazeboAdapter)
    adapter.arm = load_arm_config()
    adapter._pending_send = None
    adapter._late_result = None
    adapter._late_rejected = False
    adapter._handles = {}
    adapter._results = {}
    adapter.send_count = 0
    adapter._description = None
    adapter.probe = NS(_spin=lambda future: future.result() if future.done() else None, close=lambda: None)
    monkeypatch.setitem(sys.modules, "control_msgs.action", NS(FollowJointTrajectory=NS(Goal=lambda: NS())))
    monkeypatch.setitem(
        sys.modules,
        "trajectory_msgs.msg",
        NS(
            JointTrajectory=lambda: NS(points=[], header=NS(stamp=NS())),
            JointTrajectoryPoint=lambda: NS(time_from_start=NS()),
        ),
    )
    monkeypatch.setitem(
        sys.modules, "action_msgs.msg", NS(GoalStatus=NS(STATUS_SUCCEEDED=4, STATUS_ABORTED=6, STATUS_CANCELED=5))
    )
    return adapter


def future(value=None, *, pending=False):
    from concurrent.futures import Future

    result = Future()
    if not pending:
        result.set_result(value)
    return result


@pytest.mark.parametrize("missing_index", [None, 3, 4])
@pytest.mark.parametrize("clock_marker", [True, False, None])
def test_readiness_rejects_unset_boolean_parameters(adapter, monkeypatch, missing_index, clock_marker):
    import workbench_motion.gazebo_adapter as module

    monkeypatch.setattr(module, "hardware_is_gazebo", lambda _: True)
    values = [
        NS(type=9, string_array_value=["position"]),
        NS(type=9, string_array_value=["position", "velocity"]),
        NS(type=4, string_value="splines"),
        NS(type=1, bool_value=False),
        NS(type=1, bool_value=False),
    ]
    if missing_index is not None:
        values[missing_index].type = 0
    adapter.probe.endpoint_status = lambda: {"service": True}
    adapter.probe.robot_description = lambda: "gazebo-description"
    adapter.probe.controller_states = lambda: [
        {"name": name, "state": "active"}
        for name in (
            adapter.arm.joint_state_broadcaster,
            adapter.arm.arm_trajectory_controller,
            adapter.arm.gripper_controller,
        )
    ]
    adapter.scene_client = NS(wait_for_service=lambda **_: True)
    adapter.controller_parameters = NS(get_parameters=lambda _: future(NS(values=values)))
    adapter.manager_parameters = NS(
        get_parameters=lambda _: future(NS(values=[NS(type=0 if clock_marker is None else 1, bool_value=clock_marker)]))
    )
    assert adapter.readiness() is (missing_index is None and clock_marker is True)


def test_rejected_goal_counts_attempt_and_never_creates_active_handle(adapter):
    adapter.probe.arm_action = NS(send_goal_async=lambda _: future(NS(accepted=False)))
    assert adapter.send(point_to_point(state(), (0.1, 0), CONTEXT, LIMITS), start_time_s=1.25) is None
    assert adapter.send_count == 1
    assert adapter._handles == {}
    assert adapter._pending_send is None


def test_action_success_requires_both_ros_status_and_controller_success(adapter):
    result = future(NS(status=4, result=NS(error_code=-1)))
    handle = NS(accepted=True, get_result_async=lambda: result)
    adapter.probe.arm_action = NS(send_goal_async=lambda _: future(handle))
    key = adapter.send(point_to_point(state(), (0.1, 0), CONTEXT, LIMITS), start_time_s=1.25)
    assert adapter.status(key) == "unknown"
    adapter._results[key] = future(NS(status=4, result=NS(error_code=0)))
    assert adapter.status(key) == "succeeded"
    adapter._results[key] = future(pending=True)
    assert adapter.status(key) is None


def test_late_acceptance_is_canceled_during_close_and_never_restores_readiness(adapter):
    acceptance = future(pending=True)
    terminal = future(pending=True)
    canceled = []
    closed = []
    # Cancellation rejection is not terminal evidence.
    handle = NS(
        accepted=True,
        get_result_async=lambda: terminal,
        cancel_goal_async=lambda: canceled.append(True) or future(NS(goals_canceling=[])),
    )
    adapter.probe.arm_action = NS(send_goal_async=lambda _: acceptance)
    adapter.probe.close = lambda: closed.append(True)

    def spin():
        if not acceptance.done():
            acceptance.set_result(handle)
        else:
            terminal.set_result(NS(status=4, result=NS(error_code=0)))

    adapter.spin = spin
    with pytest.raises(TimeoutError, match="acceptance unknown"):
        adapter.send(point_to_point(state(), (0.1, 0), CONTEXT, LIMITS), start_time_s=1.25)
    adapter.close()
    assert canceled == [True]
    assert closed == [True]
    assert adapter.readiness() is False


def test_unresolved_acceptance_prevents_silent_client_destruction(adapter):
    adapter._pending_send = future(pending=True)
    closed = []
    adapter.probe.close = lambda: closed.append(True)
    with pytest.raises(TimeoutError, match="shutdown blocked"):
        adapter.close(timeout_s=0)
    assert not closed


def test_cancellation_timeout_is_not_a_stopped_confirmation(adapter):
    adapter._handles["1"] = NS(cancel_goal_async=lambda: future(pending=True))
    with pytest.raises(TimeoutError, match="cancellation unconfirmed"):
        adapter.cancel("1")


def test_scene_hash_ignores_transport_timestamps_but_binds_geometry(adapter, monkeypatch):
    import copy
    import sys

    scene = {
        "world": {
            "collision_objects": [],
            "octomap": {"octomap": {"header": {"stamp": 1, "frame_id": "world"}, "data": []}},
        },
        "allowed_collision_matrix": {},
        "link_padding": [],
        "link_scale": [],
        "robot_state": {"attached_collision_objects": [], "joint_state": {"position": [0]}},
        "fixed_frame_transforms": [
            {"header": {"frame_id": "world", "stamp": 1}, "child_frame_id": "base", "transform": {"x": 0}}
        ],
    }
    names = (
        "WORLD_OBJECT_GEOMETRY",
        "OCTOMAP",
        "TRANSFORMS",
        "ALLOWED_COLLISION_MATRIX",
        "LINK_PADDING_AND_SCALING",
        "ROBOT_STATE_ATTACHED_OBJECTS",
    )
    monkeypatch.setitem(
        sys.modules, "moveit_msgs.msg", NS(PlanningSceneComponents=NS(**{k: 1 << i for i, k in enumerate(names)}))
    )
    monkeypatch.setitem(sys.modules, "moveit_msgs.srv", NS(GetPlanningScene=NS(Request=lambda: NS(components=NS()))))
    monkeypatch.setitem(sys.modules, "rosidl_runtime_py.convert", NS(message_to_ordereddict=copy.deepcopy))
    adapter.scene_client = NS(call_async=lambda _: future(NS(scene=scene)))
    original = adapter.scene_revision()
    scene["robot_state"]["joint_state"]["position"] = [1]
    scene["world"]["octomap"]["octomap"]["header"]["stamp"] = 2
    scene["fixed_frame_transforms"][0]["header"]["stamp"] = 2
    assert adapter.scene_revision() == original
    scene["world"]["collision_objects"].append({"id": "obstacle"})
    assert adapter.scene_revision() != original


def test_collision_gate_propagates_invalid_state_and_rejects_excessive_sample_budget(adapter):
    trajectory = point_to_point(state(), (0.1, 0), CONTEXT, LIMITS)
    checked = []

    def collision_check(states):
        checked.extend(states)
        return {"all_valid": False}

    adapter.probe.collision_check = collision_check
    adapter.max_velocity = 0.2
    assert adapter.check_path(trajectory, 0.05) is False
    assert checked[0] == {"j1": 0, "j2": 0}
    assert checked[-1]["j1"] == pytest.approx(0.1)
    assert len(checked) > 2
    checked.clear()
    with pytest.raises(ValueError, match="sample budget"):
        adapter.check_path(trajectory, 1e-8)
    assert checked == []


def test_missing_endpoint_rejects_readiness_before_parameter_calls(adapter):
    adapter.probe.endpoint_status = lambda: {"arm_action": False}
    assert adapter.readiness() is False


def test_authoritative_feedback_time_is_not_rejected_when_dds_clock_lags(adapter):
    adapter.node = NS(get_clock=lambda: NS(now=lambda: NS(nanoseconds=999_000_000)))
    adapter._latest = state()
    assert adapter.now_s() == 1.0
    adapter.node = NS(get_clock=lambda: NS(now=lambda: NS(nanoseconds=1_010_000_000)))
    assert adapter.now_s() == 1.01


def test_goal_preserves_explicit_absolute_epoch(adapter):
    captured = []
    adapter.probe.arm_action = NS(send_goal_async=lambda goal: captured.append(goal) or future(NS(accepted=False)))
    trajectory = point_to_point(state(), (0.1, 0), CONTEXT, LIMITS)
    adapter.send(trajectory, start_time_s=17.123456789)
    stamp = captured[0].trajectory.header.stamp
    assert (stamp.sec, stamp.nanosec) == (17, 123456789)
    assert captured[0].trajectory.points[0].time_from_start.sec == 0


@pytest.mark.parametrize("epoch", [0, -1, float("nan"), float("inf")])
def test_invalid_absolute_epoch_never_sends(adapter, epoch):
    with pytest.raises(ValueError, match="epoch"):
        adapter.send(point_to_point(state(), (0.1, 0), CONTEXT, LIMITS), start_time_s=epoch)
    assert adapter.send_count == 0
