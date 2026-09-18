"""Issue #77: every declared fault has an executable injector and a safe outcome.

The fault taxonomy in `tools/scripts/scenario_tools.py` and `sim/scenarios/` was
only ever consumed by the scripted log generator, so a declared fault proved
nothing about the runtime. These tests drive the shipped `ExecutionController`,
`PostActionLoop` and `VirtualMcu` through the real injectors, and assert the
safe behaviour the contract declares for each fault.

Injection is seeded and every fault is exercised on a fixed clock, so a failure
here is reproducible rather than a timing artifact.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/application"),
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/agent_runtime"),
    str(ROOT / "services/world_model"),
    str(ROOT / "firmware/virtual_mcu"),
    str(ROOT / "tools/scripts"),
]

from scenario_tools import P2_FAULT_TYPES
from workbench.application.fault_injection import (
    FAULT_SPECS,
    AdapterFaultInjector,
    FaultInjectionError,
    FaultType,
    ObservationFaultInjector,
    RuntimeFault,
    SafeBehavior,
    coverage_report,
    declared_faults,
    fault_spec,
    inject_link_loss,
    inject_process_restart,
)
from workbench_agent_runtime.execution_controller import ExecutionController
from workbench_agent_runtime.policy_validator import PolicyValidator
from workbench_agent_runtime.post_action import (
    GateOutcome,
    GateReasonCode,
    ObservationAttempt,
    ObservationSample,
    PostActionLoop,
    PostActionPolicy,
)
from workbench_contracts import (
    ActionOutcome,
    ActionResult,
    ActionType,
    ClockId,
    DeviceState,
    DispatchState,
    Observation,
    Orientation,
    Pose,
    Position,
    SemanticAction,
    TaskGraph,
    TaskStep,
)
from workbench_virtual_mcu import VirtualMcu

POLICY_CONFIG = {"policy_version": "fault-injection-v1", "high_impact_actions": frozenset()}

RUN_ID = "run-fault-077"
TASK_ID = "task-place-red-block"
GRASP_ACTION_ID = "a-grasp"
PLACE_ACTION_ID = "a-place"
COMPLETED_AT = "2026-09-18T12:00:01Z"
FRESH_AT = "2026-09-18T12:00:02Z"
STALE_AT = "2026-09-18T12:00:00Z"


def _action(action_id: str, action_type: ActionType = ActionType.GRASP) -> SemanticAction:
    parameters = {"destination_id": "tray"} if action_type is ActionType.PLACE else {}
    return SemanticAction(
        action_id=action_id,
        action_type=action_type,
        target_id="red_block",
        parameters=parameters,
    )


def _observation(observation_id: str = "obs-1", *, observed_at: str = FRESH_AT) -> Observation:
    return Observation(
        observation_id=observation_id,
        run_id=RUN_ID,
        entity_id="red_block",
        entity_type="block",
        pose=Pose(
            frame_id="base_link",
            position=Position(x=0.0, y=0.0, z=0.0),
            orientation=Orientation(x=0.0, y=0.0, z=0.0, w=1.0),
        ),
        confidence=0.95,
        observed_at=observed_at,
        clock_id=ClockId.WALL,
        source="camera",
        evidence_refs=[f"frame-{observation_id}"],
    )


class ScriptedAdapter:
    """A real adapter boundary: it returns a contract result, never a raw value."""

    def __init__(self, *, outcome: ActionOutcome = ActionOutcome.COMPLETED) -> None:
        self._outcome = outcome
        self.dispatched: list[str] = []

    def dispatch(self, action: SemanticAction) -> ActionResult:
        self.dispatched.append(action.action_id)
        return ActionResult(
            result_id=f"result-{action.action_id}",
            action_id=action.action_id,
            run_id=RUN_ID,
            outcome=self._outcome,
            dispatch_state=DispatchState.SENT,
            device_state=DeviceState.CONFIRMED if self._outcome is ActionOutcome.COMPLETED else DeviceState.REJECTED,
            started_at="2026-09-18T12:00:00Z",
            ended_at=COMPLETED_AT,
            evidence_refs=[f"mcu-frame-{action.action_id}"],
        )


class RealObserver:
    """A real observation port that returns a fresh sample for the asked entity."""

    def __init__(self, *, observed_at: str = FRESH_AT) -> None:
        self._observed_at = observed_at
        self.requests: list[tuple[str, str | None]] = []

    def observe(self, *, run_id, action_id, entity_id, not_before):
        self.requests.append((action_id, entity_id))
        sample = ObservationSample(
            action_id=action_id,
            observation=_observation(f"obs-{action_id}", observed_at=self._observed_at),
        )
        return ObservationAttempt.observed(sample)


class RecordingVerifier:
    """A World Model stand-in whose verdict is scripted, so the gate is under test."""

    def __init__(self, *statuses) -> None:
        from workbench_contracts import RecoveryHint, VerificationResult, VerificationStatus

        self._statuses = list(statuses) or [VerificationStatus.CONFIRMED]
        self._verification = lambda status: VerificationResult(
            verification_id="verification-1",
            run_id=RUN_ID,
            task_id=TASK_ID,
            claim="red_block is in tray",
            status=status,
            reason_code=None,
            evidence_refs=["frame-obs-1"],
            recovery_hint=RecoveryHint.NONE,
            verified_at=FRESH_AT,
            clock_id=ClockId.WALL,
            rule_version="fault-injection-test-v1",
        )
        self.calls: list[str] = []

    def reduce(self, observations, *, run_id):
        self.calls.append("reduce")
        return {"run_id": run_id, "observations": tuple(observations)}

    def verify(self, state, *, run_id, action_id):
        self.calls.append("verify")
        status = self._statuses.pop(0) if self._statuses else self._statuses[-1]
        return self._verification(status)


def _loop(*, observer, verifier, adapter) -> PostActionLoop:
    controller = ExecutionController(
        policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
        adapter=adapter,
    )
    return PostActionLoop(
        controller=controller,
        observer=observer,
        verifier=verifier,
        policy=PostActionPolicy(max_observation_attempts=2),
    )


def _graph(*steps: TaskStep) -> TaskGraph:
    return TaskGraph(
        task_id=TASK_ID,
        goal="place the red block in the tray",
        steps=list(steps),
        planner="fault-injection",
        model_route="template",
    )


def _grasp_step() -> TaskStep:
    return TaskStep(step_id="s1", action=_action(GRASP_ACTION_ID), depends_on=[])


# --- Taxonomy -----------------------------------------------------------------


def test_every_manifest_fault_has_a_spec_and_an_injector() -> None:
    """The manifest enum and the executable taxonomy must not drift apart."""
    assert declared_faults() >= P2_FAULT_TYPES
    for name in sorted(P2_FAULT_TYPES):
        spec = fault_spec(name)
        assert spec.trigger.strip()
        assert spec.evidence_requirement.strip()
        assert isinstance(spec.expected_safe_behaviour, SafeBehavior)


def test_a_runtime_fault_is_declared_but_never_a_manifest_fault() -> None:
    """A lost link and a restart belong to the runtime, not to a scenario file."""
    for runtime_fault in (RuntimeFault.LINK_LOSS, RuntimeFault.PROCESS_RESTART):
        assert runtime_fault.value not in P2_FAULT_TYPES
        assert fault_spec(runtime_fault).declared_by


def test_every_spec_names_a_distinct_safe_behaviour_or_a_distinct_trigger() -> None:
    seen: set[tuple[str, str]] = set()
    for spec in FAULT_SPECS:
        key = (spec.fault.value, spec.trigger)
        assert key not in seen, f"duplicate fault specification: {key}"
        seen.add(key)


def test_an_undeclared_fault_is_refused() -> None:
    with pytest.raises(FaultInjectionError):
        fault_spec("earthquake")
    with pytest.raises(FaultInjectionError):
        AdapterFaultInjector(ScriptedAdapter(), fault="earthquake")
    with pytest.raises(FaultInjectionError):
        ObservationFaultInjector(RealObserver(), fault="earthquake")


# --- Actuator timeout ---------------------------------------------------------


def test_actuator_timeout_is_bounded_and_reaches_a_terminal_state() -> None:
    adapter = AdapterFaultInjector(ScriptedAdapter(), fault=FaultType.ACTUATOR_TIMEOUT, seed=7)
    loop = _loop(observer=RealObserver(), verifier=RecordingVerifier(), adapter=adapter)

    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    assert report.terminal is GateOutcome.INSUFFICIENT_EVIDENCE
    step = report.step("s1")
    assert step is not None
    assert step.reason_code is GateReasonCode.ACTION_NOT_COMPLETED
    assert step.verification is None
    assert adapter.injected == [GRASP_ACTION_ID]
    assert adapter.dispatch_count == 1, "a timeout must not be retried into a completion"


def test_actuator_timeout_never_succeeds_even_with_a_confirming_verifier() -> None:
    """The strongest form of the claim: a broken adapter cannot be verified away."""
    adapter = AdapterFaultInjector(ScriptedAdapter(), fault=FaultType.ACTUATOR_TIMEOUT, seed=3)
    verifier = RecordingVerifier()
    loop = _loop(observer=RealObserver(), verifier=verifier, adapter=adapter)

    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    assert not report.may_advance
    assert verifier.calls == [], "a failed dispatch must not reach the verifier at all"


def test_actuator_timeout_is_reproducible_for_one_seed() -> None:
    def injected_ids() -> list[str]:
        adapter = AdapterFaultInjector(ScriptedAdapter(), fault=FaultType.ACTUATOR_TIMEOUT, seed=11)
        for _ in range(3):
            with pytest.raises(TimeoutError):
                adapter.dispatch(_action(GRASP_ACTION_ID))
        return adapter.injected

    first, second = injected_ids(), injected_ids()
    assert first == second


# --- Grasp failure ------------------------------------------------------------


def test_grasp_failure_is_refused_without_fresh_evidence() -> None:
    adapter = AdapterFaultInjector(ScriptedAdapter(), fault=FaultType.GRASP_FAILURE, seed=5)
    loop = _loop(observer=RealObserver(), verifier=RecordingVerifier(), adapter=adapter)

    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    assert report.terminal is GateOutcome.INSUFFICIENT_EVIDENCE
    assert adapter.injected == [GRASP_ACTION_ID]
    # The adapter reported a failure, so the produced result must be a failure.
    assert adapter.dispatch_count == 1


def test_grasp_failure_does_not_double_inject_an_already_failed_result() -> None:
    adapter = AdapterFaultInjector(ScriptedAdapter(outcome=ActionOutcome.FAILED), fault=FaultType.GRASP_FAILURE, seed=1)
    result = adapter.dispatch(_action(GRASP_ACTION_ID))
    assert result.outcome is ActionOutcome.FAILED


def test_adapter_injector_refuses_a_perception_fault() -> None:
    with pytest.raises(FaultInjectionError):
        AdapterFaultInjector(ScriptedAdapter(), fault=FaultType.CAMERA_DROPOUT)
    with pytest.raises(FaultInjectionError):
        ObservationFaultInjector(RealObserver(), fault=FaultType.ACTUATOR_TIMEOUT)


def test_skip_first_preserves_a_real_dispatch_before_injection() -> None:
    adapter = AdapterFaultInjector(ScriptedAdapter(), fault=FaultType.ACTUATOR_TIMEOUT, seed=2, skip_first=1)
    adapter.dispatch(_action("a-first"))
    with pytest.raises(TimeoutError):
        adapter.dispatch(_action("a-second"))
    assert adapter.injected == ["a-second"]


# --- Camera dropout and occlusion --------------------------------------------


def test_camera_dropout_cannot_confirm_without_fresh_evidence() -> None:
    injector = ObservationFaultInjector(RealObserver(), fault=FaultType.CAMERA_DROPOUT, seed=4)
    loop = _loop(observer=injector, verifier=RecordingVerifier(), adapter=ScriptedAdapter())

    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    assert report.terminal is GateOutcome.INSUFFICIENT_EVIDENCE
    step = report.step("s1")
    assert step is not None
    assert step.reason_code is GateReasonCode.PERCEPTION_FAILURE
    assert step.observations == ()
    assert not step.confirmed


def test_camera_dropout_fails_every_entity_but_occlusion_only_the_named_one() -> None:
    dropout = ObservationFaultInjector(RealObserver(), fault=FaultType.CAMERA_DROPOUT, seed=1)

    def outcome(injector: ObservationFaultInjector, entity_id: str) -> str:
        return injector.observe(
            run_id=RUN_ID, action_id=GRASP_ACTION_ID, entity_id=entity_id, not_before=None
        ).outcome.value

    assert outcome(dropout, "red_block") == "perception_failure"
    assert outcome(dropout, "tray") == "perception_failure"

    occlusion = ObservationFaultInjector(RealObserver(), fault=FaultType.OCCLUSION, seed=1, entity_id="red_block")
    assert outcome(occlusion, "red_block") == "missing"
    # An occlusion must not blind the whole camera.
    assert outcome(occlusion, "tray") == "observed"


def test_occlusion_requires_the_entity_it_blocks() -> None:
    with pytest.raises(FaultInjectionError):
        ObservationFaultInjector(RealObserver(), fault=FaultType.OCCLUSION)


# --- Stale observation --------------------------------------------------------


def test_a_stale_observation_is_refused_before_a_dependent_step_runs() -> None:
    injector = ObservationFaultInjector(RealObserver(), fault=FaultType.STALE_OBSERVATION, seed=6)
    verifier = RecordingVerifier()
    loop = _loop(observer=injector, verifier=verifier, adapter=ScriptedAdapter())

    graph = _graph(
        _grasp_step(),
        TaskStep(step_id="s2", action=_action(PLACE_ACTION_ID, ActionType.PLACE), depends_on=["s1"]),
    )
    report = loop.run(graph, run_id=RUN_ID)
    step = report.step("s1")
    assert step is not None
    assert step.reason_code is GateReasonCode.OBSERVATION_STALE
    assert step.observations == ()
    assert verifier.calls == [], "stale evidence must be refused before reduction"
    dependent = report.step("s2")
    assert dependent is not None
    assert dependent.reason_code is GateReasonCode.DEPENDENCY_BLOCKED
    assert not dependent.allows_dependents
    assert not report.may_advance


def test_stale_injection_ages_a_real_sample_instead_of_inventing_one() -> None:
    stale = ObservationFaultInjector(RealObserver(), fault=FaultType.STALE_OBSERVATION, seed=1)
    not_before = datetime(2026, 9, 18, 12, 0, 1, tzinfo=UTC)
    attempt = stale.observe(run_id=RUN_ID, action_id=GRASP_ACTION_ID, entity_id="red_block", not_before=not_before)
    assert attempt.outcome.value == "observed"
    # The sample still carries the producer's evidence; only freshness changed.
    # Same instant as the action's completion, expressed on the caller's clock.
    assert datetime.fromisoformat(attempt.sample.observation.observed_at.replace("Z", "+00:00")) == not_before
    assert attempt.sample.observation.evidence_refs == ["frame-obs-a-grasp"]


def test_stale_injection_reports_a_miss_when_there_is_nothing_to_age() -> None:
    class SilentPort:
        def observe(self, *, run_id, action_id, entity_id, not_before):
            return ObservationAttempt.missing("camera produced nothing")

    stale = ObservationFaultInjector(SilentPort(), fault=FaultType.STALE_OBSERVATION, seed=1)
    attempt = stale.observe(run_id=RUN_ID, action_id=GRASP_ACTION_ID, entity_id="red_block", not_before=None)
    assert attempt.outcome.value == "missing"


# --- Moving target ------------------------------------------------------------


def test_a_moving_target_keeps_a_refuted_verification_with_its_evidence() -> None:
    from workbench_contracts import VerificationStatus

    verifier = RecordingVerifier(VerificationStatus.REFUTED)
    loop = _loop(observer=RealObserver(), verifier=verifier, adapter=ScriptedAdapter())

    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    step = report.step("s1")
    assert step is not None
    assert step.outcome is GateOutcome.REFUTED
    assert step.verification is not None
    assert step.verification.status is VerificationStatus.REFUTED
    assert step.verification.evidence_refs == ["frame-obs-1"]
    assert not report.may_advance


# --- Runtime faults -----------------------------------------------------------


def test_link_loss_reaches_the_mcu_safe_state() -> None:
    mcu = VirtualMcu()
    mcu.command("execute")
    fault_code = inject_link_loss(mcu)
    assert fault_code == "fault"
    assert mcu.state.value == "fault"
    assert mcu.fault_code == "WATCHDOG_TIMEOUT"
    # Only an explicit reset clears a fault; nothing else may quietly recover it.
    assert mcu.command("execute").rejected
    assert mcu.command("reset").accepted
    assert mcu.state.value == "idle"
    assert mcu.fault_code is None


def test_link_loss_requires_a_real_mcu_boundary() -> None:
    with pytest.raises(FaultInjectionError):
        inject_link_loss(object())


def test_a_process_restart_invalidates_the_run_correlation() -> None:
    loop = _loop(observer=RealObserver(), verifier=RecordingVerifier(), adapter=ScriptedAdapter())
    inject_process_restart(loop, run_id=RUN_ID)
    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    step = report.step("s1")
    assert step is not None
    assert step.reason_code is GateReasonCode.RESTART_INVALIDATED_RUN
    assert not step.confirmed


def test_a_restart_for_another_run_does_not_invalidate_this_one() -> None:
    loop = _loop(observer=RealObserver(), verifier=RecordingVerifier(), adapter=ScriptedAdapter())
    inject_process_restart(loop, run_id="some-other-run")
    report = loop.run(_graph(_grasp_step()), run_id=RUN_ID)
    assert report.may_advance


def test_restart_injection_requires_the_loop_boundary() -> None:
    with pytest.raises(FaultInjectionError):
        inject_process_restart(object(), run_id=RUN_ID)


# --- Coverage reporting -------------------------------------------------------


def test_coverage_never_reports_a_fault_as_passing_by_default() -> None:
    report = coverage_report()
    assert report["declared_count"] == len(FAULT_SPECS)
    assert report["not_executed_count"] == len(FAULT_SPECS)
    assert not any(entry["status"] != "NOT_EXECUTED" for entry in report["faults"])


def test_coverage_marks_a_simulated_fault_and_leaves_the_rest_unexecuted() -> None:
    report = coverage_report(simulated=["occlusion"])
    statuses = {entry["fault"]: entry["status"] for entry in report["faults"]}
    assert statuses["occlusion"] == "SIMULATED"
    assert all(status == "NOT_EXECUTED" for name, status in statuses.items() if name != "occlusion")
    assert report["simulated_count"] == 1


def test_a_hardware_claim_without_physical_evidence_is_refused() -> None:
    """The cheapest way to overstate coverage is to assert it, so it is refused."""
    with pytest.raises(FaultInjectionError):
        coverage_report(hardware_tested=["occlusion"])
    report = coverage_report(
        hardware_tested=["occlusion"],
        physical_evidence={"occlusion": "runs/hardware/val5-07.json"},
    )
    entry = next(item for item in report["faults"] if item["fault"] == "occlusion")
    assert entry["status"] == "HARDWARE_TESTED"
    assert entry["physical_evidence_ref"] == "runs/hardware/val5-07.json"


def test_coverage_refuses_an_undeclared_fault_name() -> None:
    with pytest.raises(FaultInjectionError):
        coverage_report(simulated=["earthquake"])


def test_coverage_document_is_json_serializable_and_versioned() -> None:
    import json

    report = coverage_report(simulated=sorted(P2_FAULT_TYPES))
    encoded = json.dumps(report, allow_nan=False)
    assert "fault-injection-v1" in encoded
    assert report["coverage_statuses"] == ["SIMULATED", "HARDWARE_TESTED", "NOT_EXECUTED"]
    # Every declared fault appears exactly once.
    assert len({entry["fault"] for entry in report["faults"]}) == report["declared_count"]
