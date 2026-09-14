"""ROS-free tests for phase2_probe orchestration and evidence math."""

from __future__ import annotations

import inspect
import json
import math
from dataclasses import replace
from itertools import pairwise
from types import SimpleNamespace

import pytest
from workbench_motion.arm_config import load_arm_config
from workbench_motion.controller import RobotState
from workbench_motion.joint_limits import (
    AcceptedTrajectory,
    JointLimit,
    PreflightPolicy,
    ReasonCode,
    Violation,
    build_preflight_context,
)
from workbench_motion.phase2_probe import (
    FAIL_BEHAVIORS,
    FOLLOWERS,
    PHASE2_ACCEPTED_BEHAVIORS,
    ActionObservation,
    JointSnapshot,
    RosProbeIO,
    _violation_dict,
    atomic_write_report,
    behavior_gate,
    classify_over_limit,
    densify_joint_path,
    hardware_is_gazebo,
    is_stale,
    mimic_ratios,
    run_probe,
    smoothness_report,
    snapshot_is_complete,
)

CONFIG = __import__("pathlib").Path(__file__).resolve().parent.parent / "config"
ARM = load_arm_config(CONFIG / "arm.yaml")
LIMITS = {joint: JointLimit(-2.0, 2.0, 1.0, 10.0) for joint in ARM.joints}
BASE = {joint: 0.0 for joint in ARM.joints}


class AvailableEndpoint:
    def wait_for_service(self, timeout_sec):
        return timeout_sec > 0

    def wait_for_server(self, timeout_sec):
        return timeout_sec > 0


class AvailableParameterEndpoints:
    def wait_for_services(self, timeout_sec):
        return timeout_sec > 0


def snapshot(**updates):
    values = dict(BASE)
    values.update(updates)
    return JointSnapshot(values, 10.0)


def test_endpoint_status_uses_jazzy_parameter_client_api():
    probe = object.__new__(RosProbeIO)
    probe.timeout_s = 1.0
    probe.list_client = AvailableEndpoint()
    probe.state_client = AvailableEndpoint()
    probe.arm_action = AvailableEndpoint()
    probe.gripper_action = AvailableEndpoint()
    probe.parameter_client = AvailableParameterEndpoints()

    assert all(probe.endpoint_status().values())


def test_tf_lookup_spins_the_probe_node_without_a_competing_executor():
    init_source = inspect.getsource(RosProbeIO.__init__)
    assert "spin_thread=True" not in init_source
    assert 'Parameter("use_sim_time", Parameter.Type.BOOL, True)' in init_source
    assert "self.rclpy.spin_once" in inspect.getsource(RosProbeIO.tf_chain)


@pytest.mark.parametrize(
    "observation,expected",
    [
        (ActionObservation(False, "rejected", False, {ARM.joints[0]: 2.1}, snapshot(), snapshot()), "rejected"),
        (ActionObservation(True, "aborted", False, {ARM.joints[0]: 2.1}, snapshot(), snapshot()), "aborted"),
        (
            ActionObservation(
                True,
                "aborted",
                False,
                {ARM.joints[0]: 2.1},
                snapshot(**{ARM.joints[0]: 0.05}),
                snapshot(**{ARM.joints[0]: 2.0}),
            ),
            "clamped",
        ),
        (
            ActionObservation(
                True, "aborted", False, {ARM.joints[0]: 2.1}, snapshot(), snapshot(**{ARM.joints[0]: 0.5})
            ),
            "unclassified",
        ),
        (
            ActionObservation(
                True, "succeeded", False, {ARM.joints[0]: 2.1}, snapshot(), snapshot(**{ARM.joints[0]: 2.0})
            ),
            "clamped",
        ),
        (
            ActionObservation(
                True, "succeeded", False, {ARM.joints[0]: 2.1}, snapshot(), snapshot(**{ARM.joints[0]: 2.05})
            ),
            "executed_over_limit",
        ),
        (ActionObservation(True, "unknown", True, {ARM.joints[0]: 2.1}, snapshot(), snapshot()), "timeout"),
        (
            ActionObservation(
                True, "succeeded", False, {ARM.joints[0]: 2.1}, snapshot(), snapshot(**{ARM.joints[0]: 0.5})
            ),
            "unclassified",
        ),
    ],
)
def test_six_class_over_limit_behavior(observation, expected):
    assert classify_over_limit(observation, LIMITS) == expected
    assert behavior_gate(expected) == ("safe" if expected in {"rejected", "aborted"} else "fail")


