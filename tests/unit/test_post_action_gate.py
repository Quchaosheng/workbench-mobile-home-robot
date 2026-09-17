"""Post-action observe/reduce/verify gate for Issue #151.

The fakes here are deliberate: the loop's whole claim is that an adapter saying
``completed`` is not evidence of a physical change. A fake observer whose answers
the test controls is the only way to prove that the gate, not the adapter, is
what releases a dependent step.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/agent_runtime"),
]

from workbench_agent_runtime.execution_controller import (
    ExecutionController,
)
from workbench_agent_runtime.policy_validator import PolicyValidator
from workbench_agent_runtime.post_action import (
    POST_ACTION_LOOP_VERSION,
    STATE_CHANGING_ACTIONS,
    GateOutcome,
    GateReasonCode,
    ObservationAttempt,
    ObservationAttemptOutcome,
    ObservationSample,
    PostActionError,
    PostActionLoop,
    PostActionPolicy,
    build_post_action_loop,
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
    ReasonCode,
    RecoveryHint,
    SemanticAction,
    TaskGraph,
    TaskStep,
    VerificationResult,
    VerificationStatus,
)

POLICY_CONFIG = {"policy_version": "post-action-gate-test-v1", "high_impact_actions": frozenset()}

COMPLETED_AT = "2026-09-17T12:00:01Z"
FRESH_AT = "2026-09-17T12:00:02Z"
STALE_AT = "2026-09-17T12:00:00Z"


def action(
    action_id: str,
    action_type: ActionType,
    *,
    target_id: str | None = None,
    parameters: dict | None = None,
) -> SemanticAction:
    # PLACE requires destination_id by the tool schema; a fixture without it is
    # rejected by policy before the gate is ever reached.
    resolved = dict(parameters or {})
    if action_type is ActionType.PLACE and "destination_id" not in resolved:
        resolved["destination_id"] = "tray"
    return SemanticAction(
        action_id=action_id,
        action_type=action_type,
        target_id=target_id,
        parameters=resolved,
    )


def observation(
    *,
    observation_id: str = "obs-1",
    run_id: str = "run-1",
    entity_id: str = "red_block",
    observed_at: str = FRESH_AT,
) -> Observation:
    return Observation(
        observation_id=observation_id,
        run_id=run_id,
        entity_id=entity_id,
        entity_type="block",
        pose=Pose(
            frame_id="base_link",
            position=Position(x=0.0, y=0.0, z=0.0),
            orientation=Orientation(x=0.0, y=0.0, z=0.0, w=1.0),
        ),
        confidence=0.9,
        observed_at=observed_at,
        clock_id=ClockId.WALL,
        source="camera",
        evidence_refs=["frame-1"],
    )


def result_for(
    action_id: str,
    *,
    outcome: ActionOutcome = ActionOutcome.COMPLETED,
    run_id: str = "run-1",
    ended_at: str = COMPLETED_AT,
) -> ActionResult:
    return ActionResult(
        result_id=f"result-{action_id}",
        action_id=action_id,
        run_id=run_id,
        outcome=outcome,
        dispatch_state=DispatchState.SENT,
        device_state=DeviceState.CONFIRMED if outcome is ActionOutcome.COMPLETED else DeviceState.REJECTED,
        started_at="2026-09-17T12:00:00Z",
        ended_at=ended_at,
    )


class ScriptedAdapter:
    """An adapter that always reports completed; the gate must not believe it."""

    def __init__(self, *, ended_at: str = COMPLETED_AT) -> None:
        self.dispatched: list[str] = []
        self._ended_at = ended_at

    def dispatch(self, semantic_action: SemanticAction) -> ActionResult:
        self.dispatched.append(semantic_action.action_id)
        return result_for(semantic_action.action_id, ended_at=self._ended_at)


class FailingAdapter:
    def __init__(self, error: Exception) -> None:
        self._error = error

    def dispatch(self, semantic_action: SemanticAction) -> ActionResult:
        raise self._error


class ScriptedObserver:
    """Returns queued attempts in order so a test can script the timeline."""

    def __init__(self, *attempts: ObservationAttempt) -> None:
        self._attempts = list(attempts)
        self.requests: list[tuple[str, str, str | None, object]] = []

    def observe(self, *, run_id, action_id, entity_id, not_before):
        self.requests.append((run_id, action_id, entity_id, not_before))
        if not self._attempts:
            return ObservationAttempt.missing("observer script exhausted")
        return self._attempts.pop(0)


class RecordingVerifier:
    """A World Model stand-in that records call order and returns scripted results."""

    def __init__(self, *statuses: VerificationStatus) -> None:
        self._statuses = list(statuses)
        self.calls: list[str] = []
        self.reduced_observations: list[tuple[Observation, ...]] = []

    def reduce(self, observations, *, run_id):
        self.calls.append("reduce")
        self.reduced_observations.append(tuple(observations))
        return {"run_id": run_id, "count": len(tuple(observations))}

    def verify(self, state, *, run_id, action_id):
        self.calls.append("verify")
        status = self._statuses.pop(0) if self._statuses else VerificationStatus.INSUFFICIENT_EVIDENCE
        reason = ReasonCode.GOAL_SATISFIED if status is VerificationStatus.CONFIRMED else None
        return VerificationResult(
            verification_id=f"verification-{action_id}",
            run_id=run_id,
            task_id="task-1",
            claim=f"{action_id} changed the world",
            status=status,
            reason_code=reason,
            completeness=1.0 if status is VerificationStatus.CONFIRMED else None,
            evidence_refs=["frame-1"],
            recovery_hint=RecoveryHint.NONE if status is VerificationStatus.CONFIRMED else RecoveryHint.RE_OBSERVE,
            verified_at=FRESH_AT,
            clock_id=ClockId.WALL,
            rule_version="test-rule-v1",
        )


class ExplodingVerifier:
    def __init__(self, *, fail_on: str) -> None:
        self._fail_on = fail_on

    def reduce(self, observations, *, run_id):
        if self._fail_on == "reduce":
            raise RuntimeError("reducer unavailable")
        return object()

    def verify(self, state, *, run_id, action_id):
        if self._fail_on == "verify":
            raise RuntimeError("verifier unavailable")
        return "not-a-verification-result"


def graph(*steps: TaskStep) -> TaskGraph:
    return TaskGraph(
        task_id="task-1",
        goal="gate a physical action",
        steps=list(steps),
        planner="test",
        model_route="template",
    )


def step(
    step_id: str,
    semantic_action: SemanticAction,
    *,
    depends_on: tuple[str, ...] = (),
) -> TaskStep:
    return TaskStep(step_id=step_id, action=semantic_action, depends_on=list(depends_on))


def make_loop(
    *,
    observer: ScriptedObserver,
    verifier: object,
    adapter: object | None = None,
    policy: PostActionPolicy | None = None,
) -> PostActionLoop:
    controller = ExecutionController(
        policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
        adapter=adapter if adapter is not None else ScriptedAdapter(),
    )
    return PostActionLoop(controller=controller, observer=observer, verifier=verifier, policy=policy)


class ConstantTests(unittest.TestCase):
    def test_every_state_changing_action_is_gated(self) -> None:
        for action_type in (
            ActionType.GRASP,
            ActionType.PLACE,
            ActionType.NAVIGATE,
            ActionType.OPEN,
            ActionType.CLOSE,
        ):
            self.assertIn(action_type, STATE_CHANGING_ACTIONS)
        # Perception-free actions must not be gated, or an observe step would
        # need an observation of itself.
        for action_type in (ActionType.OBSERVE, ActionType.ASK_CONFIRM, ActionType.EXPRESS):
            self.assertNotIn(action_type, STATE_CHANGING_ACTIONS)


class ConfigurationTests(unittest.TestCase):
    def test_ports_and_policy_are_validated(self) -> None:
        controller = ExecutionController(
            policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
            adapter=ScriptedAdapter(),
        )
        verifier = RecordingVerifier()
        with self.assertRaises(PostActionError):
            PostActionLoop(controller="not-a-controller", observer=ScriptedObserver(), verifier=verifier)
        with self.assertRaises(PostActionError):
            PostActionLoop(controller=controller, observer=object(), verifier=verifier)
        with self.assertRaises(PostActionError):
            PostActionLoop(controller=controller, observer=ScriptedObserver(), verifier=object())
        with self.assertRaises(PostActionError):
            PostActionLoop(
                controller=controller,
                observer=ScriptedObserver(),
                verifier=verifier,
                policy="not-a-policy",
            )

    def test_bounded_retry_policy_is_enforced(self) -> None:
        for invalid in (0, -1, 9, True, 1.5, "2"):
            with self.subTest(value=invalid), self.assertRaises(PostActionError):
                PostActionPolicy(max_observation_attempts=invalid)
        self.assertEqual(PostActionPolicy(max_observation_attempts=8).max_observation_attempts, 8)

    def test_attempt_construction_is_fail_closed(self) -> None:
        with self.assertRaises(PostActionError):
            ObservationAttempt(ObservationAttemptOutcome.OBSERVED)
        with self.assertRaises(PostActionError):
            ObservationAttempt(ObservationAttemptOutcome.MISSING, sample=ObservationSample("a-1", observation()))
        with self.assertRaises(PostActionError):
            ObservationSample("", observation())
        with self.assertRaises(PostActionError):
            ObservationSample("a-1", "not-an-observation")

    def test_build_post_action_loop_validates_the_clock(self) -> None:
        with self.assertRaises(PostActionError):
            build_post_action_loop(
                controller=ExecutionController(
                    policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
                    adapter=ScriptedAdapter(),
                ),
                observer=ScriptedObserver(),
                verifier=RecordingVerifier(),
                clock="not-callable",
            )


class GateSequenceTests(unittest.TestCase):
    def test_fresh_observation_confirms_and_releases_dependents(self) -> None:
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
            ObservationAttempt.observed(ObservationSample("a-place", observation())),
        )
        verifier = RecordingVerifier(VerificationStatus.CONFIRMED, VerificationStatus.CONFIRMED)
        self.assertEqual(
            STATE_CHANGING_ACTIONS & {ActionType.GRASP, ActionType.PLACE}, {ActionType.GRASP, ActionType.PLACE}
        )
        loop = make_loop(observer=observer, verifier=verifier)
        report = loop.run(
            graph(
                step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block")),
                step("s2", action("a-place", ActionType.PLACE, target_id="red_block"), depends_on=("s1",)),
            ),
            run_id="run-1",
        )
        self.assertTrue(report.may_advance)
        self.assertEqual(report.terminal, GateOutcome.CONFIRMED)
        self.assertEqual([item.outcome for item in report.steps], [GateOutcome.CONFIRMED] * 2)
        # Reduce precedes verify, every time.
        self.assertEqual(verifier.calls, ["reduce", "verify", "reduce", "verify"])
        self.assertEqual(loop_version_of(report), POST_ACTION_LOOP_VERSION)

    def test_completed_without_a_fresh_observation_blocks_dependents(self) -> None:
        """The core acceptance: adapter success alone never advances the task."""
        observer = ScriptedObserver(
            ObservationAttempt.missing("no new frame"),
            ObservationAttempt.missing("no new frame"),
        )
        verifier = RecordingVerifier()
        loop = make_loop(observer=observer, verifier=verifier, policy=PostActionPolicy(2))
        report = loop.run(
            graph(
                step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block")),
                step("s2", action("a-place", ActionType.PLACE, target_id="red_block"), depends_on=("s1",)),
            ),
            run_id="run-1",
        )
        self.assertFalse(report.may_advance)
        first = report.step("s1")
        self.assertEqual(first.outcome, GateOutcome.INSUFFICIENT_EVIDENCE)
        self.assertEqual(first.reason_code, GateReasonCode.OBSERVATION_MISSING)
        self.assertEqual(first.attempts, 2)
        second = report.step("s2")
        self.assertEqual(second.reason_code, GateReasonCode.DEPENDENCY_BLOCKED)
        # The refuted chain never reduced or verified anything.
        self.assertEqual(verifier.calls, [])

    def test_stale_wrong_run_wrong_action_and_wrong_entity_all_refuse(self) -> None:
        cases = {
            "stale": (
                ObservationAttempt.observed(ObservationSample("a-grasp", observation(observed_at=STALE_AT))),
                GateReasonCode.OBSERVATION_STALE,
            ),
            "wrong_run": (
                ObservationAttempt.observed(ObservationSample("a-grasp", observation(run_id="run-other"))),
                GateReasonCode.OBSERVATION_WRONG_RUN,
            ),
            "wrong_action": (
                ObservationAttempt.observed(ObservationSample("a-other", observation())),
                GateReasonCode.OBSERVATION_WRONG_ACTION,
            ),
            "wrong_entity": (
                ObservationAttempt.observed(ObservationSample("a-grasp", observation(entity_id="blue_cylinder"))),
                GateReasonCode.OBSERVATION_WRONG_ENTITY,
            ),
        }
        for label, (attempt, expected) in cases.items():
            with self.subTest(case=label):
                # A wrong correlation is a defect, not a transient miss, so the
                # loop must refuse on the first response instead of retrying.
                fresh = ObservationAttempt.observed(ObservationSample("a-grasp", observation()))
                observer = ScriptedObserver(attempt, fresh)
                verifier = RecordingVerifier(VerificationStatus.CONFIRMED)
                loop = make_loop(observer=observer, verifier=verifier, policy=PostActionPolicy(2))
                report = loop.run(
                    graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
                    run_id="run-1",
                )
                self.assertFalse(report.may_advance)
                self.assertEqual(report.steps[0].reason_code, expected)
                self.assertEqual(report.steps[0].attempts, 1)
                self.assertEqual(verifier.calls, [])

    def test_observation_at_the_completion_instant_is_not_fresh(self) -> None:
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation(observed_at=COMPLETED_AT)))
        )
        loop = make_loop(observer=observer, verifier=RecordingVerifier())
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.OBSERVATION_STALE)

    def test_refuted_stops_the_sequence_immediately(self) -> None:
        observer = ScriptedObserver(ObservationAttempt.observed(ObservationSample("a-grasp", observation())))
        verifier = RecordingVerifier(VerificationStatus.REFUTED)
        loop = make_loop(observer=observer, verifier=verifier)
        report = loop.run(
            graph(
                step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block")),
                step("s2", action("a-place", ActionType.PLACE, target_id="red_block"), depends_on=("s1",)),
            ),
            run_id="run-1",
        )
        self.assertEqual(report.terminal, GateOutcome.REFUTED)
        self.assertEqual(report.step("s1").reason_code, GateReasonCode.VERIFICATION_REFUTED)
        self.assertEqual(report.step("s2").reason_code, GateReasonCode.DEPENDENCY_BLOCKED)
        self.assertFalse(report.may_advance)

    def test_insufficient_evidence_re_observes_within_the_bounded_budget(self) -> None:
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
        )
        verifier = RecordingVerifier(
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            VerificationStatus.CONFIRMED,
        )
        loop = make_loop(observer=observer, verifier=verifier, policy=PostActionPolicy(2))
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertTrue(report.may_advance)
        self.assertEqual(report.steps[0].attempts, 2)
        self.assertEqual(len(report.steps[0].observations), 2)

    def test_insufficient_evidence_stops_when_the_budget_is_spent(self) -> None:
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
        )
        verifier = RecordingVerifier(VerificationStatus.INSUFFICIENT_EVIDENCE)
        loop = make_loop(observer=observer, verifier=verifier, policy=PostActionPolicy(3))
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertFalse(report.may_advance)
        self.assertEqual(report.steps[0].attempts, 3)
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.VERIFICATION_INSUFFICIENT)

    def test_perception_free_actions_do_not_need_an_observation(self) -> None:
        observer = ScriptedObserver()
        verifier = RecordingVerifier()
        loop = make_loop(observer=observer, verifier=verifier)
        report = loop.run(
            graph(step("s1", action("a-observe", ActionType.OBSERVE, target_id="tray"))),
            run_id="run-1",
        )
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.NOT_A_STATE_CHANGING_ACTION)
        self.assertEqual(report.steps[0].outcome, GateOutcome.NOT_APPLICABLE)
        self.assertEqual(observer.requests, [])
        self.assertEqual(verifier.calls, [])

    def test_failed_execution_is_never_verified(self) -> None:
        observer = ScriptedObserver()
        verifier = RecordingVerifier(VerificationStatus.CONFIRMED)
        loop = make_loop(
            observer=observer,
            verifier=verifier,
            adapter=FailingAdapter(RuntimeError("adapter exploded")),
        )
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.ACTION_NOT_COMPLETED)
        self.assertEqual(observer.requests, [])
        self.assertEqual(verifier.calls, [])


class StopTests(unittest.TestCase):
    def test_stop_is_dispatched_and_never_waits_for_perception(self) -> None:
        observer = ScriptedObserver()
        verifier = RecordingVerifier()
        loop = make_loop(observer=observer, verifier=verifier)
        report = loop.run(
            graph(step("s1", action("a-stop", ActionType.STOP))),
            run_id="run-1",
        )
        self.assertEqual(report.steps[0].outcome, GateOutcome.STOPPED)
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.STOP_DISPATCHED)
        self.assertFalse(report.may_advance)
        self.assertEqual(observer.requests, [])
        self.assertEqual(verifier.calls, [])

    def test_requested_stop_preempts_a_pending_physical_action(self) -> None:
        observer = ScriptedObserver(ObservationAttempt.observed(ObservationSample("a-place", observation())))
        verifier = RecordingVerifier(VerificationStatus.CONFIRMED)
        loop = make_loop(observer=observer, verifier=verifier)
        status = loop.request_stop(action("a-stop", ActionType.STOP))
        self.assertEqual(status.value, "accepted")
        report = loop.run(
            graph(
                step("s1", action("a-place", ActionType.PLACE, target_id="red_block")),
                step("s2", action("a-stop", ActionType.STOP), depends_on=("s1",)),
            ),
            run_id="run-1",
        )
        self.assertEqual(report.terminal, GateOutcome.STOPPED)
        self.assertFalse(report.may_advance)


class FailClosedStateTests(unittest.TestCase):
    def test_timeout_and_perception_failure_have_distinct_reasons(self) -> None:
        cases = {
            "timeout": (ObservationAttempt.timeout("camera slow"), GateReasonCode.OBSERVATION_TIMEOUT),
            "perception_failure": (
                ObservationAttempt.perception_failure("detector crashed"),
                GateReasonCode.PERCEPTION_FAILURE,
            ),
            "missing": (ObservationAttempt.missing("no frame"), GateReasonCode.OBSERVATION_MISSING),
        }
        for label, (attempt, expected) in cases.items():
            with self.subTest(case=label):
                observer = ScriptedObserver(attempt, attempt)
                loop = make_loop(
                    observer=observer,
                    verifier=RecordingVerifier(),
                    policy=PostActionPolicy(2),
                )
                report = loop.run(
                    graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
                    run_id="run-1",
                )
                self.assertEqual(report.steps[0].reason_code, expected)
                self.assertEqual(report.steps[0].attempts, 2)

    def test_verifier_failure_is_distinct_from_refutation(self) -> None:
        for fail_on in ("reduce", "verify"):
            with self.subTest(fail_on=fail_on):
                observer = ScriptedObserver(ObservationAttempt.observed(ObservationSample("a-grasp", observation())))
                loop = make_loop(observer=observer, verifier=ExplodingVerifier(fail_on=fail_on))
                report = loop.run(
                    graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
                    run_id="run-1",
                )
                self.assertEqual(report.steps[0].reason_code, GateReasonCode.VERIFIER_FAILED)
                self.assertEqual(report.steps[0].outcome, GateOutcome.INSUFFICIENT_EVIDENCE)

    def test_cancellation_blocks_every_step(self) -> None:
        observer = ScriptedObserver()
        loop = make_loop(observer=observer, verifier=RecordingVerifier())
        loop.cancel(reason="operator cancelled the run")
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertEqual(report.terminal, GateOutcome.INSUFFICIENT_EVIDENCE)
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.CANCELLED)
        self.assertEqual(observer.requests, [])

    def test_restart_invalidates_run_correlation_fail_closed(self) -> None:
        observer = ScriptedObserver()
        loop = make_loop(observer=observer, verifier=RecordingVerifier())
        loop.note_restart(run_id="run-1")
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.RESTART_INVALIDATED_RUN)
        self.assertEqual(observer.requests, [])
        # A different run is unaffected.
        other = loop.run(
            graph(step("s1", action("a-observe", ActionType.OBSERVE, target_id="tray"))),
            run_id="run-2",
        )
        self.assertNotEqual(other.steps[0].reason_code, GateReasonCode.RESTART_INVALIDATED_RUN)

    def test_cancel_and_restart_inputs_are_validated(self) -> None:
        loop = make_loop(observer=ScriptedObserver(), verifier=RecordingVerifier())
        with self.assertRaises(PostActionError):
            loop.cancel(reason="  ")
        with self.assertRaises(PostActionError):
            loop.note_restart(run_id="")
        with self.assertRaises(PostActionError):
            loop.run(graph(step("s1", action("a-observe", ActionType.OBSERVE))), run_id=" ")
        with self.assertRaises(PostActionError):
            loop.run("not-a-graph", run_id="run-1")


class DuplicateDeliveryTests(unittest.TestCase):
    def test_the_same_action_cannot_be_gated_twice(self) -> None:
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
        )
        verifier = RecordingVerifier(VerificationStatus.CONFIRMED, VerificationStatus.CONFIRMED)
        loop = make_loop(observer=observer, verifier=verifier)
        run_step = graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block")))

        first = loop.run(run_step, run_id="run-1")
        self.assertTrue(first.may_advance)

        second = loop.run(run_step, run_id="run-1")
        self.assertEqual(second.steps[0].reason_code, GateReasonCode.DUPLICATE_DELIVERY)
        self.assertFalse(second.may_advance)
        # The duplicate was neither observed nor verified a second time.
        self.assertEqual(len(observer.requests), 1)
        self.assertEqual(verifier.calls, ["reduce", "verify"])

    def test_controller_duplicate_suppression_still_applies(self) -> None:
        adapter = ScriptedAdapter()
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
        )
        controller = ExecutionController(
            policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
            adapter=adapter,
        )
        loop = PostActionLoop(controller=controller, observer=observer, verifier=RecordingVerifier())
        run_step = graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block")))
        loop.run(run_step, run_id="run-1")

        # A second loop over the same controller cannot redispatch the action.
        fresh = PostActionLoop(controller=controller, observer=observer, verifier=RecordingVerifier())
        report = fresh.run(run_step, run_id="run-1")
        self.assertEqual(adapter.dispatched, ["a-grasp"])
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.DUPLICATE_DELIVERY)
        # The controller reports the duplicate at the report level; the never-run
        # record itself carries no per-step reason.
        self.assertIsNone(report.steps[0].execution.reason_code)
        self.assertNotEqual(report.steps[0].outcome, GateOutcome.CONFIRMED)


class DependencyOrderTests(unittest.TestCase):
    def test_a_step_with_an_unconfirmed_dependency_is_blocked_not_dispatched(self) -> None:
        adapter = ScriptedAdapter()
        observer = ScriptedObserver(
            ObservationAttempt.observed(ObservationSample("a-grasp", observation())),
        )
        verifier = RecordingVerifier(VerificationStatus.REFUTED)
        controller = ExecutionController(
            policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
            adapter=adapter,
        )
        loop = PostActionLoop(controller=controller, observer=observer, verifier=verifier)
        report = loop.run(
            graph(
                step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block")),
                step("s2", action("a-place", ActionType.PLACE, target_id="red_block"), depends_on=("s1",)),
            ),
            run_id="run-1",
        )
        self.assertEqual(report.step("s2").reason_code, GateReasonCode.DEPENDENCY_BLOCKED)
        self.assertIsNone(report.step("s2").execution)
        self.assertEqual(adapter.dispatched, ["a-grasp"])

    def test_unreadable_completion_time_is_refused_rather_than_assumed_fresh(self) -> None:
        adapter = ScriptedAdapter(ended_at="not-a-timestamp")
        controller = ExecutionController(
            policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
            adapter=adapter,
        )
        observer = ScriptedObserver(ObservationAttempt.observed(ObservationSample("a-grasp", observation())))
        loop = PostActionLoop(controller=controller, observer=observer, verifier=RecordingVerifier())
        report = loop.run(
            graph(step("s1", action("a-grasp", ActionType.GRASP, target_id="red_block"))),
            run_id="run-1",
        )
        self.assertEqual(report.steps[0].reason_code, GateReasonCode.OBSERVATION_UNREADABLE_TIME)
        self.assertFalse(report.may_advance)


def loop_version_of(report) -> str:
    return report.steps[0].loop_version


if __name__ == "__main__":
    unittest.main()
