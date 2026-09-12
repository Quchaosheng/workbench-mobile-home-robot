from __future__ import annotations

import math

import pytest
from workbench_motion.controller import (
    CommandType,
    Controller,
    ControllerMode,
    DispatchStatus,
    RobotCommand,
    RobotState,
)

JOINTS = ("joint_a", "joint_b")


class ProbeController(Controller):
    def __init__(self) -> None:
        super().__init__(RobotState(JOINTS, (0.0, 0.0)))
        self.dispatched: list[str] = []
        self.cancelled: list[str] = []
        self.holds = 0
        self.stops = 0

    def _dispatch_motion(self, command: RobotCommand) -> tuple[bool, str | None]:
        self.dispatched.append(command.command_id)
        return True, None

    def _cancel_motion(self, command_id: str) -> bool:
        self.cancelled.append(command_id)
        return True

    def _hold_motion(self) -> bool:
        self.holds += 1
        return True

    def _safe_stop_motion(self, reason: str) -> bool:
        self.stops += 1
        return True

    def _reset_motion(self) -> bool:
        return True

    def _resume_motion(self, command_id: str) -> bool:
        return True


class RefusingController(ProbeController):
    def _dispatch_motion(self, command: RobotCommand) -> tuple[bool, str | None]:
        return False, "backend_unavailable"


def test_robot_state_is_immutable_and_validates_shape() -> None:
    state = RobotState(JOINTS, [1.0, 2.0], [0.0, 0.1])
    assert state.positions == (1.0, 2.0)
    with pytest.raises((AttributeError, TypeError)):
        state.positions[0] = 3.0  # type: ignore[index]
    with pytest.raises(ValueError, match="same length"):
        RobotState(JOINTS, (0.0,), (0.0, 0.0))
    with pytest.raises(ValueError, match="finite"):
        RobotState(JOINTS, (math.nan, 0.0))


