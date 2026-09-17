from __future__ import annotations

import inspect
import json
import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

import workbench_world_model.aging as aging_module
from workbench_contracts import (
    ATTRIBUTE_SCHEMA_VERSION,
    ActionOutcome,
    ActionResult,
    ClockId,
    DeviceState,
    DispatchState,
    ReasonCode,
    RecoveryHint,
    VerificationStatus,
    WorldBelief,
    WorldEvent,
    WorldEventType,
)
from workbench_world_model import (
    FreshnessThresholds,
    ObservationAgingBoundary,
    ObservationFreshnessPolicy,
    VerificationContext,
    apply_event,
    create_world_state_snapshot,
    reduce_events,
    verify_inspection_evidence,
    verify_kit_contents,
    verify_object_in_tray,
    verify_parcel_policy,
    verify_parcel_sorting,
    verify_workspace_clearance,
)
from workbench_world_model.event_payloads import WorldEventPayloadValidationError
from workbench_world_model.reducer import WorldState

MISSING = object()


def freshness_policy(
    *,
    block_stale: float = 10.0,
    block_lost: float = 20.0,
    tray_stale: float | None = None,
    tray_lost: float | None = None,
) -> ObservationFreshnessPolicy:
    return ObservationFreshnessPolicy(
        rules={
            ("camera", "block"): FreshnessThresholds(
                stale_after_s=block_stale,
                lost_after_s=block_lost,
            ),
            ("camera", "tray"): FreshnessThresholds(
                stale_after_s=block_stale if tray_stale is None else tray_stale,
                lost_after_s=block_lost if tray_lost is None else tray_lost,
            ),
            ("camera", "fixture"): FreshnessThresholds(
                stale_after_s=block_stale,
                lost_after_s=block_lost,
            ),
            ("camera", "parcel"): FreshnessThresholds(
                stale_after_s=block_stale,
                lost_after_s=block_lost,
            ),
        }
    )


def boundary(as_of: str, clock_id: str = "wall") -> ObservationAgingBoundary:
    return ObservationAgingBoundary(as_of=as_of, clock_id=clock_id)


def observation_event(
    event_id: str,
    sequence_no: int,
    *,
    run_id: str = "run-aging",
    entity_id: str = "red_block",
    entity_type: str = "block",
    location: str | None = "in:tray",
    observed_at: object = "2026-08-28T00:00:00Z",
    clock_id: object = "wall",
    source: object = "camera",
    confidence: float = 0.95,
    attributes: dict[str, str] | None = None,
    pose: dict[str, object] | None = None,
    evidence_refs: list[str] | None = None,
) -> WorldEvent:
    payload: dict[str, object] = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "confidence": confidence,
    }
    if location is not None:
        payload["location"] = location
    if observed_at is not MISSING:
        payload["observed_at"] = observed_at
    if clock_id is not MISSING:
        payload["clock_id"] = clock_id
    if source is not MISSING:
        payload["source"] = source
    if attributes is not None:
        payload["attributes"] = attributes
        payload["attributes_schema_version"] = ATTRIBUTE_SCHEMA_VERSION
        payload["attribute_metadata"] = {
            key: {
                "observed_at": "2026-08-28T00:00:00Z" if observed_at is MISSING else observed_at,
                "confidence": confidence,
                "evidence_refs": evidence_refs or [f"frame://{event_id}"],
                "belief": "observed",
                "clock_id": "wall",
                "source": "camera" if source is MISSING else source,
            }
            for key in attributes
        }
    if pose is not None:
        payload["pose"] = pose
    return WorldEvent(
        event_id=event_id,
        run_id=run_id,
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-28T00:00:00Z",
        payload=payload,
        evidence_refs=evidence_refs or [f"frame://{event_id}"],
    )