def test_last_four_classes_fail_gate():
    assert FAIL_BEHAVIORS == {"clamped", "executed_over_limit", "timeout", "unclassified"}
    assert PHASE2_ACCEPTED_BEHAVIORS == {"rejected", "aborted", "clamped"}


def test_executed_over_limit_in_an_intermediate_sample_is_not_hidden_by_final_state():
    observation = ActionObservation(
        True,
        "aborted",
        False,
        {ARM.joints[0]: 2.1},
        snapshot(),
        snapshot(),
        (snapshot(**{ARM.joints[0]: 2.05}),),
    )
    assert classify_over_limit(observation, LIMITS) == "executed_over_limit"


@pytest.mark.parametrize(
    "positions",
    [
        {ARM.joints[0]: 0.0},
        {**BASE, ARM.joints[1]: math.nan},
        {**BASE, ARM.joints[1]: math.inf},
    ],
)
def test_incomplete_or_nonfinite_snapshots_are_not_safe(positions):
    before = snapshot()
    after = JointSnapshot(dict(positions), 10.1)
    observation = ActionObservation(True, "aborted", False, {ARM.joints[0]: 2.1}, before, after)
    assert not snapshot_is_complete(after, ARM.joints)
    assert classify_over_limit(observation, LIMITS) == "unclassified"


def test_smoothness_report_rejects_velocity_jump_and_overshoot():
    samples = [
        snapshot(),
        JointSnapshot({**BASE, ARM.joints[0]: 0.2}, 10.1),
        JointSnapshot({**BASE, ARM.joints[0]: 0.21}, 10.1001),
    ]
    report = smoothness_report(samples, {**BASE, ARM.joints[0]: 0.2})
    assert not report["valid"]
    assert not report["velocity_continuous"]


def test_smoothness_report_accepts_monotonic_samples():
    target = {**BASE, ARM.joints[0]: 0.2}
    samples = [snapshot(), JointSnapshot({**BASE, ARM.joints[0]: 0.1}, 10.5), JointSnapshot(target, 11.0)]
    assert smoothness_report(samples, target)["valid"]


@pytest.mark.parametrize(
    "names,positions",
    [
        (ARM.joints[:-1], [0.0] * (len(ARM.joints) - 1)),
        (ARM.joints, [0.0] * (len(ARM.joints) - 1)),
        (ARM.joints, [0.0, math.nan, 0.0, 0.0, 0.0, 0.0]),
        (ARM.joints, [0.0, math.inf, 0.0, 0.0, 0.0, 0.0]),
    ],
)
def test_ros_callback_drops_incomplete_or_nonfinite_joint_states(names, positions):
    probe = object.__new__(RosProbeIO)
    probe.arm = ARM
    probe.latest = None
    probe.history = []
    message = SimpleNamespace(
        name=list(names),
        position=list(positions),
        header=SimpleNamespace(stamp=SimpleNamespace(sec=10, nanosec=0)),
    )
    probe._on_joint_state(message)
    assert probe.latest is None
    assert probe.history == []


def test_ros_callback_does_not_record_feedback_rejected_by_controller():
    probe = object.__new__(RosProbeIO)
    probe.arm = ARM
    probe.latest = None
    probe.history = []

    class Controller:
        def ingest_joint_state(self, _message):
            return False

    probe.gazebo_controller = Controller()
    message = SimpleNamespace(
        name=list(ARM.joints),
        position=[0.0] * len(ARM.joints),
        header=SimpleNamespace(stamp=SimpleNamespace(sec=10, nanosec=0)),
    )
    probe._on_joint_state(message)
    assert probe.latest is None
    assert probe.history == []


