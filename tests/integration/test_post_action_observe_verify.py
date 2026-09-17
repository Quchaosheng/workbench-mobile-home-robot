"""Issue #151 against the real World Model reducer and verifier.

`tests/unit/test_post_action_gate.py` proves the gate's own rules with fakes.
This file proves the port shapes compose with the shipped reduction and
verification, so the gate is not a private protocol no production consumer could
satisfy.

The adapter below is the knowledge a real caller also has to supply: how a
location is derived from a detection and which claim an action is verifying.
Neither belongs in the gate, and neither is reimplemented here.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/agent_runtime"),
    str(ROOT / "services/world_model"),
]

from workbench_agent_runtime.execution_controller import ExecutionController
from workbench_agent_runtime.policy_validator import PolicyValidator
from workbench_agent_runtime.post_action import (
    GateOutcome,
    GateReasonCode,
    ObservationAttempt,
    ObservationSample,
    PostActionLoop,
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
    WorldEvent,
    WorldEventType,
)
from workbench_world_model import (
    FreshnessThresholds,
    ObservationAgingBoundary,
    ObservationFreshnessPolicy,
    VerificationContext,
    create_world_state_snapshot,
    reduce_events,
    verify_object_in_tray,
)
from workbench_world_model.reducer import WorldState

POLICY_CONFIG = {"policy_version": "post-action-integration-v1", "high_impact_actions": frozenset()}

RUN_ID = "run-151"
GRASP_ACTION_ID = "a-grasp"
TASK_ID = "task-place-red-block"
TRAY_ID = "tray"
COMPLETED_AT = "2026-09-17T12:00:01Z"
FRESH_AT = "2026-09-17T12:00:02Z"
AGED_AS_OF = "2026-09-17T12:00:03Z"

# The real verifier refuses to confirm from a state whose freshness was never
# evaluated, so the adapter must supply an explicit policy and boundary rather
# than let the gate assume freshness.
FRESHNESS_POLICY = ObservationFreshnessPolicy(
    rules={
        ("camera", "block"): FreshnessThresholds(stale_after_s=5.0, lost_after_s=30.0),
        # A location relation is only confirmable when its endpoint is also fresh,
        # so the policy has to cover the endpoint's entity type as well.
        ("camera", "tray"): FreshnessThresholds(stale_after_s=5.0, lost_after_s=30.0),
    }
)
AGING_BOUNDARY = ObservationAgingBoundary(as_of=AGED_AS_OF, clock_id="wall")


def observation(observation_id: str, *, observed_at: str = FRESH_AT) -> Observation:
    return Observation(
        observation_id=observation_id,
        run_id=RUN_ID,
        entity_id="red_block",
        entity_type="block",
        pose=Pose(
            frame_id="base_link",
            position=Position(x=0.1, y=0.2, z=0.3),
            orientation=Orientation(x=0.0, y=0.0, z=0.0, w=1.0),
        ),
        confidence=0.95,
        observed_at=observed_at,
        clock_id=ClockId.WALL,
        source="camera",
        evidence_refs=[f"frame-{observation_id}"],
    )


class CompletedAdapter:
    def __init__(self) -> None:
        self.dispatched: list[str] = []

    def dispatch(self, semantic_action: SemanticAction) -> ActionResult:
        self.dispatched.append(semantic_action.action_id)
        return ActionResult(
            result_id=f"result-{semantic_action.action_id}",
            action_id=semantic_action.action_id,
            run_id=RUN_ID,
            outcome=ActionOutcome.COMPLETED,
            dispatch_state=DispatchState.SENT,
            device_state=DeviceState.CONFIRMED,
            started_at="2026-09-17T12:00:00Z",
            ended_at=COMPLETED_AT,
        )


class ScriptedObserver:
    def __init__(self, *attempts: ObservationAttempt) -> None:
        self._attempts = list(attempts)
        self.requests: list[str] = []

    def observe(self, *, run_id, action_id, entity_id, not_before):
        self.requests.append(action_id)
        if not self._attempts:
            return ObservationAttempt.missing("observer script exhausted")
        return self._attempts.pop(0)


class RealWorldModelVerifier:
    """Reduce observations with the shipped reducer, then verify tray membership.

    ``locations`` maps an observation id to the location the detection implies;
    that mapping is caller knowledge. Everything after it is the real World Model.
    """

    def __init__(self, locations: dict[str, str]) -> None:
        self._locations = locations
        self._last_events: list[WorldEvent] = []
        self.calls: list[str] = []

    def _events(self, observations, *, run_id: str) -> list[WorldEvent]:
        events: list[WorldEvent] = []
        for item in observations:
            if item.observation_id not in self._locations:
                raise KeyError(f"no location configured for {item.observation_id}")
            location = self._locations[item.observation_id]
            events.append(
                self._event(
                    run_id,
                    len(events),
                    entity_id=item.entity_id,
                    entity_type=item.entity_type,
                    location=location,
                    confidence=item.confidence,
                    item=item,
                )
            )
            # A location relation is only verifiable when its endpoint was also
            # observed: the reducer marks an unobserved endpoint LOST, and the
            # verifier then refuses to confirm the relation. The same camera frame
            # that shows the block in the tray also shows the tray, so the adapter
            # reports the endpoint from the same evidence rather than inventing a
            # second detection.
            endpoint = location.partition(":")[2]
            if endpoint and not any(event.payload["entity_id"] == endpoint for event in events):
                events.append(
                    self._event(
                        run_id,
                        len(events),
                        entity_id=endpoint,
                        entity_type="tray",
                        location=None,
                        confidence=item.confidence,
                        item=item,
                    )
                )
        return events

    @staticmethod
    def _event(
        run_id: str,
        index: int,
        *,
        entity_id: str,
        entity_type: str,
        location: str | None,
        confidence: float,
        item: Observation,
    ) -> WorldEvent:
        payload: dict[str, object] = {
            "observation_id": f"{item.observation_id}-{entity_id}",
            "entity_id": entity_id,
            "entity_type": entity_type,
            "confidence": confidence,
            # Aging needs to know which clock produced the stamp and which
            # source's policy governs it; without both, the reducer marks the
            # entity LOST rather than fresh. The reducer also reads the timestamp
            # from the payload, not from the event envelope.
            "clock_id": item.clock_id.value,
            "source": item.source,
            "observed_at": item.observed_at,
        }
        if location is not None:
            payload["location"] = location
        return WorldEvent(
            event_id=f"{run_id}-evt-{index:03d}",
            run_id=run_id,
            sequence_no=index + 1,
            event_type=WorldEventType.OBSERVATION,
            occurred_at=item.observed_at,
            payload=payload,
            evidence_refs=list(item.evidence_refs),
        )

    def reduce(self, observations, *, run_id: str) -> WorldState:
        self.calls.append("reduce")
        self._last_events = self._events(observations, run_id=run_id)
        return reduce_events(
            run_id,
            self._last_events,
            freshness_policy=FRESHNESS_POLICY,
            aging_boundary=AGING_BOUNDARY,
        )

    def verify(self, state: WorldState, *, run_id: str, action_id: str):
        self.calls.append("verify")
        snapshot = create_world_state_snapshot(run_id, self._last_events)
        return verify_object_in_tray(
            state,
            TASK_ID,
            "red_block",
            TRAY_ID,
            context=VerificationContext(
                state_hash=snapshot.state_hash,
                verified_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                clock_id="wall",
            ),
        )


def build_loop(*, observer: ScriptedObserver, verifier: RealWorldModelVerifier) -> PostActionLoop:
    return PostActionLoop(
        controller=ExecutionController(
            policy_validator=PolicyValidator(policy_config=POLICY_CONFIG),
            adapter=CompletedAdapter(),
        ),
        observer=observer,
        verifier=verifier,
    )


def grasp_graph() -> TaskGraph:
    return TaskGraph(
        task_id=TASK_ID,
        goal="grasp the red block",
        steps=[
            TaskStep(
                step_id="s1",
                action=SemanticAction(
                    action_id=GRASP_ACTION_ID,
                    action_type=ActionType.GRASP,
                    target_id="red_block",
                ),
                depends_on=[],
            )
        ],
        planner="integration",
        model_route="template",
    )


def test_adapter_success_without_a_fresh_observation_does_not_advance() -> None:
    """The issue's core claim, against the real World Model.

    The entity is already in the tray in the adapter's view, so the verifier
    would confirm on the first try. With no fresh observation, the loop must
    still refuse and must not even call the verifier.
    """
    verifier = RealWorldModelVerifier({"obs-fresh": "in:tray"})
    loop = build_loop(observer=ScriptedObserver(), verifier=verifier)
    report = loop.run(grasp_graph(), run_id=RUN_ID)

    assert report.may_advance is False
    assert report.steps[0].outcome is GateOutcome.INSUFFICIENT_EVIDENCE
    assert report.steps[0].reason_code is GateReasonCode.OBSERVATION_MISSING
    assert verifier.calls == []


def test_fresh_observation_is_reduced_and_verified_by_the_real_reducer() -> None:
    verifier = RealWorldModelVerifier({"obs-fresh": "in:tray"})
    loop = build_loop(
        observer=ScriptedObserver(
            ObservationAttempt.observed(ObservationSample(GRASP_ACTION_ID, observation("obs-fresh")))
        ),
        verifier=verifier,
    )
    report = loop.run(grasp_graph(), run_id=RUN_ID)

    assert report.may_advance is True
    step = report.steps[0]
    assert step.outcome is GateOutcome.CONFIRMED
    assert step.reason_code is GateReasonCode.POST_ACTION_CONFIRMED
    # The verifier really was the World Model verifier, in the right order.
    assert verifier.calls == ["reduce", "verify"]
    assert step.verification.status.value == "confirmed"
    assert step.verification.rule_version != "unversioned"


def test_a_fresh_observation_that_contradicts_the_claim_is_refuted() -> None:
    """A genuinely fresh observation saying 'still on the table' must refute."""
    verifier = RealWorldModelVerifier({"obs-fresh": "on:table"})
    loop = build_loop(
        observer=ScriptedObserver(
            ObservationAttempt.observed(ObservationSample(GRASP_ACTION_ID, observation("obs-fresh")))
        ),
        verifier=verifier,
    )
    report = loop.run(grasp_graph(), run_id=RUN_ID)

    assert report.may_advance is False
    assert report.steps[0].outcome is GateOutcome.REFUTED
    assert report.steps[0].reason_code is GateReasonCode.VERIFICATION_REFUTED


def test_a_real_reducer_failure_becomes_a_verifier_failure_not_a_refutation() -> None:
    """An unconfigured observation makes the adapter raise; that is a failure."""
    verifier = RealWorldModelVerifier({})
    loop = build_loop(
        observer=ScriptedObserver(
            ObservationAttempt.observed(ObservationSample(GRASP_ACTION_ID, observation("obs-fresh")))
        ),
        verifier=verifier,
    )
    report = loop.run(grasp_graph(), run_id=RUN_ID)

    assert report.may_advance is False
    assert report.steps[0].reason_code is GateReasonCode.VERIFIER_FAILED
    assert report.steps[0].outcome is GateOutcome.INSUFFICIENT_EVIDENCE