def action_result_event(sequence_no: int = 2) -> WorldEvent:
    result = ActionResult(
        result_id="result-aging",
        action_id="action-aging",
        run_id="run-aging",
        outcome=ActionOutcome.COMPLETED,
        dispatch_state=DispatchState.SENT,
        device_state=DeviceState.CONFIRMED,
        started_at="2026-08-28T00:00:01Z",
        ended_at="2026-08-28T00:00:30Z",
        clock_id=ClockId.WALL,
        entity_id="red_block",
        resulting_location="in:tray",
        evidence_refs=["action://aging"],
    )
    return WorldEvent(
        event_id="evt-action-aging",
        run_id="run-aging",
        sequence_no=sequence_no,
        event_type=WorldEventType.ACTION_RESULT,
        occurred_at=result.ended_at,
        payload=result.model_dump(mode="json"),
        evidence_refs=list(result.evidence_refs),
    )


def aged_state(
    events: list[WorldEvent],
    *,
    as_of: str,
    policy: ObservationFreshnessPolicy | None = None,
    clock_id: str = "wall",
) -> WorldState:
    return reduce_events(
        events[0].run_id,
        events,
        freshness_policy=policy or freshness_policy(),
        aging_boundary=boundary(as_of, clock_id),
    )


def aged_snapshot(
    events: list[WorldEvent],
    *,
    as_of: str,
    policy: ObservationFreshnessPolicy | None = None,
    clock_id: str = "wall",
):
    return create_world_state_snapshot(
        events[0].run_id,
        events,
        freshness_policy=policy or freshness_policy(),
        aging_boundary=boundary(as_of, clock_id),
    )


def verification_context(state_hash: str) -> VerificationContext:
    return VerificationContext(
        state_hash=state_hash,
        verified_at="2026-08-28T12:00:00Z",
        clock_id="wall",
    )


def test_fresh_observation_remains_observed() -> None:
    event = observation_event("evt-fresh", 1)
    state = aged_state([event], as_of="2026-08-28T00:00:09.999999Z")
    snapshot = aged_snapshot([event], as_of="2026-08-28T00:00:09.999999Z")

    assert state.entity_beliefs["red_block"] is WorldBelief.OBSERVED
    assert snapshot.entities[0].belief is WorldBelief.OBSERVED


def test_exact_stale_and_lost_thresholds() -> None:
    event = observation_event("evt-threshold", 1)

    stale = aged_snapshot([event], as_of="2026-08-28T00:00:10Z")
    lost = aged_snapshot([event], as_of="2026-08-28T00:00:20Z")

    assert stale.entities[0].belief is WorldBelief.STALE
    assert lost.entities[0].belief is WorldBelief.LOST


def test_policy_requires_explicit_bounded_rules() -> None:
    with pytest.raises(ValueError):
        ObservationFreshnessPolicy(rules={})
    with pytest.raises(ValueError):
        ObservationFreshnessPolicy(rules={("*", "block"): FreshnessThresholds(stale_after_s=1, lost_after_s=2)})
    for stale_after, lost_after in (
        (0, 1),
        (-1, 1),
        (2, 1),
        (float("inf"), float("inf")),
        (1, float("nan")),
    ):
        with pytest.raises(ValueError):
            FreshnessThresholds(stale_after_s=stale_after, lost_after_s=lost_after)


def test_deployment_policy_can_only_tighten() -> None:
    original = freshness_policy()

    equal = original.tightened({("camera", "block"): FreshnessThresholds(stale_after_s=10, lost_after_s=20)})
    tighter = original.tightened({("camera", "block"): FreshnessThresholds(stale_after_s=5, lost_after_s=15)})

    assert equal.rules[("camera", "block")] == original.rules[("camera", "block")]
    assert tighter.rules[("camera", "block")].stale_after_s == 5
    assert tighter.rules[("camera", "block")].lost_after_s == 15
    with pytest.raises(ValueError, match="widen"):
        original.tightened({("camera", "block"): FreshnessThresholds(stale_after_s=11, lost_after_s=20)})
    with pytest.raises(ValueError, match="widen"):
        original.tightened({("camera", "block"): FreshnessThresholds(stale_after_s=10, lost_after_s=21)})
    with pytest.raises(ValueError, match="unknown"):
        original.tightened({("lidar", "block"): FreshnessThresholds(stale_after_s=5, lost_after_s=10)})