def test_ros_probe_legal_arm_action_uses_accepted_trajectory_adapter():
    probe = object.__new__(RosProbeIO)
    probe.arm = ARM
    probe.timeout_s = 1.0
    probe.latest = snapshot()
    probe.history = [probe.latest]

    def snapshot_provider(_staleness):
        return probe.latest

    probe.joint_snapshot = snapshot_provider
    probe.context_provider = lambda: build_preflight_context(
        policy=PreflightPolicy("test", 1e-6, 30.0, 0.05),
        expected_joint_names=ARM.joints,
        hard_limits=LIMITS,
        override_limits={},
    )

    class Adapter:
        def __init__(self):
            self.state = RobotState(ARM.joints, tuple(BASE.values()), timestamp_ns=11_000_000_000, sequence=1)
            self.calls = []

        def execute_accepted(self, accepted, *, expected_state, context_provider, timeout_s):
            self.calls.append((accepted, expected_state, context_provider, timeout_s))
            return SimpleNamespace(
                gate=SimpleNamespace(
                    status=SimpleNamespace(value="accepted"),
                    reason=None,
                    dispatch_attempted=True,
                    expected_state_hash="sha256:expected",
                    observed_state_hash="sha256:observed",
                    expected_context_hash=accepted.context_sha256,
                    observed_context_hash=accepted.context_sha256,
                ),
                status=SimpleNamespace(value="succeeded"),
                action_error_code=0,
                final_state=RobotState(ARM.joints, tuple(BASE.values()), timestamp_ns=12_000_000_000, sequence=2),
                max_position_error=0.0,
                max_velocity=0.0,
                detail=None,
            )

    probe.gazebo_controller = Adapter()
    target = dict(BASE)
    target[ARM.joints[0]] = 0.02
    observation = probe.execute_arm(target, 2.0, 1.0)
    assert len(probe.gazebo_controller.calls) == 1
    accepted, expected_state, context_provider, timeout_s = probe.gazebo_controller.calls[0]
    assert isinstance(accepted, AcceptedTrajectory)
    assert expected_state is probe.gazebo_controller.state
    assert context_provider is probe.context_provider
    assert timeout_s == 3.0
    assert observation.gate["dispatch_attempted"] is True
    assert observation.execution["convergence_status"] == "verified"
    assert observation.execution["fresh_feedback"]["advanced"] is True


def test_ros_probe_raw_arm_action_is_a_separate_safety_path():
    probe = object.__new__(RosProbeIO)
    probe.arm = ARM
    probe.timeout_s = 1.0
    probe.latest = snapshot()
    probe.history = [probe.latest]

    def snapshot_provider(_staleness):
        return probe.latest

    def spin(future, timeout_s=None):
        return future.result() if future.done() else None

    probe.joint_snapshot = snapshot_provider
    probe._spin = spin

    class Goal:
        accepted = False

    class Client:
        def send_goal_async(self, _goal):
            return SimpleNamespace(done=lambda: True, result=lambda: Goal())

    probe.arm_action = Client()
    target = dict(BASE)
    target[ARM.joints[0]] = 2.1
    observation = probe.execute_raw_arm(target, 2.0, 1.0)
    assert observation.accepted is False
    assert observation.status == "rejected"


def test_ros_probe_preflight_rejection_is_zero_dispatch():
    probe = object.__new__(RosProbeIO)
    probe.arm = ARM
    probe.timeout_s = 1.0
    probe.latest = snapshot()
    probe.history = [probe.latest]

    def snapshot_provider(_staleness):
        return probe.latest

    class Adapter:
        def __init__(self):
            self.state = RobotState(ARM.joints, tuple(BASE.values()), timestamp_ns=10_000_000_000)
            self.calls = 0

        def execute_accepted(self, **_kwargs):
            self.calls += 1
            raise AssertionError("preflight rejection must not call the Gazebo adapter")

    probe.joint_snapshot = snapshot_provider
    probe.context_provider = lambda: build_preflight_context(
        policy=PreflightPolicy("test", 1e-6, 30.0, 0.05),
        expected_joint_names=ARM.joints,
        hard_limits=LIMITS,
        override_limits={},
    )
    probe.gazebo_controller = Adapter()
    target = dict(BASE)
    target[ARM.joints[0]] = LIMITS[ARM.joints[0]].max_position + 0.1
    observation = probe.execute_arm(target, 2.0, 1.0)
    assert observation.gate["dispatch_attempted"] is False
    assert observation.gate["reason"] == "position"
    assert observation.execution["convergence_status"] == "not_converged"
    assert probe.gazebo_controller.calls == 0


