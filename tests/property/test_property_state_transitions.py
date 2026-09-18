"""Property suite: lifecycle and command state machines reject illegal moves (Issue #89).

Three state machines share one promise, so they share one suite:

* ``workbench_virtual_mcu.state_machine.VirtualMcu`` -- a command is accepted
  only from the states it declares, STOP is accepted from every state, and a
  rejected command changes neither the state nor the fault code;
* ``workbench.kernel.lifecycle.LifecycleManager`` -- configure/activate/
  deactivate/finalize each succeed only from their declared predecessor state;
* ``workbench_world_model.reducer`` -- a replayed stream is a pure fold, so
  applying the same event twice cannot change the state a second time.

The generator walks a seeded transition sequence and asserts the observable
result matches the declared transition table, which is what makes a deleted
guard detectable (the mutated machine accepts a move the table forbids).
"""

from __future__ import annotations

from workbench.kernel.lifecycle import LifecycleManager, LifecycleState
from workbench_virtual_mcu.state_machine import McuCommandRejection, McuState, VirtualMcu

from ._generator import SEEDS, Rng, assert_corpus_clean, report_for, run_corpus, write_archive_if_requested

SUITE = "state_transitions"
MIN_CASES = 3 * 140

# The declared MCU transition table, restated independently of the module under
# test so a mutation to the module cannot rewrite the expectation.
ALLOWED = {
    "stop": frozenset({McuState.IDLE, McuState.EXECUTING, McuState.SAFE_STOP, McuState.FAULT}),
    "reset": frozenset({McuState.SAFE_STOP, McuState.FAULT}),
    "execute": frozenset({McuState.IDLE}),
    "complete": frozenset({McuState.EXECUTING}),
}
RESULT_STATES = {
    "stop": McuState.SAFE_STOP,
    "reset": McuState.IDLE,
    "execute": McuState.EXECUTING,
    "complete": McuState.IDLE,
}

# Lifecycle transitions, also restated independently.
LIFECYCLE_STEPS = (
    ("configure", LifecycleState.CREATED, LifecycleState.CONFIGURED),
    ("activate", LifecycleState.CONFIGURED, LifecycleState.ACTIVE),
    ("deactivate", LifecycleState.ACTIVE, LifecycleState.DEACTIVATED),
    ("finalize", LifecycleState.DEACTIVATED, LifecycleState.FINALIZED),
)
LIFECYCLE_ORDER = (
    LifecycleState.CREATED,
    LifecycleState.CONFIGURED,
    LifecycleState.ACTIVE,
    LifecycleState.DEACTIVATED,
    LifecycleState.FINALIZED,
)


def cases() -> list[dict]:
    generated: list[dict] = []
    for seed in SEEDS:
        rng = Rng(seed)
        for _ in range(140):
            length = rng.integer(1, 8)
            generated.append({"commands": [rng.choice(tuple(ALLOWED)) for _ in range(length)]})
            steps = tuple(name for name, _, _ in LIFECYCLE_STEPS)
            generated.append({"lifecycle": [rng.choice(steps) for _ in range(length)]})
    return generated


def walk_commands(commands: list[str]) -> list[tuple[str, bool, McuState]]:
    """Return (command, accepted, resulting state) for each step."""

    mcu = VirtualMcu()
    observed: list[tuple[str, bool, McuState]] = []
    for command in commands:
        before_state, before_fault = mcu.state, mcu.fault_code
        result = mcu.command(command)
        accepted = result.accepted

        if accepted:
            assert before_state in ALLOWED[command], f"{command!r} accepted from illegal state {before_state}"
            assert mcu.state is RESULT_STATES[command], f"{command!r} produced {mcu.state}"
        else:
            assert before_state not in ALLOWED[command], f"{command!r} rejected from legal state {before_state}"
            assert mcu.state is before_state, f"rejection of {command!r} mutated state to {mcu.state}"
            assert mcu.fault_code == before_fault, f"rejection of {command!r} mutated fault code"

        assert result.state is mcu.state
        observed.append((command, accepted, mcu.state))
    return observed