def test_robot_command_factories_reject_malformed_commands() -> None:
    command = RobotCommand.position("cmd-1", JOINTS, (0.2, -0.1))
    assert command.command_type is CommandType.POSITION
    assert command.values == (0.2, -0.1)
    with pytest.raises(ValueError, match="same length"):
        RobotCommand.velocity("cmd-2", JOINTS, (0.1,))
    with pytest.raises(ValueError, match="sequence of names"):
        RobotCommand.position("cmd-str", "joint_a", (0.0,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="finite"):
        RobotCommand.position("cmd-3", JOINTS, (math.inf, 0.0))
    with pytest.raises(ValueError, match="non-empty"):
        RobotCommand.position("", JOINTS, (0.0, 0.0))


def test_controller_dispatch_and_active_goal_cancel() -> None:
    controller = ProbeController()
    receipt = controller.dispatch(RobotCommand.position("cmd-1", JOINTS, (0.2, -0.1)))
    assert receipt.status is DispatchStatus.ACCEPTED
    assert controller.state.mode is ControllerMode.EXECUTING
    assert controller.state.active_command_id == "cmd-1"

    busy = controller.dispatch(RobotCommand.position("cmd-2", JOINTS, (0.0, 0.0)))
    assert busy.status is DispatchStatus.REJECTED
    assert busy.reason == "active_command"
    assert controller.cancel("cmd-1") is True
    assert controller.cancelled == ["cmd-1"]
    assert controller.state.mode is ControllerMode.IDLE
    assert controller.state.active_command_id is None
    assert controller.cancel("cmd-1") is False


def test_hold_then_resume_and_safe_stop_are_fail_closed() -> None:
    controller = ProbeController()
    controller.dispatch(RobotCommand.velocity("cmd-1", JOINTS, (0.1, 0.1)))
    assert controller.hold() is True
    assert controller.state.mode is ControllerMode.HOLDING
    assert controller.state.active_command_id == "cmd-1"
    assert controller.holds == 1
    assert controller.resume() is True
    assert controller.state.mode is ControllerMode.EXECUTING
    assert controller.cancel("cmd-1") is True
    resumed = controller.dispatch(RobotCommand.position("cmd-2", JOINTS, (0.0, 0.0)))
    assert resumed.status is DispatchStatus.ACCEPTED

    assert controller.safe_stop("operator request") is True
    assert controller.state.mode is ControllerMode.STOPPED
    assert controller.state.active_command_id is None
    rejected = controller.dispatch(RobotCommand.position("cmd-3", JOINTS, (0.0, 0.0)))
    assert rejected.status is DispatchStatus.REJECTED
    assert rejected.reason == "stopped"
    assert controller.reset() is True
    assert controller.state.mode is ControllerMode.IDLE


def test_controller_rejects_invalid_feedback_without_mutating_state() -> None:
    controller = ProbeController()
    before = controller.state
    with pytest.raises(ValueError):
        controller.update_state(RobotState(("other",), (0.0,)))
    assert controller.state == before


def test_feedback_cannot_clear_active_lifecycle() -> None:
    controller = ProbeController()
    controller.dispatch(RobotCommand.position("cmd-1", JOINTS, (0.0, 0.0)))
    with pytest.raises(ValueError, match="clear an active command"):
        controller.update_state(RobotState(JOINTS, (0.0, 0.0), sequence=1, mode=ControllerMode.IDLE))


def test_feedback_cannot_replace_active_command_identity() -> None:
    controller = ProbeController()
    controller.dispatch(RobotCommand.position("cmd-1", JOINTS, (0.0, 0.0)))
    conflicting = RobotState(
        JOINTS,
        (0.0, 0.0),
        sequence=1,
        mode=ControllerMode.EXECUTING,
        active_command_id="cmd-2",
    )
    with pytest.raises(ValueError, match="replace the active command"):
        controller.update_state(conflicting)


def test_feedback_sequence_must_advance() -> None:
    controller = ProbeController()
    with pytest.raises(ValueError, match="sequence must increase"):
        controller.update_state(RobotState(JOINTS, (0.0, 0.0), sequence=0))


def test_feedback_timestamp_cannot_move_backwards() -> None:
    controller = ProbeController()
    controller.update_state(RobotState(JOINTS, (0.0, 0.0), timestamp_ns=10, sequence=1))
    with pytest.raises(ValueError, match="timestamp must not move backwards"):
        controller.update_state(RobotState(JOINTS, (0.0, 0.0), timestamp_ns=9, sequence=2))


def test_robot_state_requires_complete_positions_and_consistent_mode() -> None:
    with pytest.raises(ValueError, match="positions must have"):
        RobotState(JOINTS, ())
    with pytest.raises(ValueError, match="requires exactly one"):
        RobotState(JOINTS, (0.0, 0.0), mode=ControllerMode.EXECUTING)


def test_controller_rejects_command_with_different_joint_schema() -> None:
    controller = ProbeController()
    receipt = controller.dispatch(RobotCommand.position("cmd-1", ("other", "joint_b"), (0.0, 0.0)))
    assert receipt.status is DispatchStatus.REJECTED
    assert receipt.reason == "joint_names"
    assert controller.dispatched == []


def test_backend_rejection_does_not_enter_executing() -> None:
    controller = RefusingController()
    receipt = controller.dispatch(RobotCommand.position("cmd-1", JOINTS, (0.0, 0.0)))
    assert receipt.status is DispatchStatus.REJECTED
    assert receipt.reason == "backend_unavailable"
    assert controller.state.mode is ControllerMode.IDLE
    assert controller.state.active_command_id is None


def test_lifecycle_commands_use_controller_safety_methods() -> None:
    controller = ProbeController()
    controller.dispatch(RobotCommand.position("motion", JOINTS, (0.0, 0.0)))
    hold_receipt = controller.dispatch(RobotCommand.hold("hold-1"))
    assert hold_receipt.status is DispatchStatus.ACCEPTED
    assert controller.state.mode is ControllerMode.HOLDING
    controller.dispatch(RobotCommand.position("motion-2", JOINTS, (0.0, 0.0)))
    stop_receipt = controller.dispatch(RobotCommand.stop("stop-1"))
    assert stop_receipt.status is DispatchStatus.ACCEPTED
    reset_receipt = controller.dispatch(RobotCommand.reset("reset-1"))
    assert reset_receipt.status is DispatchStatus.ACCEPTED
    assert controller.state.mode is ControllerMode.IDLE