def test_aging_requires_explicit_boundary_without_hidden_clock_reads() -> None:
    event = observation_event("evt-explicit", 1)
    policy = freshness_policy()

    with pytest.raises(ValueError, match="together"):
        reduce_events("run-aging", [event], freshness_policy=policy)
    with pytest.raises(ValueError, match="together"):
        reduce_events("run-aging", [event], aging_boundary=boundary("2026-08-28T00:00:01Z"))

    explicit_boundary = boundary("2026-08-28T00:00:01Z")
    with pytest.raises(FrozenInstanceError):
        explicit_boundary.as_of = "2026-08-28T00:00:02Z"  # type: ignore[misc]

    source = inspect.getsource(aging_module)
    for hidden_clock in ("datetime.now", "datetime.utcnow", "time.time", "time.monotonic", "time.sleep"):
        assert hidden_clock not in source
    assert aged_state([event], as_of=explicit_boundary.as_of).entity_beliefs["red_block"] is WorldBelief.OBSERVED


def test_unaged_public_reducer_and_apply_event_cannot_confirm() -> None:
    event = observation_event(
        "evt-unaged-public-path",
        1,
        observed_at="2026-08-01T00:00:00Z",
        evidence_refs=["frame://unaged-public-path"],
    )
    context = verification_context("a" * 64)

    reduced_state = reduce_events("run-aging", [event])
    applied_state = apply_event(WorldState(run_id="run-aging"), event)

    assert all(not state.freshness_evaluated for state in (reduced_state, applied_state))
    for state in (reduced_state, applied_state):
        result = verify_object_in_tray(
            state,
            "task-unaged-public-path",
            "red_block",
            "tray",
            context=context,
        )
        assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
        assert result.reason_code is ReasonCode.STALE_OBSERVATION
        assert result.recovery_hint is RecoveryHint.RE_OBSERVE


def test_clock_id_enum_remains_comparable() -> None:
    event = observation_event(
        "evt-enum-clock",
        1,
        location=None,
        clock_id=ClockId.WALL,
    )

    state = aged_state([event], as_of="2026-08-28T00:00:01Z")

    assert state.entity_observation_clock_ids["red_block"] == ClockId.WALL.value
    assert state.entity_beliefs["red_block"] is WorldBelief.OBSERVED


@pytest.mark.parametrize(
    ("updates", "as_of"),
    [
        ({"observed_at": MISSING}, "2026-08-28T00:00:01Z"),
        ({"clock_id": MISSING}, "2026-08-28T00:00:01Z"),
        ({"source": "unknown-camera"}, "2026-08-28T00:00:01Z"),
        ({"observed_at": "not-a-time"}, "2026-08-28T00:00:01Z"),
        ({"observed_at": "2026-08-28T00:00:02Z"}, "2026-08-28T00:00:01Z"),
    ],
)
def test_missing_or_incomparable_time_fails_closed(updates: dict[str, object], as_of: str) -> None:
    event = observation_event("evt-invalid-clock", 1, **updates)
    state = aged_state([event], as_of=as_of)
    snapshot = aged_snapshot([event], as_of=as_of)
    result = verify_object_in_tray(
        state,
        "task-aging",
        "red_block",
        "tray",
        context=verification_context(snapshot.state_hash),
    )

    assert state.entity_beliefs["red_block"] is WorldBelief.LOST
    assert snapshot.entities[0].belief is WorldBelief.LOST
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code is ReasonCode.STALE_OBSERVATION
    assert result.recovery_hint is RecoveryHint.RE_OBSERVE


def test_non_string_observation_time_is_rejected_at_the_event_boundary() -> None:
    """A malformed timestamp never reaches aging: it fails at ingestion."""

    with pytest.raises(WorldEventPayloadValidationError):
        reduce_events("run-aging", [observation_event("evt-nonstring-time", 1, observed_at=7)])


@pytest.mark.parametrize(
    ("observation_clock", "boundary_clock"),
    [("monotonic", "monotonic"), ("monotonic", "wall"), ("wall", "monotonic")],
)
def test_monotonic_without_serialized_origin_fails_closed(
    observation_clock: str,
    boundary_clock: str,
) -> None:
    event = observation_event("evt-monotonic", 1, clock_id=observation_clock)
    state = aged_state(
        [event],
        as_of="2026-08-28T00:00:01Z",
        clock_id=boundary_clock,
    )

    assert state.entity_beliefs["red_block"] is WorldBelief.LOST