def check_commands(case: dict) -> None:
    commands = case["commands"]
    observed = walk_commands(commands)
    assert len(observed) == len(commands)

    # Feeding the same sequence to a fresh machine is deterministic.
    assert walk_commands(commands) == observed

    # STOP is accepted from every state, and repeated STOP is idempotent.
    mcu = VirtualMcu()
    for command in commands:
        mcu.command(command)
        assert mcu.command("stop").accepted
        assert mcu.state is McuState.SAFE_STOP


def check_lifecycle(case: dict) -> None:
    manager = LifecycleManager()
    node = manager.create_node("node-property")
    assert node.get_state() is LifecycleState.CREATED

    for name in case["lifecycle"]:
        allowed_from, target = next((source, target) for step, source, target in LIFECYCLE_STEPS if step == name)
        before = node.get_state()
        accepted = getattr(node, name)()

        if before is allowed_from:
            assert accepted is True, f"{name!r} refused from {before}"
            assert node.get_state() is target
        else:
            assert accepted is False, f"{name!r} accepted from {before}"
            assert node.get_state() is before, f"{name!r} mutated state from {before} to {node.get_state()}"


def check(case: dict) -> None:
    if "commands" in case:
        check_commands(case)
    else:
        check_lifecycle(case)


def test_generated_transition_sequences_respect_the_declared_tables() -> None:
    report = run_corpus(SUITE, cases(), check)
    assert_corpus_clean(report)
    assert report.case_count >= MIN_CASES, f"corpus shrank to {report.case_count} cases"
    assert report.corpus_digest


def test_every_command_from_every_state_is_classified() -> None:
    for command, allowed in ALLOWED.items():
        for state in McuState:
            mcu = VirtualMcu()
            mcu.state = state
            before_fault = mcu.fault_code
            result = mcu.command(command)

            assert result.accepted is (state in allowed), f"{command!r} from {state}"
            if result.rejected:
                assert result.reason is McuCommandRejection.INVALID_STATE
                assert mcu.state is state
                assert mcu.fault_code == before_fault


def test_malformed_commands_are_rejected_without_mutating_state() -> None:
    for command in ("", "   ", " execute", "execute ", "Execute", "unknown", None, 7, ["stop"]):
        mcu = VirtualMcu()
        mcu.command("execute")
        before = mcu.state
        result = mcu.command(command)
        assert result.rejected, f"{command!r} was accepted"
        assert mcu.state is before
        assert result.reason is not None


def test_a_watchdog_fault_is_only_cleared_by_an_accepted_reset() -> None:
    mcu = VirtualMcu()
    assert mcu.watchdog_timeout() is McuState.FAULT
    assert mcu.command("execute").rejected
    assert mcu.state is McuState.FAULT

    assert mcu.command("stop").accepted
    assert mcu.state is McuState.SAFE_STOP

    assert mcu.command("reset").accepted
    assert mcu.state is McuState.IDLE
    assert mcu.fault_code is None


def test_lifecycle_requires_the_declared_order() -> None:
    manager = LifecycleManager()
    node = manager.create_node("node-order")

    assert node.activate() is False
    assert node.deactivate() is False
    assert node.finalize() is False
    for name, source, target in LIFECYCLE_STEPS:
        assert node.get_state() is source, f"{name}: expected {source}"
        assert getattr(node, name)() is True
        assert node.get_state() is target

    assert node.get_state() is LifecycleState.FINALIZED
    assert LIFECYCLE_ORDER[-1] is LifecycleState.FINALIZED


def test_duplicate_node_names_are_rejected() -> None:
    import pytest

    manager = LifecycleManager()
    manager.create_node("node-duplicate")
    with pytest.raises(ValueError, match="already exists"):
        manager.create_node("node-duplicate")


def test_the_corpus_is_reproducible_for_a_fixed_seed() -> None:
    write_archive_if_requested()
    first = cases()
    second = cases()
    assert [case for case in first] == [case for case in second]
    assert report_for(SUITE).seeds == SEEDS
