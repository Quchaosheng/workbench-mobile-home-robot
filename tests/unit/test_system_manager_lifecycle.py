"""Issue #101: application startup must be transactional.

A component graph that is half started keeps ports, threads and device handles
held while the manager reports a failure, and a retry re-enters the partial
graph. These tests pin the rollback, the reported state, the operator
diagnostics, and the idempotence of a repeated startup or shutdown.
"""

from __future__ import annotations

from workbench.application.system_manager_v2 import ComponentState, SystemManager


class FakeComponent:
    """A component that records the order of its lifecycle calls."""

    def __init__(
        self,
        name: str,
        *,
        start_ok: bool = True,
        events: list[str] | None = None,
        raise_on_start: bool = False,
        raise_on_stop: bool = False,
    ) -> None:
        self.name = name
        self.start_ok = start_ok
        self.raise_on_start = raise_on_start
        self.raise_on_stop = raise_on_stop
        self.events = events if events is not None else []
        self.start_calls = 0
        self.stop_calls = 0

    def startup(self) -> bool:
        self.start_calls += 1
        self.events.append(f"start:{self.name}")
        if self.raise_on_start:
            raise RuntimeError(f"{self.name} exploded")
        return self.start_ok

    def shutdown(self) -> None:
        self.stop_calls += 1
        self.events.append(f"stop:{self.name}")
        if self.raise_on_stop:
            raise RuntimeError(f"{self.name} refuses to stop")


def manager_with(*components: FakeComponent) -> SystemManager:
    manager = SystemManager()
    for component in components:
        assert manager.register(component.name, component) is True
    return manager


def test_first_component_failure_leaves_nothing_running() -> None:
    events: list[str] = []
    bad = FakeComponent("bad", start_ok=False, events=events)
    good = FakeComponent("good", events=events)
    manager = manager_with(bad, good)

    assert manager.startup() is False

    assert manager.state == "failed"
    assert manager.last_failed_component == "bad"
    assert manager.started_components == []
    assert manager.components["bad"]["state"] is ComponentState.STOPPED
    assert manager.components["good"]["state"] is ComponentState.CREATED
    # The failing component may have acquired resources before reporting failure,
    # so it is released too; the untouched one is never started.
    assert events == ["start:bad", "stop:bad"]
    assert good.start_calls == 0
    assert good.stop_calls == 0


def test_mid_sequence_failure_rolls_back_in_reverse_with_the_culprit_first() -> None:
    events: list[str] = []
    first = FakeComponent("first", events=events)
    second = FakeComponent("second", events=events)
    third = FakeComponent("third", start_ok=False, events=events)
    fourth = FakeComponent("fourth", events=events)
    manager = manager_with(first, second, third, fourth)

    assert manager.startup() is False

    assert manager.state == "failed"
    assert manager.last_failed_component == "third"
    assert manager.started_components == ["first", "second"]
    assert events == [
        "start:first",
        "start:second",
        "start:third",
        "stop:third",
        "stop:second",
        "stop:first",
    ]
    assert manager.components["first"]["state"] is ComponentState.STOPPED
    assert manager.components["second"]["state"] is ComponentState.STOPPED
    assert manager.components["third"]["state"] is ComponentState.STOPPED
    assert manager.components["fourth"]["state"] is ComponentState.CREATED
    assert fourth.start_calls == 0


def test_component_that_raises_during_startup_is_a_failure() -> None:
    events: list[str] = []
    good = FakeComponent("good", events=events)
    bad = FakeComponent("bad", events=events, raise_on_start=True)
    manager = manager_with(good, bad)

    assert manager.startup() is False
    assert manager.state == "failed"
    assert manager.last_failed_component == "bad"
    assert manager.components["good"]["state"] is ComponentState.STOPPED
    assert manager.components["bad"]["state"] is ComponentState.STOPPED
    assert good.stop_calls == 1