def test_aging_preserves_audit_value_and_evidence() -> None:
    pose = {
        "frame_id": "table",
        "position": {"x": 0.2, "y": 0.1, "z": 0.02},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }
    event = observation_event(
        "evt-audit",
        1,
        confidence=0.93,
        attributes={"colour": "red"},
        pose=pose,
        evidence_refs=["camera://audit"],
    )
    unaged = reduce_events("run-aging", [event])

    for as_of, expected_belief in (
        ("2026-08-28T00:00:10Z", WorldBelief.STALE),
        ("2026-08-28T00:00:20Z", WorldBelief.LOST),
    ):
        state = aged_state([event], as_of=as_of)
        snapshot = aged_snapshot([event], as_of=as_of)
        entity = snapshot.entities[0]

        assert state.entity_locations == unaged.entity_locations
        assert state.entity_poses == unaged.entity_poses
        assert state.entity_confidence == unaged.entity_confidence
        assert state.entity_attributes == unaged.entity_attributes
        assert state.entity_evidence_refs == unaged.entity_evidence_refs
        assert state.entity_last_observed_at == unaged.entity_last_observed_at
        assert state.entity_observation_clock_ids == unaged.entity_observation_clock_ids
        assert state.entity_observation_sources == unaged.entity_observation_sources
        assert entity.belief is expected_belief
        assert entity.pose is not None
        assert entity.confidence == 0.93
        assert entity.last_observed_at == "2026-08-28T00:00:00Z"
        assert entity.evidence_refs == ["camera://audit"]
        assert snapshot.relations[0].evidence_refs == ["camera://audit"]


def _confirmed_verifier_cases(belief: WorldBelief):
    object_state = WorldState(
        run_id="object-aging",
        entity_locations={"red_block": "in:tray"},
        entity_evidence_refs={"red_block": ["frame://object"]},
        entity_beliefs={"red_block": belief},
    )
    kit_state = WorldState(
        run_id="kit-aging",
        entity_locations={"part": "in:kit_tray"},
        entity_confidence={"part": 0.95},
        entity_evidence_refs={"part": ["frame://kit"]},
        entity_beliefs={"part": belief},
    )
    inspection_state = WorldState(
        run_id="inspection-aging",
        entity_locations={"sensor": "on:table"},
        entity_confidence={"sensor": 0.95},
        entity_evidence_refs={"sensor": ["frame://inspection"]},
        entity_beliefs={"sensor": belief},
    )
    clearance_state = WorldState(
        run_id="clearance-aging",
        entity_locations={"red_block": "in:tray", "blue_cylinder": "in:staging_bin"},
        entity_evidence_refs={
            "red_block": ["frame://clear/red"],
            "blue_cylinder": ["frame://clear/blue"],
        },
        entity_beliefs={"red_block": belief, "blue_cylinder": belief},
    )
    parcel_sorting_state = WorldState(
        run_id="sorting-aging",
        entity_locations={
            "parcel_box": "in:pickup_shelf",
            "parcel_envelope": "in:pickup_shelf",
            "parcel_damaged": "in:quarantine_bin",
        },
        entity_confidence={"parcel_box": 0.95, "parcel_envelope": 0.95, "parcel_damaged": 0.95},
        entity_attributes={
            "parcel_box": {"label_status": "verified", "condition": "intact"},
            "parcel_envelope": {"label_status": "verified", "condition": "intact"},
            "parcel_damaged": {"label_status": "verified", "condition": "damaged"},
        },
        entity_evidence_refs={
            "parcel_box": ["frame://sort/box"],
            "parcel_envelope": ["frame://sort/envelope"],
            "parcel_damaged": ["frame://sort/damaged"],
        },
        entity_beliefs={
            "parcel_box": belief,
            "parcel_envelope": belief,
            "parcel_damaged": belief,
        },
    )
    parcel_policy_state = WorldState(
        run_id="policy-aging",
        entity_locations={"parcel": "in:pickup_shelf"},
        entity_confidence={"parcel": 0.95},
        entity_attributes={"parcel": {"label_status": "verified", "condition": "intact"}},
        entity_evidence_refs={"parcel": ["frame://policy"]},
        entity_beliefs={"parcel": belief},
    )
    context = verification_context("a" * 64)
    return [
        verify_object_in_tray(object_state, "task-object", "red_block", "tray", context=context),
        verify_kit_contents(kit_state, "task-kit", ["part"], context=context),
        verify_inspection_evidence(inspection_state, "task-inspection", ["sensor"], context=context),
        verify_workspace_clearance(clearance_state, "task-clearance", context=context),
        verify_parcel_sorting(parcel_sorting_state, "task-sorting", context=context),
        verify_parcel_policy(parcel_policy_state, "task-policy", ["parcel"], context=context),
    ]


