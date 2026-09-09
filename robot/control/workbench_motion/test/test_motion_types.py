from __future__ import annotations

import math

import pytest
from workbench_motion.motion_types import CommandMode, RobotCommand, RobotState


def test_state_normalizes_joint_values_by_the_declared_order():
    state = RobotState(
        robot_id="ur5e-left",
        joint_names=("shoulder", "elbow"),
        positions=(0, 1.25),
        velocities=(0, -0.5),
        observed_at_s=12.0,
    )

    assert state.position_by_joint == {"shoulder": 0.0, "elbow": 1.25}
    assert state.velocity_by_joint == {"shoulder": 0.0, "elbow": -0.5}


def test_command_is_immutable_and_uses_an_explicit_command_mode():
    command = RobotCommand(
        command_id="cmd-1",
        robot_id="ur5e-left",
        joint_names=("shoulder",),
        mode=CommandMode.POSITION,
        values=(0.25,),
        issued_at_s=12.0,
    )

    assert command.values == (0.25,)
    with pytest.raises(AttributeError):
        command.values = (0.5,)  # type: ignore[misc]


@pytest.mark.parametrize(
    "build",
    [
        lambda: RobotState("", ("j1",), (0.0,), (0.0,), 1.0),
        lambda: RobotState("arm", ("j1", "j1"), (0.0, 0.0), (0.0, 0.0), 1.0),
        lambda: RobotState("arm", ("j1",), (0.0,), (), 1.0),
        lambda: RobotState("arm", ("j1",), (math.nan,), (0.0,), 1.0),
        lambda: RobotState("arm", ("j1",), (0.0,), (0.0,), math.inf),
        lambda: RobotCommand("", "arm", ("j1",), CommandMode.POSITION, (0.0,), 1.0),
        lambda: RobotCommand("cmd", "arm", ("j1",), "position", (0.0,), 1.0),
        lambda: RobotCommand("cmd", "arm", ("j1",), CommandMode.VELOCITY, (math.inf,), 1.0),
    ],
)
def test_invalid_identity_joint_schema_or_numeric_data_fails_closed(build):
    with pytest.raises((TypeError, ValueError)):
        build()
