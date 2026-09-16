from __future__ import annotations

import pytest

from hardware.linux_drivers.pm import FakePowerManager, PMError, PMResource, PMState, PMTimeout


def manager() -> FakePowerManager:
    return FakePowerManager(
        (
            PMResource("irq", suspend_cost_s=0.1, resume_cost_s=0.2),
            PMResource("dma", suspend_cost_s=0.2, resume_cost_s=0.1),
            PMResource("bus", suspend_cost_s=0.1, resume_cost_s=0.2),
        )
    )


def test_suspend_and_resume_follow_dependency_order() -> None:
    power = manager()
    power.suspend(timeout_s=1.0)
    assert power.state is PMState.SUSPENDED
    assert power.suspended_resources == ("bus", "dma", "irq")
    power.resume(timeout_s=1.0)
    assert power.state is PMState.ACTIVE
    assert power.suspended_resources == ()


def test_suspend_timeout_fails_closed_without_claiming_suspended() -> None:
    power = manager()
    with pytest.raises(PMTimeout):
        power.suspend(timeout_s=0.25)
    assert power.state is PMState.FAULT
    assert power.suspended_resources == ("bus",)
    with pytest.raises(PMError, match="cannot suspend"):
        power.suspend()
    with pytest.raises(PMError, match="cannot resume"):
        power.resume()


def test_resume_timeout_fails_closed_and_close_is_idempotent() -> None:
    power = manager()
    power.suspend()
    with pytest.raises(PMTimeout):
        power.resume(timeout_s=0.25)
    assert power.state is PMState.FAULT
    power.close()
    assert power.state is PMState.CLOSED
    power.close()


def test_invalid_transitions_and_resource_definitions_are_rejected() -> None:
    with pytest.raises(PMError):
        PMResource("")
    with pytest.raises(PMError):
        FakePowerManager((PMResource("irq"), PMResource("irq")))
    power = manager()
    with pytest.raises(PMError):
        power.resume()
    with pytest.raises(PMError):
        power.suspend(timeout_s=0)
    power.close()
    with pytest.raises(PMError):
        power.suspend()