def test_mimic_ratio_uses_deltas_not_absolute_positions():
    before = {ARM.driver_joint: 0.1, **{joint: 0.2 for joint in FOLLOWERS}}
    after = {ARM.driver_joint: 0.6}
    after.update({joint: before[joint] + nominal * 0.5 for joint, nominal in FOLLOWERS.items()})
    result = mimic_ratios(before, after, ARM.driver_joint)
    assert result["all_ok"]
    assert result["control_declaration"] == "explicit_followers_with_vendor_multipliers"
    assert [entry["observed_ratio"] for entry in result["followers"]] == pytest.approx(list(FOLLOWERS.values()))


def test_mimic_ratio_rejects_zero_driver_delta():
    values = {ARM.driver_joint: 0.1, **{joint: 0.2 for joint in FOLLOWERS}}
    with pytest.raises(ValueError, match="did not move"):
        mimic_ratios(values, values, ARM.driver_joint)


def test_staleness_and_hardware_fail_closed_helpers():
    assert is_stale(8.0, 10.0, 1.0)
    assert is_stale(math.nan, 10.0, 1.0)
    assert not is_stale(9.5, 10.0, 1.0)
    assert hardware_is_gazebo("<plugin>gz_ros2_control/GazeboSimSystem</plugin>")
    assert not hardware_is_gazebo("<plugin>ur_robot_driver/URPositionHardwareInterface</plugin>")


def test_densify_uses_fixed_max_joint_step():
    states = densify_joint_path([{"a": 0.0, "b": 0.0}, {"a": 0.11, "b": 0.02}], 0.05)
    assert len(states) == 4
    assert states[-1] == {"a": 0.11, "b": 0.02}
    assert all(abs(b["a"] - a["a"]) <= 0.05 for a, b in pairwise(states))


def valid_report():
    return {
        "generated_at": "now",
        "commit": "abc",
        "git_dirty": False,
        "config_hashes": {},
        "versions": {},
        "arm": ARM.arm_label,
        "controllers": [],
        "tf_chain": {},
        "gripper_mimic": {},
        "legal_trajectory": {
            "execution_path": "accepted_trajectory_adapter",
            "smoothness": {"valid": True},
            "execution": {
                "gate": {"status": "accepted", "dispatch_attempted": True},
                "action": {
                    "status": "succeeded",
                    "convergence_status": "verified",
                    "fresh_feedback": {"advanced": True},
                    "max_position_error": 0.0,
                    "max_velocity": 0.0,
                },
            },
        },
        "observed_controller_over_limit_behavior": {"execution_path": "raw_safety_probe", "kind": "rejected"},
        "validator_violation": {"kind": "position"},
        "all_passed": True,
    }


def test_validator_violation_evidence_keeps_exact_legacy_shape():
    payload = _violation_dict(
        Violation(
            kind=ReasonCode.POSITION,
            message="outside",
            joint="j1",
            value=2.0,
            bound=1.0,
            point_index=3,
        )
    )
    assert payload == {
        "kind": "position",
        "message": "outside",
        "joint": "j1",
        "value": 2.0,
        "bound": 1.0,
        "point_index": 3,
    }
    assert _violation_dict(None) is None


def test_atomic_report_schema_and_replace(tmp_path):
    output = tmp_path / "report.json"
    atomic_write_report(output, valid_report())
    assert json.loads(output.read_text(encoding="utf-8"))["all_passed"] is True
    broken = valid_report()
    del broken["tf_chain"]
    with pytest.raises(ValueError, match="missing fields"):
        atomic_write_report(output, broken)
    assert json.loads(output.read_text(encoding="utf-8"))["all_passed"] is True


def test_report_rejects_malformed_execution_evidence(tmp_path):
    output = tmp_path / "report.json"
    broken = valid_report()
    broken["legal_trajectory"]["execution"] = {"gate": {}, "action": {"convergence_status": "unknown"}}
    with pytest.raises(ValueError, match="convergence status"):
        atomic_write_report(output, broken)


