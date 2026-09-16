from dataclasses import replace

import pytest
from test_motion_safety import CONTEXT, NAMES, state
from workbench_motion.controller import Controller
from workbench_motion.motion_types import CommandMode, ReceiptStatus, RobotCommand
from workbench_motion.reference_controller import InMemoryController


def controller():
    result = InMemoryController("test", NAMES, preflight_context=CONTEXT, max_state_age_s=0.2)
    result.update_state(state())
    return result


@pytest.mark.parametrize(
    "mode, values, accepted",
    [
        (CommandMode.POSITION, (0.1, 0), True),
        (CommandMode.POSITION, (3, 0), False),
        (CommandMode.VELOCITY, (0.1, 0), True),
        (CommandMode.VELOCITY, (0.4, 0), False),
    ],
)
def test_reference_admission_uses_existing_effective_limits_without_dispatch(mode, values, accepted):
    instance = controller()
    assert isinstance(instance, Controller)
    receipt = instance.submit_command(RobotCommand("one", "test", NAMES, mode, values, 1), now_s=1)
    assert (receipt.status is ReceiptStatus.ACCEPTED) is accepted
    assert not receipt.dispatch_attempted


def test_reference_rejects_stale_feedback_and_backwards_clock():
    instance = controller()
    assert instance.get_state(now_s=1.3) is None
    with pytest.raises(ValueError):
        instance.update_state(replace(state(), observed_at_s=0.9))
