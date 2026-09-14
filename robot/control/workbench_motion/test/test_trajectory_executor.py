from __future__ import annotations

from dataclasses import replace

import pytest
from workbench_motion.controller import Controller, RobotCommand, RobotState
from workbench_motion.joint_limits import (
    AcceptedTrajectory,
    JointLimit,
    PreflightPolicy,
    build_preflight_context,
    preflight_trajectory,
)
from workbench_motion.trajectory_executor import (
    AcceptedTrajectoryExecutor,
    ExecutionGateReason,
    ExecutionGateStatus,
    state_hash,
)

JOINTS = ("j1", "j2")
LIMITS = {name: JointLimit(-2.0, 2.0, 2.0, 10.0) for name in JOINTS}
POLICY = PreflightPolicy("test-1", 1e-6, 30.0, 0.05)


class ProbeController(Controller):
    def __init__(self, state: RobotState, *, accept: bool = True) -> None:
        super().__init__(state)
        self.calls = 0
        self.accept = accept

    def _dispatch_motion(self, command: RobotCommand) -> tuple[bool, str | None]:
        self.calls += 1
        return (True, None) if self.accept else (False, "backend_rejected")


def context():
    return build_preflight_context(
        policy=POLICY,
        expected_joint_names=JOINTS,
        hard_limits=LIMITS,
        override_limits={},
    )


def accepted():
    result = preflight_trajectory(
        {
            "joint_names": list(JOINTS),
            "points": [{"positions": [0.0, 0.0], "time_from_start": 0.0}],
        },
        {name: 0.0 for name in JOINTS},
        context=context(),
    )
    assert isinstance(result, AcceptedTrajectory), result
    return result


def controller():
    return ProbeController(RobotState(JOINTS, (0.0, 0.0)))


def test_matching_state_and_context_dispatch_once() -> None:
    ctl = controller()
    result = AcceptedTrajectoryExecutor(ctl, context).execute(accepted(), expected_state=ctl.state)
    assert result.status is ExecutionGateStatus.ACCEPTED
    assert result.dispatch_attempted is True
    assert ctl.calls == 1


def test_controller_rejection_records_attempted_dispatch() -> None:
    ctl = ProbeController(RobotState(JOINTS, (0.0, 0.0)), accept=False)
    result = AcceptedTrajectoryExecutor(ctl, context).execute(accepted(), expected_state=ctl.state)
    assert result.status is ExecutionGateStatus.REJECTED
    assert result.reason is ExecutionGateReason.CONTROLLER_REJECTED
    assert result.dispatch_attempted is True
    assert result.receipt is not None
    assert result.receipt.reason == "backend_rejected"
    assert ctl.calls == 1


@pytest.mark.parametrize(
    "mutation,reason",
    [
        (lambda ctx: replace(ctx, context_sha256="sha256:" + "0" * 64), ExecutionGateReason.CONTEXT_MISMATCH),
    ],
)
def test_context_mismatch_is_zero_dispatch(mutation, reason) -> None:
    ctl = controller()
    current = context()
    result = AcceptedTrajectoryExecutor(ctl, lambda: mutation(current)).execute(accepted(), expected_state=ctl.state)
    assert result.status is ExecutionGateStatus.REJECTED
    assert result.reason is reason
    assert result.dispatch_attempted is False
    assert ctl.calls == 0


def test_stale_state_is_zero_dispatch() -> None:
    ctl = controller()
    expected = ctl.state
    ctl.update_state(RobotState(JOINTS, (0.1, 0.0), sequence=1))
    result = AcceptedTrajectoryExecutor(ctl, context).execute(accepted(), expected_state=expected)
    assert result.reason is ExecutionGateReason.STATE_STALE
    assert result.dispatch_attempted is False
    assert ctl.calls == 0


def test_state_change_during_context_read_is_zero_dispatch() -> None:
    ctl = controller()
    expected = ctl.state

    def context_after_feedback():
        ctl.update_state(RobotState(JOINTS, (0.1, 0.0), sequence=1))
        return context()

    result = AcceptedTrajectoryExecutor(ctl, context_after_feedback).execute(accepted(), expected_state=expected)
    assert result.reason is ExecutionGateReason.STATE_STALE
    assert result.observed_state_hash == state_hash(ctl.state)
    assert result.dispatch_attempted is False
    assert ctl.calls == 0


def test_invalid_trajectory_type_and_tampered_hash_are_zero_dispatch() -> None:
    ctl = controller()
    executor = AcceptedTrajectoryExecutor(ctl, context)
    invalid = executor.execute(object(), expected_state=ctl.state)  # type: ignore[arg-type]
    assert invalid.reason is ExecutionGateReason.INVALID_TRAJECTORY
    assert ctl.calls == 0

    candidate = accepted()
    tampered = object.__new__(AcceptedTrajectory)
    for field in ("snapshot", "canonical_bytes", "policy_version", "effective_limits_sha256", "context_sha256"):
        object.__setattr__(tampered, field, getattr(candidate, field))
    object.__setattr__(tampered, "trajectory_sha256", "sha256:" + "0" * 64)
    result = executor.execute(tampered, expected_state=ctl.state)
    assert result.reason is ExecutionGateReason.TRAJECTORY_HASH
    assert result.dispatch_attempted is False
    assert ctl.calls == 0


def test_invalid_trajectory_with_raising_context_property_is_rejected() -> None:
    ctl = controller()

    class MalformedTrajectory:
        @property
        def context_sha256(self):
            raise RuntimeError("malformed trajectory")

    result = AcceptedTrajectoryExecutor(ctl, context).execute(MalformedTrajectory(), expected_state=ctl.state)
    assert result.reason is ExecutionGateReason.INVALID_TRAJECTORY
    assert result.expected_context_hash == ""
    assert result.dispatch_attempted is False
    assert ctl.calls == 0


def test_state_hash_is_deterministic_and_changes_with_feedback() -> None:
    first = RobotState(JOINTS, (0.0, 0.0))
    second = RobotState(JOINTS, (0.0, 0.0), sequence=1)
    assert state_hash(first) == state_hash(first)
    assert state_hash(first) != state_hash(second)