class FakeIO:
    def __init__(self, *, endpoints=True, gazebo=True, over_kind="rejected", tf_present=True, joint_error=False):
        self.endpoints = endpoints
        self.gazebo = gazebo
        self.over_kind = over_kind
        self.tf_present = tf_present
        self.joint_error = joint_error
        self.arm_calls = 0
        self.raw_arm_calls = 0
        positions = dict(BASE)
        positions[ARM.driver_joint] = 0.0
        positions.update({joint: 0.0 for joint in FOLLOWERS})
        self.now = JointSnapshot(positions, 10.0)

    def endpoint_status(self):
        return {
            "list": self.endpoints,
            "action": self.endpoints,
            "state_validity": self.endpoints,
            "tf": self.endpoints,
        }

    def robot_description(self):
        return "<plugin>gz_ros2_control/GazeboSimSystem</plugin>" if self.gazebo else "<plugin>real_hardware</plugin>"

    def controller_states(self):
        return [
            {"name": ARM.joint_state_broadcaster, "type": "jsb", "state": "active"},
            {"name": ARM.arm_trajectory_controller, "type": "jtc", "state": "active"},
            {"name": ARM.gripper_controller, "type": "gripper", "state": "active"},
        ]

    def tf_chain(self, frames, staleness_s):
        return {
            "expected": list(frames),
            "present": self.tf_present,
            "missing": [],
            "stale": [] if self.tf_present else ["world->base_link"],
        }

    def joint_snapshot(self, staleness_s):
        if self.joint_error:
            raise RuntimeError("stale joint state")
        return self.now

    def execute_arm(self, target, duration_s, staleness_s):
        self.arm_calls += 1
        before = self.now
        if self.arm_calls == 1:
            after_positions = dict(self.now.positions)
            after_positions.update(target)
            self.now = JointSnapshot(after_positions, 10.1)
            return ActionObservation(
                True,
                "succeeded",
                False,
                dict(target),
                before,
                self.now,
                (self.now,),
                gate={"status": "accepted", "dispatch_attempted": True},
                execution={
                    "status": "succeeded",
                    "convergence_status": "verified",
                    "fresh_feedback": {
                        "expected_sequence": 0,
                        "expected_timestamp_ns": 10_000_000_000,
                        "sequence": 1,
                        "timestamp_ns": 10_100_000_000,
                        "advanced": True,
                    },
                    "max_position_error": 0.0,
                    "max_velocity": 0.0,
                },
            )
        raise AssertionError("raw safety probe must not use execute_arm")

    def execute_raw_arm(self, target, duration_s, staleness_s):
        self.raw_arm_calls += 1
        before = self.now
        if self.over_kind == "rejected":
            return ActionObservation(False, "rejected", False, dict(target), before, before)
        if self.over_kind == "aborted":
            return ActionObservation(True, "aborted", False, dict(target), before, before)
        if self.over_kind == "timeout":
            return ActionObservation(True, "unknown", True, dict(target), before, before)
        after_positions = dict(before.positions)
        after_positions[ARM.joints[0]] = {
            "clamped": LIMITS[ARM.joints[0]].max_position,
            "executed_over_limit": LIMITS[ARM.joints[0]].max_position + 0.05,
            "unclassified": 0.5,
        }[self.over_kind]
        after = JointSnapshot(after_positions, 10.2)
        return ActionObservation(True, "succeeded", False, dict(target), before, after)

    def collision_check(self, states):
        return {
            "source": "moveit",
            "resolution_rad": 0.05,
            "states_checked": len(states),
            "all_valid": True,
            "first_invalid": None,
        }

    def execute_gripper(self, position, staleness_s):
        before = self.now
        after_positions = dict(before.positions)
        driver_delta = position - before.positions[ARM.driver_joint]
        after_positions[ARM.driver_joint] = position
        for joint, nominal in FOLLOWERS.items():
            after_positions[joint] = before.positions[joint] + nominal * driver_delta
        self.now = JointSnapshot(after_positions, 10.2)
        return ActionObservation(True, "succeeded", False, {ARM.driver_joint: position}, before, self.now)

    def versions(self):
        return {"ros_distro": "jazzy", "gz": "harmonic"}


def test_run_probe_refuses_non_gazebo_before_sending_any_trajectory(tmp_path):
    io = FakeIO(gazebo=False)
    output = tmp_path / "report.json"
    assert run_probe(io, arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG) == 2
    assert io.arm_calls == 0
    assert not output.exists()