@pytest.mark.parametrize("belief", [WorldBelief.STALE, WorldBelief.LOST])
def test_all_verifiers_reject_stale_and_lost_support(belief: WorldBelief) -> None:
    for result in _confirmed_verifier_cases(belief):
        assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
        assert result.reason_code is ReasonCode.STALE_OBSERVATION
        assert result.recovery_hint is RecoveryHint.RE_OBSERVE


def test_newer_observation_restores_observed() -> None:
    old = observation_event(
        "evt-old",
        1,
        observed_at="2026-08-28T00:00:00Z",
        evidence_refs=["frame://old"],
    )
    assert aged_state([old], as_of="2026-08-28T00:00:20Z").entity_beliefs["red_block"] is WorldBelief.LOST

    new = observation_event(
        "evt-new",
        2,
        observed_at="2026-08-28T00:00:19Z",
        evidence_refs=["frame://new"],
    )
    state = aged_state([old, new], as_of="2026-08-28T00:00:20Z")
    snapshot = aged_snapshot([old, new], as_of="2026-08-28T00:00:20Z")

    assert state.entity_beliefs["red_block"] is WorldBelief.OBSERVED
    assert state.entity_last_observed_at["red_block"] == "2026-08-28T00:00:19Z"
    assert "frame://new" in state.entity_evidence_refs["red_block"]
    assert snapshot.entities[0].belief is WorldBelief.OBSERVED


def test_out_of_order_observation_cannot_refresh_state() -> None:
    tray = observation_event(
        "evt-tray",
        0,
        entity_id="tray",
        entity_type="tray",
        location=None,
        observed_at="2026-08-28T00:00:10Z",
        evidence_refs=["frame://tray"],
    )
    newer = observation_event(
        "evt-newer",
        1,
        location="in:tray",
        observed_at="2026-08-28T00:00:10Z",
        evidence_refs=["frame://newer"],
    )
    older_later_sequence = observation_event(
        "evt-older-later-sequence",
        2,
        location="on:table",
        observed_at="2026-08-28T00:00:05Z",
        evidence_refs=["frame://older"],
    )
    state = aged_state([tray, newer, older_later_sequence], as_of="2026-08-28T00:00:12Z")
    snapshot = aged_snapshot([tray, newer, older_later_sequence], as_of="2026-08-28T00:00:12Z")
    result = verify_object_in_tray(
        state,
        "task-order",
        "red_block",
        "tray",
        context=verification_context(snapshot.state_hash),
    )

    assert state.entity_locations["red_block"] == "in:tray"
    assert state.entity_last_observed_at["red_block"] == "2026-08-28T00:00:10Z"
    assert state.entity_evidence_refs["red_block"] == ["frame://newer"]
    assert result.status is VerificationStatus.CONFIRMED
    assert result.evidence_refs == ["frame://newer"]


def test_action_result_does_not_refresh_observation_age() -> None:
    observation = observation_event("evt-observation", 1)
    observation_only = aged_state([observation], as_of="2026-08-28T00:00:10Z")
    with_action = aged_state(
        [observation, action_result_event()],
        as_of="2026-08-28T00:00:10Z",
    )

    for field_name in (
        "entity_last_observed_at",
        "entity_observation_clock_ids",
        "entity_observation_sources",
        "entity_location_last_observed_at",
        "entity_location_clock_ids",
        "entity_location_sources",
        "entity_beliefs",
        "entity_location_beliefs",
    ):
        assert getattr(with_action, field_name) == getattr(observation_only, field_name)
    assert "action://aging" in with_action.evidence_refs
    assert "action://aging" not in with_action.entity_evidence_refs["red_block"]