def test_rollback_failure_is_recorded_and_component_left_in_error() -> None:
    events: list[str] = []
    stubborn = FakeComponent("stubborn", events=events, raise_on_stop=True)
    bad = FakeComponent("bad", start_ok=False, events=events)
    manager = manager_with(stubborn, bad)

    assert manager.startup() is False

    # The manager must not claim a clean rollback when a stop failed.
    assert manager.state == "failed"
    assert manager.components["stubborn"]["state"] is ComponentState.ERROR
    assert manager.rollback_failures == [("stubborn", "stubborn refuses to stop")]
    assert manager.components["bad"]["state"] is ComponentState.STOPPED
    assert manager.last_failed_component == "bad"


def test_failed_startup_can_be_retried_after_the_cause_is_fixed() -> None:
    events: list[str] = []
    good = FakeComponent("good", events=events)
    flaky = FakeComponent("flaky", start_ok=False, events=events)
    manager = manager_with(good, flaky)

    assert manager.startup() is False
    assert manager.state == "failed"
    assert manager.rollback_failures == []

    flaky.start_ok = True
    assert manager.startup() is True

    assert manager.state == "running"
    assert manager.started_components == ["good", "flaky"]
    assert all(component["state"] is ComponentState.RUNNING for component in manager.components.values())
    # The retry re-started `good`, which the rollback had already stopped.
    assert good.start_calls == 2
    assert good.stop_calls == 1


def test_repeated_startup_does_not_double_start() -> None:
    good = FakeComponent("good")
    manager = manager_with(good)

    assert manager.startup() is True
    assert manager.startup() is True

    assert good.start_calls == 1
    assert manager.state == "running"


def test_repeated_shutdown_does_not_double_stop() -> None:
    good = FakeComponent("good")
    manager = manager_with(good)

    assert manager.startup() is True
    assert manager.shutdown() is True
    assert manager.shutdown() is True

    assert good.stop_calls == 1
    assert manager.state == "stopped"


def test_shutdown_failure_does_not_claim_a_stopped_system() -> None:
    stubborn = FakeComponent("stubborn", raise_on_stop=True)
    manager = manager_with(stubborn)

    assert manager.startup() is True
    assert manager.shutdown() is False

    assert manager.state == "failed"
    assert [name for name, _ in manager.rollback_failures] == ["stubborn"]


def test_shutdown_after_failed_startup_retries_the_unreleased_component() -> None:
    stubborn = FakeComponent("stubborn", raise_on_stop=True)
    bad = FakeComponent("bad", start_ok=False)
    manager = manager_with(stubborn, bad)

    assert manager.startup() is False
    assert manager.components["stubborn"]["state"] is ComponentState.ERROR

    # The failed release is retried, and now succeeds, so the system is stopped.
    stubborn.raise_on_stop = False
    assert manager.shutdown() is True

    assert manager.state == "stopped"
    assert manager.components["stubborn"]["state"] is ComponentState.STOPPED
    assert stubborn.stop_calls == 2


def test_shutdown_does_not_stop_a_component_that_never_started() -> None:
    events: list[str] = []
    good = FakeComponent("good", events=events)
    bad = FakeComponent("bad", start_ok=False, events=events)
    late = FakeComponent("late", events=events)
    manager = manager_with(good, bad, late)

    assert manager.startup() is False
    assert late.start_calls == 0

    assert manager.shutdown() is True
    assert late.stop_calls == 0
    assert manager.components["late"]["state"] is ComponentState.STOPPED
    assert manager.state == "stopped"


def test_registration_rejects_duplicate_names() -> None:
    first = FakeComponent("dup")
    second = FakeComponent("dup")
    manager = manager_with(first)

    assert manager.register("dup", second) is False
    assert manager.components["dup"]["instance"] is first


def test_component_without_lifecycle_hooks_is_still_tracked() -> None:
    manager = SystemManager()

    class Bare:
        pass

    assert manager.register("bare", Bare()) is True
    assert manager.startup() is True
    assert manager.components["bare"]["state"] is ComponentState.RUNNING
    assert manager.shutdown() is True
    assert manager.components["bare"]["state"] is ComponentState.STOPPED