def test_missing_endpoint_exits_nonzero_without_publishing(tmp_path, capsys):
    output = tmp_path / "report.json"
    assert (
        run_probe(FakeIO(endpoints=False), arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG) == 2
    )
    assert not output.exists()
    assert "required endpoints unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("io", [FakeIO(tf_present=False), FakeIO(joint_error=True)])
def test_stale_tf_or_joint_state_exits_nonzero_without_publishing(io, tmp_path):
    output = tmp_path / "report.json"
    assert run_probe(io, arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG) == 2
    assert not output.exists()


def test_successful_mock_probe_publishes_complete_json(tmp_path, monkeypatch):
    from workbench_motion import phase2_probe

    monkeypatch.setattr(phase2_probe, "_git_metadata", lambda repo: ("abc123", True))
    output = tmp_path / "phase2.json"
    rc = run_probe(FakeIO(), arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG)
    assert rc == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["observed_controller_over_limit_behavior"]["kind"] == "rejected"
    assert report["gripper_mimic"]["all_ok"]
    assert report["tf_chain"]["present"]
    assert report["legal_trajectory"]["execution"]["action"]["convergence_status"] == "verified"
    assert report["legal_trajectory"]["execution_path"] == "accepted_trajectory_adapter"
    assert report["observed_controller_over_limit_behavior"]["execution_path"] == "raw_safety_probe"


@pytest.mark.parametrize(
    ("section", "field", "value"),
    [
        ("gate", "status", "rejected"),
        ("gate", "dispatch_attempted", False),
        ("action", "status", "aborted"),
        ("action", "convergence_status", "not_converged"),
        ("fresh_feedback", "advanced", False),
        ("action", "max_position_error", 0.051),
        ("action", "max_velocity", 0.011),
    ],
)
def test_legal_probe_evidence_fails_closed_without_complete_verified_execution(section, field, value, tmp_path):
    io = FakeIO()
    original = io.execute_arm

    def incomplete_evidence(target, duration_s, staleness_s):
        observation = original(target, duration_s, staleness_s)
        gate = dict(observation.gate)
        action = dict(observation.execution)
        action["fresh_feedback"] = dict(action["fresh_feedback"])
        target_section = (
            gate if section == "gate" else action["fresh_feedback"] if section == "fresh_feedback" else action
        )
        target_section[field] = value
        return replace(observation, gate=gate, execution=action)

    io.execute_arm = incomplete_evidence
    output = tmp_path / "phase2.json"
    assert run_probe(io, arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG) == 2
    assert not output.exists()
    assert io.raw_arm_calls == 0


def test_clamped_controller_publishes_phase4_risk_evidence(tmp_path, monkeypatch):
    from workbench_motion import phase2_probe

    monkeypatch.setattr(phase2_probe, "_git_metadata", lambda repo: ("abc123", False))
    output = tmp_path / "phase2.json"
    io = FakeIO(over_kind="clamped")
    rc = run_probe(io, arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG)
    assert rc == 0
    report = json.loads(output.read_text(encoding="utf-8"))
    behavior = report["observed_controller_over_limit_behavior"]
    assert behavior["kind"] == "clamped"
    assert behavior["gate"] == "fail"
    assert behavior["is_phase4_bypass_risk"] is True
    assert behavior["requested_limit_excess"][0]["excess"] == pytest.approx(0.1)
    assert report["all_passed"] is True
    assert io.raw_arm_calls == 1


@pytest.mark.parametrize("kind", ["executed_over_limit", "timeout", "unclassified"])
def test_unsafe_or_inconclusive_behavior_publishes_diagnostic_evidence(kind, tmp_path, monkeypatch):
    from workbench_motion import phase2_probe

    monkeypatch.setattr(phase2_probe, "_git_metadata", lambda repo: ("abc123", False))
    output = tmp_path / "phase2.json"
    rc = run_probe(FakeIO(over_kind=kind), arm=ARM, limits=LIMITS, output=output, repo=tmp_path, config_dir=CONFIG)
    assert rc == 1
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["observed_controller_over_limit_behavior"]["kind"] == kind
    assert report["all_passed"] is False