def test_relation_belief_cannot_outlive_required_entities() -> None:
    events = [
        observation_event(
            "evt-tray-old",
            1,
            entity_id="tray",
            entity_type="tray",
            location="on:table",
            observed_at="2026-08-28T00:00:00Z",
        ),
        observation_event(
            "evt-block-fresh",
            2,
            entity_id="red_block",
            entity_type="block",
            location="in:tray",
            observed_at="2026-08-28T00:00:19Z",
        ),
    ]
    policy = freshness_policy(tray_stale=5, tray_lost=10)
    state = aged_state(events, as_of="2026-08-28T00:00:20Z", policy=policy)
    snapshot = aged_snapshot(events, as_of="2026-08-28T00:00:20Z", policy=policy)
    relation = next(item for item in snapshot.relations if item.subject_id == "red_block")
    result = verify_object_in_tray(
        state,
        "task-relation-aging",
        "red_block",
        "tray",
        context=verification_context(snapshot.state_hash),
    )

    assert state.entity_beliefs["red_block"] is WorldBelief.OBSERVED
    assert state.entity_beliefs["tray"] is WorldBelief.LOST
    assert state.entity_location_beliefs["red_block"] is WorldBelief.LOST
    assert relation.belief is WorldBelief.LOST
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code is ReasonCode.STALE_OBSERVATION


def test_same_boundary_replays_identically_across_processes() -> None:
    script = """
import json
from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model import (
    FreshnessThresholds,
    ObservationAgingBoundary,
    ObservationFreshnessPolicy,
    create_world_state_snapshot,
)
event = WorldEvent(
    event_id="evt-process-aging",
    run_id="run-process-aging",
    sequence_no=1,
    event_type=WorldEventType.OBSERVATION,
    occurred_at="2026-08-28T00:00:00Z",
    payload={
        "entity_id":"red_block",
        "entity_type":"block",
        "location":"in:tray",
        "confidence":0.95,
        "observed_at":"2026-08-28T00:00:00Z",
        "clock_id":"wall",
        "source":"camera",
    },
    evidence_refs=["frame://process-aging"],
)
policy = ObservationFreshnessPolicy(
    rules={("camera", "block"): FreshnessThresholds(stale_after_s=10, lost_after_s=20)}
)
snapshot = create_world_state_snapshot(
    "run-process-aging",
    [event],
    freshness_policy=policy,
    aging_boundary=ObservationAgingBoundary(as_of="2026-08-28T00:00:10Z", clock_id="wall"),
)
print(json.dumps({"snapshot": snapshot.model_dump(mode="json"), "state_hash": snapshot.state_hash}, sort_keys=True))
"""
    python_path = os.pathsep.join([str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")])
    outputs = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = python_path
        outputs.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                text=True,
            ).strip()
        )

    assert outputs[0] == outputs[1]
    assert json.loads(outputs[0])["snapshot"]["entities"][0]["belief"] == "stale"


def test_boundary_changes_belief_and_hash_deterministically() -> None:
    event = observation_event("evt-hash-boundary", 1)
    snapshots = {
        "observed": aged_snapshot([event], as_of="2026-08-28T00:00:09Z"),
        "stale": aged_snapshot([event], as_of="2026-08-28T00:00:10Z"),
        "lost": aged_snapshot([event], as_of="2026-08-28T00:00:20Z"),
    }
    replayed_stale = aged_snapshot([event], as_of="2026-08-28T00:00:10Z")

    assert {name: snapshot.entities[0].belief.value for name, snapshot in snapshots.items()} == {
        "observed": "observed",
        "stale": "stale",
        "lost": "lost",
    }
    assert len({snapshot.state_hash for snapshot in snapshots.values()}) == 3
    assert replayed_stale.model_dump_json() == snapshots["stale"].model_dump_json()
