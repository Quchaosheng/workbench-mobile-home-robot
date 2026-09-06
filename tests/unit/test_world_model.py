import sys
import unittest
from itertools import permutations
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

from workbench_contracts import ReasonCode, RecoveryHint, VerificationStatus, WorldEvent, WorldEventType
from workbench_world_model import (
    apply_event,
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


def action_result_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "result_id": "res-001",
        "action_id": "act-001",
        "run_id": "run-001",
        "outcome": "completed",
        "dispatch_state": "sent",
        "device_state": "confirmed",
        "error_code": None,
        "error_reason": None,
        "started_at": "2026-08-04T00:00:00Z",
        "ended_at": "2026-08-04T00:00:01Z",
        "clock_id": "monotonic",
        "retry_count": 0,
        "entity_id": "red_block",
        "resulting_location": "in:tray",
        "evidence_refs": ["act-001"],
    }
    payload.update(updates)
    return payload


def placed_event() -> WorldEvent:
    return WorldEvent(
        event_id="evt-place",
        run_id="run-001",
        sequence_no=1,
        event_type=WorldEventType.ACTION_RESULT,
        occurred_at="2026-08-04T00:00:00Z",
        payload=action_result_payload(),
        evidence_refs=["act-001"],
    )


def observation_event(
    event_id: str,
    sequence_no: int,
    *,
    run_id: str = "run-001",
    entity_id: str = "red_block",
    entity_type: object = "block",
    location: object = "on:table",
    confidence: object = 0.9,
) -> WorldEvent:
    return WorldEvent(
        event_id=event_id,
        run_id=run_id,
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at=f"2026-08-04T00:00:{sequence_no:02d}Z",
        payload={
            "entity_id": entity_id,
            "entity_type": entity_type,
            "location": location,
            "confidence": confidence,
        },
        evidence_refs=[f"frame://{event_id}"],
    )


def location_support_case(rule: str, *, evidence: bool, contradictory: bool = False):
    locations = {"red_block": "in:tray"}
    if rule == "clearance":
        locations["blue_cylinder"] = "in:staging_bin"
    if rule in {"sorting", "policy"}:
        locations = {"parcel": "in:pickup_shelf"}
    events = []
    for entity_id, location in locations.items():
        event = observation_event(
            f"location-{entity_id}",
            len(events),
            entity_id=entity_id,
            location="on:table" if contradictory else location,
        )
        event.evidence_refs = [f"location://{entity_id}"] if evidence else []
        events.append(event)
        appearance = observation_event(f"appearance-{entity_id}", len(events), entity_id=entity_id)
        appearance.payload.pop("location")
        appearance.payload["attributes"] = {"label_status": "verified", "condition": "intact"}
        appearance.evidence_refs = [f"appearance://{entity_id}"]
        events.append(appearance)
    state = reduce_events("run-001", events)

    def verify(candidate):
        if rule == "object":
            return verify_object_in_tray(candidate, "task", "red_block", "tray")
        if rule == "kit":
            return verify_kit_contents(candidate, "task", ["red_block"], "tray")
        if rule == "clearance":
            return verify_workspace_clearance(candidate, "task")
        if rule == "sorting":
            return verify_parcel_sorting(
                candidate,
                "task",
                {"parcel": "pickup_shelf"},
                {"parcel": {"label_status": "verified", "condition": "intact"}},
            )
        return verify_parcel_policy(candidate, "task", ["parcel"])

    return state, verify


@pytest.mark.parametrize("rule", ["object", "kit", "clearance", "sorting", "policy"])
@pytest.mark.parametrize("contradictory", [False, True])
def test_location_support_cannot_be_replaced_by_appearance(rule, contradictory):
    state, verify = location_support_case(rule, evidence=False, contradictory=contradictory)
    before = state.model_dump()
    result = verify(state)
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code is ReasonCode.EVIDENCE_MISSING
    assert result.evidence_refs == ["system://world-state/no-evidence"]
    assert state.model_dump() == before


@pytest.mark.parametrize("rule", ["object", "kit", "clearance", "sorting", "policy"])
@pytest.mark.parametrize("contradictory", [False, True])
def test_location_support_is_used_when_present(rule, contradictory):
    state, verify = location_support_case(rule, evidence=True, contradictory=contradictory)
    result = verify(state)
    expected = VerificationStatus.REFUTED if contradictory else VerificationStatus.CONFIRMED
    assert result.status is expected
    location_refs = {f"location://{entity}" for entity in state.entity_locations}
    assert location_refs <= set(result.evidence_refs)
    if rule in {"object", "kit", "clearance"} or (rule == "sorting" and contradictory):
        assert set(result.evidence_refs) == location_refs


@pytest.mark.parametrize("rule", ["kit", "sorting", "policy"])
@pytest.mark.parametrize("extra_evidence", [False, True])
def test_location_support_is_required_for_extra_entities(rule, extra_evidence):
    state, verify = location_support_case(rule, evidence=True)
    event = observation_event(
        "extra",
        10,
        entity_id="extra",
        location="in:tray" if rule == "kit" else "in:pickup_shelf",
    )
    event.evidence_refs = ["location://extra"] if extra_evidence else []
    state = apply_event(state, event)
    appearance = observation_event("extra-appearance", 11, entity_id="extra")
    appearance.payload.pop("location")
    appearance.evidence_refs = ["appearance://extra"]
    state = apply_event(state, appearance)
    result = verify(state)
    if extra_evidence:
        assert result.status is VerificationStatus.REFUTED
        assert result.evidence_refs == ["location://extra"]
    else:
        assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
        assert result.reason_code is ReasonCode.EVIDENCE_MISSING
        assert result.evidence_refs == ["system://world-state/no-evidence"]


def test_location_support_changes_and_deduplicates_without_appearance():
    state, verify = location_support_case("object", evidence=True)
    new_location = observation_event("new", 10, location="on:table")
    new_location.evidence_refs = ["location://new"]
    state = apply_event(state, new_location)
    confirmation = observation_event("repeat", 11, location="on:table")
    confirmation.evidence_refs = ["location://new", "location://repeat"]
    state = apply_event(state, confirmation)
    appearance = observation_event("color", 12)
    appearance.payload.pop("location")
    appearance.evidence_refs = ["appearance://new"]
    state = apply_event(state, appearance)
    assert state.entity_location_evidence_refs["red_block"] == ["location://new", "location://repeat"]
    assert verify(state).evidence_refs == ["location://new", "location://repeat"]
    assert verify(state).status is VerificationStatus.REFUTED


def test_location_support_does_not_replace_attribute_inspection_evidence():
    state, _ = location_support_case("object", evidence=False)
    result = verify_inspection_evidence(state, "task", ["red_block"])
    assert result.status is VerificationStatus.CONFIRMED
    assert result.evidence_refs == ["appearance://red_block"]


@pytest.mark.parametrize("rule", ["sorting", "policy"])
def test_location_support_is_not_needed_for_attribute_only_refutation(rule):
    state, verify = location_support_case(rule, evidence=False)
    if rule == "sorting":
        state.entity_attributes["parcel"]["condition"] = "damaged"
        result = verify(state)
    else:
        state.entity_attributes["parcel"]["barcode"] = "observed"
        result = verify_parcel_policy(
            state,
            "task",
            ["parcel"],
            parcel_manifest={"parcel": {"barcode": "expected"}},
            manifest_id="manifest",
        )
    assert result.status is VerificationStatus.REFUTED
    assert result.evidence_refs == ["appearance://parcel"]


class WorldModelTests(unittest.TestCase):
    def parcel_state(self) -> WorldState:
        return WorldState(
            run_id="parcel-run",
            entity_locations={
                "parcel_box": "in:pickup_shelf",
                "parcel_envelope": "in:pickup_shelf",
                "parcel_damaged": "in:quarantine_bin",
            },
            entity_confidence={"parcel_box": 0.96, "parcel_envelope": 0.93, "parcel_damaged": 0.91},
            entity_attributes={
                "parcel_box": {"label_status": "verified", "condition": "intact", "tracking_id": "TRK-BOX"},
                "parcel_envelope": {
                    "label_status": "verified",
                    "condition": "intact",
                    "parcel_uid": "TRK-ENV",
                },
                "parcel_damaged": {"label_status": "verified", "condition": "damaged", "barcode": "TRK-DMG"},
            },
            entity_evidence_refs={
                "parcel_box": ["frame://parcel/box", "motion-log://parcel/box"],
                "parcel_envelope": ["frame://parcel/envelope", "motion-log://parcel/envelope"],
                "parcel_damaged": ["frame://parcel/damaged", "motion-log://parcel/damaged"],
            },
            entity_location_evidence_refs={
                "parcel_box": ["frame://parcel/box"],
                "parcel_envelope": ["frame://parcel/envelope"],
                "parcel_damaged": ["frame://parcel/damaged"],
            },
        )

    def test_action_result_replay_is_deterministic_and_idempotent(self) -> None:
        action = placed_event()
        duplicate = action.model_copy(deep=True)
        observation = observation_event("evt-tray", 2, location="in:tray")

        states = [
            reduce_events("run-001", events).model_dump(mode="json")
            for events in (
                [action, duplicate, observation],
                [observation, action, duplicate],
                [duplicate, observation, action],
            )
        ]

        self.assertTrue(all(state == states[0] for state in states))
        self.assertEqual(states[0]["applied_event_ids"], ["evt-place", "evt-tray"])
        self.assertEqual(states[0]["evidence_refs"], ["act-001", "frame://evt-tray"])
        self.assertEqual(states[0]["entity_evidence_refs"]["red_block"], ["frame://evt-tray"])

        once = apply_event(WorldState(run_id="run-001"), action)
        twice = apply_event(once, action)
        self.assertEqual(once.model_dump(), twice.model_dump())

    def test_reduce_events_rejects_empty_run_id_before_apply(self) -> None:
        streams = [[], [observation_event("evt-001", 1, run_id="")]]

        for events in streams:
            with self.subTest(event_count=len(events)):
                with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
                    with self.assertRaisesRegex(ValueError, "run_id"):
                        reduce_events("", events)
                    apply_spy.assert_not_called()

    def test_reduce_events_rejects_mixed_run_before_apply(self) -> None:
        events = [
            observation_event("evt-001", 1),
            observation_event("evt-002", 2, run_id="run-002"),
        ]

        with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
            with self.assertRaisesRegex(ValueError, "run_id"):
                reduce_events("run-001", events)
            apply_spy.assert_not_called()

    def test_reduce_events_rejects_duplicate_sequence_before_apply(self) -> None:
        events = [
            observation_event("evt-001", 1),
            observation_event("evt-002", 1, location="in:tray"),
        ]

        with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
            with self.assertRaisesRegex(ValueError, "sequence_no"):
                reduce_events("run-001", events)
            apply_spy.assert_not_called()

    def test_reduce_events_rejects_conflicting_duplicate_event_id_before_apply(self) -> None:
        original = observation_event("evt-001", 1)
        conflicts = [
            original.model_copy(update={"sequence_no": 2}),
            original.model_copy(update={"occurred_at": "2026-08-04T00:01:00Z"}),
            original.model_copy(
                update={
                    "payload": {
                        "entity_id": "red_block",
                        "entity_type": "block",
                        "location": "in:tray",
                        "confidence": 0.9,
                    }
                }
            ),
            original.model_copy(update={"evidence_refs": ["frame://different"]}),
            original.model_copy(update={"event_type": WorldEventType.FAULT}),
        ]

        for conflict in conflicts:
            with self.subTest(conflict=conflict.model_dump(mode="json")):
                with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
                    with self.assertRaisesRegex(ValueError, "event_id"):
                        reduce_events("run-001", [original, conflict])
                    apply_spy.assert_not_called()

    def test_reduce_events_accepts_exact_duplicate_idempotently(self) -> None:
        original = observation_event("evt-001", 1)
        duplicate = WorldEvent(
            event_id=original.event_id,
            run_id=original.run_id,
            sequence_no=original.sequence_no,
            event_type=original.event_type,
            occurred_at=original.occurred_at,
            payload={
                "confidence": 0.9,
                "location": "on:table",
                "entity_id": "red_block",
                "entity_type": "block",
            },
            evidence_refs=list(original.evidence_refs),
        )

        state = reduce_events("run-001", [original, duplicate])

        self.assertEqual(state.applied_event_ids, ["evt-001"])
        self.assertEqual(state.evidence_refs, ["frame://evt-001"])
        self.assertEqual(state.entity_evidence_refs["red_block"], ["frame://evt-001"])

    def test_reduce_events_orders_unique_events_by_sequence_number(self) -> None:
        events = [
            observation_event("evt-003", 3, entity_id="blue_block"),
            observation_event("evt-001", 1),
            observation_event("evt-002", 2, location="in:tray"),
        ]

        state = reduce_events("run-001", events)

        self.assertEqual(state.applied_event_ids, ["evt-001", "evt-002", "evt-003"])
        self.assertEqual(
            state.evidence_refs,
            ["frame://evt-001", "frame://evt-002", "frame://evt-003"],
        )
        self.assertEqual(state.entity_locations["red_block"], "in:tray")

    def test_reduce_events_is_deterministic_for_every_input_permutation(self) -> None:
        events = [
            observation_event("evt-001", 1),
            observation_event("evt-002", 2, location="in:tray"),
            observation_event("evt-003", 3, entity_id="blue_block"),
        ]
        states = [
            reduce_events("run-001", list(event_order)).model_dump(mode="json") for event_order in permutations(events)
        ]

        self.assertEqual(len(states), 6)
        self.assertTrue(all(state == states[0] for state in states))
        self.assertEqual(states[0]["applied_event_ids"], ["evt-001", "evt-002", "evt-003"])

    def test_reduce_events_preflights_entire_stream_before_apply(self) -> None:
        events = [
            observation_event("evt-001", 1),
            observation_event("evt-002", 2),
            observation_event("evt-003", 2, location="in:tray"),
        ]

        with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
            with self.assertRaises(ValueError):
                reduce_events("run-001", events)
            apply_spy.assert_not_called()

    def test_reduce_events_preflights_all_typed_payloads_before_apply(self) -> None:
        events = [
            observation_event("evt-valid", 1),
            observation_event("evt-invalid", 2, confidence=float("nan")),
        ]

        with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
            with self.assertRaisesRegex(WorldEventPayloadValidationError, "confidence"):
                reduce_events("run-001", events)
            apply_spy.assert_not_called()

    def test_reduce_events_rejects_conflicting_entity_types_before_apply(self) -> None:
        events = [
            observation_event("evt-block", 1, entity_id="shared", entity_type="block"),
            observation_event("evt-tray", 2, entity_id="shared", entity_type="tray"),
        ]

        with patch("workbench_world_model.reducer.apply_event", wraps=apply_event) as apply_spy:
            with self.assertRaisesRegex(ValueError, "entity_type"):
                reduce_events("run-001", events)
            apply_spy.assert_not_called()

    def test_direct_apply_rejects_invalid_payload_without_mutating_state(self) -> None:
        state = WorldState(
            run_id="run-001",
            entity_locations={"existing": "on:table"},
            evidence_refs=["frame://existing"],
        )
        original = state.model_dump_json()

        with self.assertRaisesRegex(WorldEventPayloadValidationError, "location"):
            apply_event(
                state,
                observation_event("evt-invalid", 2, location=["not", "a", "location"]),
            )

        self.assertEqual(state.model_dump_json(), original)

    def test_nan_observation_cannot_reach_verifiers(self) -> None:
        event = observation_event("evt-nan", 1, location="in:kit_tray", confidence=float("nan"))

        with (
            patch(f"{__name__}.verify_kit_contents") as kit_spy,
            patch(f"{__name__}.verify_inspection_evidence") as inspection_spy,
        ):
            with self.assertRaisesRegex(WorldEventPayloadValidationError, "confidence"):
                state = reduce_events("run-001", [event])
                verify_kit_contents(state, "task-kit", ["red_block"])
                verify_inspection_evidence(state, "task-inspection", ["red_block"])

            kit_spy.assert_not_called()
            inspection_spy.assert_not_called()

    def test_reduce_events_accepts_empty_stream_for_valid_run(self) -> None:
        state = reduce_events("run-001", [])

        self.assertEqual(state, WorldState(run_id="run-001"))

    def test_completed_action_result_preserves_observed_state_and_entity_evidence(self) -> None:
        state = WorldState(
            run_id="run-001",
            entity_locations={"red_block": "on:table"},
            entity_confidence={"red_block": 0.91},
            entity_attributes={"red_block": {"colour": "red"}},
            entity_evidence_refs={"red_block": ["frame://before-action"]},
            entity_location_evidence_refs={"red_block": ["frame://before-action"]},
            evidence_refs=["frame://before-action"],
        )
        observed_facts = {
            "entity_locations": state.entity_locations,
            "entity_confidence": state.entity_confidence,
            "entity_attributes": state.entity_attributes,
            "entity_evidence_refs": state.entity_evidence_refs,
        }

        without_spatial_claim = placed_event().model_copy(
            update={
                "event_id": "evt-completed-without-spatial-claim",
                "payload": action_result_payload(
                    result_id="res-completed-without-spatial-claim",
                    entity_id=None,
                    resulting_location=None,
                    evidence_refs=["act-without-spatial-claim"],
                ),
                "evidence_refs": ["act-without-spatial-claim"],
            }
        )

        for action, evidence_ref in (
            (placed_event(), "act-001"),
            (without_spatial_claim, "act-without-spatial-claim"),
        ):
            result = apply_event(state, action)

            with self.subTest(event_id=action.event_id):
                for field_name, expected in observed_facts.items():
                    self.assertEqual(getattr(result, field_name), expected)
                self.assertEqual(result.applied_event_ids, [action.event_id])
                self.assertEqual(result.evidence_refs, ["frame://before-action", evidence_ref])
                self.assertNotIn(evidence_ref, result.entity_evidence_refs["red_block"])

    def test_failed_action_result_preserves_observed_state_and_entity_evidence(self) -> None:
        observed = WorldState(
            run_id="run-001",
            entity_locations={"red_block": "on:table"},
            entity_confidence={"red_block": 0.91},
            entity_attributes={"red_block": {"colour": "red"}},
            entity_evidence_refs={"red_block": ["frame://before-action"]},
            entity_location_evidence_refs={"red_block": ["frame://before-action"]},
            evidence_refs=["frame://before-action"],
        )
        observed_facts = {
            "entity_locations": observed.entity_locations,
            "entity_confidence": observed.entity_confidence,
            "entity_attributes": observed.entity_attributes,
            "entity_evidence_refs": observed.entity_evidence_refs,
        }

        for outcome, device_state in (
            ("failed", "rejected"),
            ("canceled", "stopped"),
            ("safe_stop", "stopped"),
            ("timeout", "unconfirmed"),
        ):
            evidence_ref = f"execution://{outcome}"
            action = placed_event().model_copy(
                update={
                    "event_id": f"evt-{outcome}",
                    "payload": action_result_payload(
                        result_id=f"res-{outcome}",
                        outcome=outcome,
                        device_state=device_state,
                        resulting_location=None,
                        evidence_refs=[evidence_ref],
                    ),
                    "evidence_refs": [evidence_ref],
                }
            )

            result = apply_event(observed, action)

            with self.subTest(outcome=outcome):
                for field_name, expected in observed_facts.items():
                    self.assertEqual(getattr(result, field_name), expected)
                self.assertEqual(result.applied_event_ids, [f"evt-{outcome}"])
                self.assertEqual(result.evidence_refs, ["frame://before-action", evidence_ref])
                self.assertNotIn(evidence_ref, result.entity_evidence_refs["red_block"])

    def test_action_result_without_spatial_observation_is_insufficient(self) -> None:
        state = reduce_events("run-001", [placed_event()])
        result = verify_object_in_tray(state, "task-001", "red_block", "tray")

        self.assertEqual(state.entity_locations, {})
        self.assertEqual(state.entity_evidence_refs, {})
        self.assertEqual(state.evidence_refs, ["act-001"])
        self.assertEqual(state.applied_event_ids, ["evt-place"])
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.reason_code, ReasonCode.TARGET_NOT_OBSERVED)
        self.assertEqual(result.evidence_refs, ["system://world-state/no-evidence"])
        self.assertNotIn("act-001", result.evidence_refs)

    def test_old_observation_cannot_confirm_evidence_free_action_claim(self) -> None:
        state = reduce_events("run-001", [observation_event("evt-table", 0), placed_event()])

        result = verify_object_in_tray(state, "task-001", "red_block", "tray")

        self.assertEqual(state.entity_locations["red_block"], "on:table")
        self.assertEqual(state.entity_evidence_refs["red_block"], ["frame://evt-table"])
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertEqual(result.evidence_refs, ["frame://evt-table"])
        self.assertNotIn("act-001", result.evidence_refs)

    def test_post_action_supporting_observation_confirms_with_sensor_evidence(self) -> None:
        state = reduce_events(
            "run-001",
            [placed_event(), observation_event("evt-tray", 2, location="in:tray")],
        )
        result = verify_object_in_tray(state, "task-001", "red_block", "tray")

        self.assertEqual(result.status, VerificationStatus.CONFIRMED)
        self.assertEqual(result.reason_code, ReasonCode.GOAL_SATISFIED)
        self.assertEqual(result.evidence_refs, ["frame://evt-tray"])
        self.assertEqual(state.entity_evidence_refs["red_block"], ["frame://evt-tray"])
        self.assertEqual(state.evidence_refs, ["act-001", "frame://evt-tray"])
        self.assertNotIn("act-001", result.evidence_refs)

    def test_post_action_contradictory_observation_refutes_with_sensor_evidence(self) -> None:
        state = reduce_events(
            "run-001",
            [placed_event(), observation_event("evt-table", 2, location="on:table")],
        )
        result = verify_object_in_tray(state, "task-001", "red_block", "tray")

        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertEqual(result.reason_code, ReasonCode.GOAL_NOT_SATISFIED)
        self.assertEqual(result.evidence_refs, ["frame://evt-table"])
        self.assertEqual(state.entity_evidence_refs["red_block"], ["frame://evt-table"])
        self.assertEqual(state.evidence_refs, ["act-001", "frame://evt-table"])
        self.assertNotIn("act-001", result.evidence_refs)

    def test_same_location_evidence_accumulates_until_location_changes(self) -> None:
        table_events = [observation_event("evt-table-1", 1), observation_event("evt-table-2", 2)]
        same_location = reduce_events("run-001", table_events)
        state = reduce_events("run-001", [*table_events, observation_event("evt-tray", 3, location="in:tray")])

        self.assertEqual(
            same_location.entity_evidence_refs["red_block"],
            ["frame://evt-table-1", "frame://evt-table-2"],
        )
        self.assertEqual(state.entity_evidence_refs["red_block"], ["frame://evt-tray"])
        self.assertEqual(
            verify_object_in_tray(state, "task-001", "red_block", "tray").evidence_refs,
            ["frame://evt-tray"],
        )

    def test_object_verifier_requires_claimed_object_evidence(self) -> None:
        state = WorldState(
            run_id="object-missing-evidence",
            entity_locations={"red_block": "in:tray", "wrong_entity": "on:table"},
            entity_evidence_refs={"wrong_entity": ["evidence://wrong/one", "evidence://wrong/two"]},
            entity_location_evidence_refs={"wrong_entity": ["evidence://wrong/one", "evidence://wrong/two"]},
            evidence_refs=["evidence://global"],
        )

        result = verify_object_in_tray(state, "task-object", "red_block", "tray")

        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.reason_code, ReasonCode.EVIDENCE_MISSING)
        self.assertEqual(result.recovery_hint, RecoveryHint.RE_OBSERVE)
        self.assertEqual(result.evidence_refs, ["system://world-state/no-evidence"])

        unobserved = WorldState(
            run_id="object-unobserved",
            entity_locations={"wrong_entity": "in:tray"},
            entity_evidence_refs={"wrong_entity": ["evidence://wrong/abundant"]},
            entity_location_evidence_refs={"wrong_entity": ["evidence://wrong/abundant"]},
            evidence_refs=["evidence://global/abundant"],
        )
        result = verify_object_in_tray(unobserved, "task-object", "red_block", "tray")

        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(result.reason_code, ReasonCode.TARGET_NOT_OBSERVED)
        self.assertEqual(result.recovery_hint, RecoveryHint.RE_OBSERVE)
        self.assertEqual(result.evidence_refs, ["system://world-state/no-evidence"])

    def test_object_verifier_returns_only_claimed_object_support(self) -> None:
        state = WorldState(
            run_id="object-bound-evidence",
            entity_locations={"red_block": "in:tray", "tray": "on:table", "wrong_entity": "on:table"},
            entity_evidence_refs={
                "tray": ["evidence://tray"],
                "wrong_entity": ["evidence://wrong"],
                "red_block": ["evidence://object/observation", "evidence://object/action"],
            },
            entity_location_evidence_refs={
                "tray": ["evidence://tray"],
                "wrong_entity": ["evidence://wrong"],
                "red_block": ["evidence://object/observation"],
            },
            evidence_refs=["evidence://global"],
        )

        result = verify_object_in_tray(state, "task-object", "red_block", "tray")

        self.assertEqual(result.status, VerificationStatus.CONFIRMED)
        self.assertEqual(result.reason_code, ReasonCode.GOAL_SATISFIED)
        self.assertEqual(result.recovery_hint, RecoveryHint.NONE)
        self.assertEqual(
            result.evidence_refs,
            ["evidence://object/observation"],
        )

    def test_multi_entity_verifiers_exclude_unrelated_evidence(self) -> None:
        kit_state = WorldState(
            run_id="kit-evidence-scope",
            entity_locations={
                "part-b": "in:kit_tray",
                "unrelated": "on:table",
                "part-a": "in:kit_tray",
            },
            entity_confidence={"part-a": 0.96, "part-b": 0.95},
            entity_evidence_refs={
                "unrelated": ["evidence://unrelated"],
                "part-b": ["evidence://kit/b"],
                "part-a": ["evidence://kit/a"],
            },
            entity_location_evidence_refs={
                "unrelated": ["evidence://unrelated"],
                "part-b": ["evidence://kit/b"],
                "part-a": ["evidence://kit/a"],
            },
            evidence_refs=["evidence://global"],
        )
        inspection_state = WorldState(
            run_id="inspection-evidence-scope",
            entity_locations={"sensor-a": "on:table", "sensor-b": "on:table", "unrelated": "on:table"},
            entity_confidence={"sensor-a": 0.95, "sensor-b": 0.2},
            entity_evidence_refs={
                "sensor-a": ["evidence://inspection/a"],
                "sensor-b": ["evidence://inspection/b-low-confidence"],
                "unrelated": ["evidence://unrelated"],
            },
            entity_location_evidence_refs={
                "sensor-a": ["evidence://inspection/a"],
                "sensor-b": ["evidence://inspection/b-low-confidence"],
                "unrelated": ["evidence://unrelated"],
            },
            evidence_refs=["evidence://global"],
        )
        clearance_state = WorldState(
            run_id="clearance-evidence-scope",
            entity_locations={
                "red_block": "in:tray",
                "unrelated": "on:table",
                "blue_cylinder": "in:staging_bin",
            },
            entity_evidence_refs={
                "red_block": ["evidence://clear/red"],
                "unrelated": ["evidence://unrelated"],
                "blue_cylinder": ["evidence://clear/blue"],
            },
            entity_location_evidence_refs={
                "red_block": ["evidence://clear/red"],
                "unrelated": ["evidence://unrelated"],
                "blue_cylinder": ["evidence://clear/blue"],
            },
            evidence_refs=["evidence://global"],
        )
        parcel_sorting_state = WorldState(
            run_id="parcel-sorting-evidence-scope",
            entity_locations={
                "parcel-a": "in:pickup",
                "parcel-b": "in:pickup",
                "unrelated": "on:table",
            },
            entity_confidence={"parcel-a": 0.95, "parcel-b": 0.95},
            entity_attributes={"parcel-a": {}, "parcel-b": {"label_status": "verified"}},
            entity_evidence_refs={
                "parcel-b": ["evidence://parcel/b"],
                "unrelated": ["evidence://unrelated"],
                "parcel-a": ["evidence://parcel/a-missing-attribute"],
            },
            entity_location_evidence_refs={
                "parcel-b": ["evidence://parcel/b"],
                "unrelated": ["evidence://unrelated"],
                "parcel-a": ["evidence://parcel/a-missing-attribute"],
            },
            evidence_refs=["evidence://global"],
        )
        parcel_policy_state = WorldState(
            run_id="parcel-policy-evidence-scope",
            entity_locations={"box-b": "in:pickup_shelf", "unrelated": "on:table", "box-a": "in:pickup_shelf"},
            entity_confidence={"box-a": 0.95, "box-b": 0.95},
            entity_attributes={
                "box-a": {"label_status": "verified", "condition": "intact"},
                "box-b": {"label_status": "verified", "condition": "intact"},
            },
            entity_evidence_refs={
                "box-b": ["evidence://policy/b"],
                "unrelated": ["evidence://unrelated"],
                "box-a": ["evidence://policy/a"],
            },
            entity_location_evidence_refs={
                "box-b": ["evidence://policy/b"],
                "unrelated": ["evidence://unrelated"],
                "box-a": ["evidence://policy/a"],
            },
            evidence_refs=["evidence://global"],
        )

        cases = [
            (
                "kit-confirmed",
                verify_kit_contents(kit_state, "task-kit", ["part-b", "part-a"]),
                VerificationStatus.CONFIRMED,
                ReasonCode.GOAL_SATISFIED,
                RecoveryHint.NONE,
                ["evidence://kit/a", "evidence://kit/b"],
            ),
            (
                "inspection-low-confidence",
                verify_inspection_evidence(inspection_state, "task-inspection", ["sensor-a", "sensor-b"]),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
                RecoveryHint.RE_OBSERVE,
                ["evidence://inspection/b-low-confidence"],
            ),
            (
                "clearance-confirmed",
                verify_workspace_clearance(clearance_state, "task-clearance"),
                VerificationStatus.CONFIRMED,
                ReasonCode.GOAL_SATISFIED,
                RecoveryHint.NONE,
                ["evidence://clear/blue", "evidence://clear/red"],
            ),
            (
                "parcel-sorting-missing-attribute",
                verify_parcel_sorting(
                    parcel_sorting_state,
                    "task-parcel-sorting",
                    parcel_routes={"parcel-b": "pickup", "parcel-a": "pickup"},
                    expected_attributes={
                        "parcel-a": {"label_status": "verified"},
                        "parcel-b": {"label_status": "verified"},
                    },
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.EVIDENCE_MISSING,
                RecoveryHint.RE_OBSERVE,
                ["evidence://parcel/a-missing-attribute"],
            ),
            (
                "parcel-policy-confirmed",
                verify_parcel_policy(parcel_policy_state, "task-parcel-policy", ["box-b", "box-a"]),
                VerificationStatus.CONFIRMED,
                ReasonCode.GOAL_SATISFIED,
                RecoveryHint.NONE,
                ["evidence://policy/a", "evidence://policy/b"],
            ),
        ]

        for name, result, status, reason_code, recovery_hint, evidence_refs in cases:
            with self.subTest(name=name):
                self.assertEqual(result.status, status)
                self.assertEqual(result.reason_code, reason_code)
                self.assertEqual(result.recovery_hint, recovery_hint)
                self.assertEqual(result.evidence_refs, evidence_refs)

        def scoped_state(
            run_id: str,
            locations: dict[str, str],
            *,
            confidences: dict[str, float] | None = None,
            attributes: dict[str, dict[str, str]] | None = None,
            entity_evidence: dict[str, list[str]] | None = None,
        ) -> WorldState:
            return WorldState(
                run_id=run_id,
                entity_locations={"unrelated": "on:table", **locations},
                entity_confidence=confidences or {},
                entity_attributes=attributes or {},
                entity_evidence_refs={
                    "unrelated": ["evidence://unrelated"],
                    **(entity_evidence or {}),
                },
                entity_location_evidence_refs={
                    "unrelated": ["evidence://unrelated"],
                    **(entity_evidence or {}),
                },
                evidence_refs=["evidence://global"],
            )

        remaining_cases = [
            (
                "kit-unobserved",
                verify_kit_contents(
                    scoped_state(
                        "kit-unobserved",
                        {"part-b": "in:kit_tray"},
                        confidences={"part-b": 0.95},
                        entity_evidence={"part-b": ["evidence://kit/b-correct"]},
                    ),
                    "task-kit",
                    ["part-a", "part-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.TARGET_NOT_OBSERVED,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "kit-low-confidence",
                verify_kit_contents(
                    scoped_state(
                        "kit-low-confidence",
                        {"part-a": "in:kit_tray", "part-b": "in:kit_tray"},
                        confidences={"part-a": 0.2, "part-b": 0.95},
                        entity_evidence={
                            "part-a": ["evidence://kit/a-low-confidence"],
                            "part-b": ["evidence://kit/b-correct"],
                        },
                    ),
                    "task-kit",
                    ["part-a", "part-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
                RecoveryHint.RE_OBSERVE,
                ["evidence://kit/a-low-confidence"],
            ),
            (
                "kit-missing-evidence",
                verify_kit_contents(
                    scoped_state(
                        "kit-missing-evidence",
                        {"part-a": "in:kit_tray", "part-b": "in:kit_tray"},
                        confidences={"part-a": 0.95, "part-b": 0.95},
                        entity_evidence={"part-b": ["evidence://kit/b-correct"]},
                    ),
                    "task-kit",
                    ["part-a", "part-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.EVIDENCE_MISSING,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "inspection-unobserved",
                verify_inspection_evidence(
                    scoped_state(
                        "inspection-unobserved",
                        {"sensor-b": "on:table"},
                        confidences={"sensor-b": 0.95},
                        entity_evidence={"sensor-b": ["evidence://inspection/b-correct"]},
                    ),
                    "task-inspection",
                    ["sensor-a", "sensor-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.TARGET_NOT_OBSERVED,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "inspection-missing-evidence",
                verify_inspection_evidence(
                    scoped_state(
                        "inspection-missing-evidence",
                        {"sensor-a": "on:table", "sensor-b": "on:table"},
                        confidences={"sensor-a": 0.95, "sensor-b": 0.95},
                        entity_evidence={"sensor-b": ["evidence://inspection/b-correct"]},
                    ),
                    "task-inspection",
                    ["sensor-a", "sensor-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.EVIDENCE_MISSING,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "clearance-unobserved",
                verify_workspace_clearance(
                    scoped_state(
                        "clearance-unobserved",
                        {"blue_cylinder": "in:staging_bin"},
                        entity_evidence={"blue_cylinder": ["evidence://clearance/blue-correct"]},
                    ),
                    "task-clearance",
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.TARGET_NOT_OBSERVED,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "clearance-missing-evidence",
                verify_workspace_clearance(
                    scoped_state(
                        "clearance-missing-evidence",
                        {"blue_cylinder": "in:staging_bin", "red_block": "in:tray"},
                        entity_evidence={"blue_cylinder": ["evidence://clearance/blue-correct"]},
                    ),
                    "task-clearance",
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.EVIDENCE_MISSING,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "parcel-sorting-unobserved",
                verify_parcel_sorting(
                    scoped_state(
                        "parcel-sorting-unobserved",
                        {"parcel-b": "in:pickup"},
                        confidences={"parcel-b": 0.95},
                        attributes={"parcel-b": {"label_status": "verified"}},
                        entity_evidence={"parcel-b": ["evidence://parcel/b-correct"]},
                    ),
                    "task-parcel-sorting",
                    parcel_routes={"parcel-a": "pickup", "parcel-b": "pickup"},
                    expected_attributes={
                        "parcel-a": {"label_status": "verified"},
                        "parcel-b": {"label_status": "verified"},
                    },
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.TARGET_NOT_OBSERVED,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "parcel-sorting-low-confidence",
                verify_parcel_sorting(
                    scoped_state(
                        "parcel-sorting-low-confidence",
                        {"parcel-a": "in:pickup", "parcel-b": "in:pickup"},
                        confidences={"parcel-a": 0.2, "parcel-b": 0.95},
                        attributes={
                            "parcel-a": {"label_status": "verified"},
                            "parcel-b": {"label_status": "verified"},
                        },
                        entity_evidence={
                            "parcel-a": ["evidence://parcel/a-low-confidence"],
                            "parcel-b": ["evidence://parcel/b-correct"],
                        },
                    ),
                    "task-parcel-sorting",
                    parcel_routes={"parcel-a": "pickup", "parcel-b": "pickup"},
                    expected_attributes={
                        "parcel-a": {"label_status": "verified"},
                        "parcel-b": {"label_status": "verified"},
                    },
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
                RecoveryHint.RE_OBSERVE,
                ["evidence://parcel/a-low-confidence"],
            ),
            (
                "parcel-sorting-missing-support",
                verify_parcel_sorting(
                    scoped_state(
                        "parcel-sorting-missing-support",
                        {
                            "parcel-a": "in:pickup",
                            "parcel-b": "in:pickup",
                            "parcel-c": "in:pickup",
                        },
                        confidences={"parcel-a": 0.95, "parcel-b": 0.95, "parcel-c": 0.95},
                        attributes={
                            "parcel-a": {"label_status": "verified", "condition": "intact"},
                            "parcel-b": {"label_status": "verified"},
                            "parcel-c": {"label_status": "verified", "condition": "intact"},
                        },
                        entity_evidence={
                            "parcel-b": ["evidence://parcel/b-missing-attribute"],
                            "parcel-c": ["evidence://parcel/c-correct"],
                        },
                    ),
                    "task-parcel-sorting",
                    parcel_routes={"parcel-a": "pickup", "parcel-b": "pickup", "parcel-c": "pickup"},
                    expected_attributes={
                        "parcel-a": {"label_status": "verified", "condition": "intact"},
                        "parcel-b": {"label_status": "verified", "condition": "intact"},
                        "parcel-c": {"label_status": "verified", "condition": "intact"},
                    },
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.EVIDENCE_MISSING,
                RecoveryHint.RE_OBSERVE,
                ["evidence://parcel/b-missing-attribute"],
            ),
            (
                "parcel-sorting-confirmed",
                verify_parcel_sorting(
                    scoped_state(
                        "parcel-sorting-confirmed",
                        {"parcel-a": "in:pickup", "parcel-b": "in:pickup"},
                        confidences={"parcel-a": 0.95, "parcel-b": 0.95},
                        attributes={
                            "parcel-a": {"label_status": "verified"},
                            "parcel-b": {"label_status": "verified"},
                        },
                        entity_evidence={
                            "parcel-b": ["evidence://parcel/b"],
                            "parcel-a": ["evidence://parcel/a"],
                        },
                    ),
                    "task-parcel-sorting",
                    parcel_routes={"parcel-b": "pickup", "parcel-a": "pickup"},
                    expected_attributes={
                        "parcel-a": {"label_status": "verified"},
                        "parcel-b": {"label_status": "verified"},
                    },
                ),
                VerificationStatus.CONFIRMED,
                ReasonCode.GOAL_SATISFIED,
                RecoveryHint.NONE,
                ["evidence://parcel/a", "evidence://parcel/b"],
            ),
            (
                "parcel-policy-unobserved",
                verify_parcel_policy(
                    scoped_state(
                        "parcel-policy-unobserved",
                        {"box-b": "in:pickup_shelf"},
                        confidences={"box-b": 0.95},
                        attributes={"box-b": {"label_status": "verified", "condition": "intact"}},
                        entity_evidence={"box-b": ["evidence://policy/b-correct"]},
                    ),
                    "task-parcel-policy",
                    ["box-a", "box-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.TARGET_NOT_OBSERVED,
                RecoveryHint.RE_OBSERVE,
                ["system://world-state/no-evidence"],
            ),
            (
                "parcel-policy-low-confidence",
                verify_parcel_policy(
                    scoped_state(
                        "parcel-policy-low-confidence",
                        {"box-a": "in:pickup_shelf", "box-b": "in:pickup_shelf"},
                        confidences={"box-a": 0.2, "box-b": 0.95},
                        attributes={
                            "box-a": {"label_status": "verified", "condition": "intact"},
                            "box-b": {"label_status": "verified", "condition": "intact"},
                        },
                        entity_evidence={
                            "box-a": ["evidence://policy/a-low-confidence"],
                            "box-b": ["evidence://policy/b-correct"],
                        },
                    ),
                    "task-parcel-policy",
                    ["box-a", "box-b"],
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
                RecoveryHint.RE_OBSERVE,
                ["evidence://policy/a-low-confidence"],
            ),
            (
                "parcel-policy-missing-support",
                verify_parcel_policy(
                    scoped_state(
                        "parcel-policy-missing-support",
                        {
                            "box-a": "in:pickup_shelf",
                            "box-b": "in:pickup_shelf",
                            "box-c": "in:pickup_shelf",
                            "box-d": "in:pickup_shelf",
                        },
                        confidences={"box-a": 0.95, "box-b": 0.95, "box-c": 0.95, "box-d": 0.95},
                        attributes={
                            "box-a": {
                                "label_status": "verified",
                                "condition": "intact",
                                "tracking_id": "TRACK-A",
                            },
                            "box-b": {"label_status": "verified", "condition": "intact"},
                            "box-c": {"label_status": "verified", "tracking_id": "TRACK-C"},
                            "box-d": {
                                "label_status": "verified",
                                "condition": "intact",
                                "tracking_id": "TRACK-D",
                            },
                        },
                        entity_evidence={
                            "box-b": ["evidence://policy/b-missing-identity"],
                            "box-c": ["evidence://policy/c-missing-attribute"],
                            "box-d": ["evidence://policy/d-correct"],
                        },
                    ),
                    "task-parcel-policy",
                    ["box-a", "box-b", "box-c", "box-d"],
                    parcel_manifest={
                        "box-a": {"tracking_id": "TRACK-A"},
                        "box-b": {"tracking_id": "TRACK-B"},
                        "box-c": {"tracking_id": "TRACK-C"},
                        "box-d": {"tracking_id": "TRACK-D"},
                    },
                    manifest_id="manifest-evidence-scope",
                ),
                VerificationStatus.INSUFFICIENT_EVIDENCE,
                ReasonCode.EVIDENCE_MISSING,
                RecoveryHint.RE_OBSERVE,
                ["evidence://policy/b-missing-identity", "evidence://policy/c-missing-attribute"],
            ),
        ]

        for name, result, status, reason_code, recovery_hint, evidence_refs in remaining_cases:
            with self.subTest(name=name):
                self.assertEqual(result.status, status)
                self.assertEqual(result.reason_code, reason_code)
                self.assertEqual(result.recovery_hint, recovery_hint)
                self.assertEqual(result.evidence_refs, evidence_refs)

    def test_refuted_verifiers_return_refuting_entity_evidence(self) -> None:
        object_state = WorldState(
            run_id="object-refuted",
            entity_locations={"red_block": "on:table", "unrelated": "in:tray"},
            entity_evidence_refs={
                "unrelated": ["evidence://unrelated"],
                "red_block": ["evidence://object/refute"],
            },
            entity_location_evidence_refs={
                "unrelated": ["evidence://unrelated"],
                "red_block": ["evidence://object/refute"],
            },
            evidence_refs=["evidence://global"],
        )
        kit_state = WorldState(
            run_id="kit-refuted",
            entity_locations={
                "part-a": "on:table",
                "part-b": "in:kit_tray",
                "extra-part": "in:kit_tray",
                "unrelated": "on:table",
            },
            entity_confidence={"part-a": 0.95, "part-b": 0.95},
            entity_evidence_refs={
                "part-b": ["evidence://kit/b-correct"],
                "part-a": ["evidence://kit/a-refute"],
                "extra-part": ["evidence://kit/extra-refute"],
                "unrelated": ["evidence://unrelated"],
            },
            entity_location_evidence_refs={
                "part-b": ["evidence://kit/b-correct"],
                "part-a": ["evidence://kit/a-refute"],
                "extra-part": ["evidence://kit/extra-refute"],
                "unrelated": ["evidence://unrelated"],
            },
            evidence_refs=["evidence://global"],
        )
        clearance_state = WorldState(
            run_id="clearance-refuted",
            entity_locations={"red_block": "in:tray", "blue_cylinder": "on:table"},
            entity_evidence_refs={
                "red_block": ["evidence://clear/red-correct"],
                "blue_cylinder": ["evidence://clear/blue-refute"],
            },
            entity_location_evidence_refs={
                "red_block": ["evidence://clear/red-correct"],
                "blue_cylinder": ["evidence://clear/blue-refute"],
            },
            evidence_refs=["evidence://global"],
        )
        parcel_sorting_state = WorldState(
            run_id="parcel-sorting-refuted",
            entity_locations={
                "parcel-a": "on:intake",
                "parcel-b": "in:pickup",
                "unexpected": "in:pickup",
            },
            entity_confidence={"parcel-a": 0.95, "parcel-b": 0.95},
            entity_attributes={
                "parcel-a": {"label_status": "verified"},
                "parcel-b": {"label_status": "verified"},
            },
            entity_evidence_refs={
                "parcel-b": ["evidence://parcel/b-correct"],
                "unexpected": ["evidence://parcel/extra-refute"],
                "parcel-a": ["evidence://parcel/a-refute"],
            },
            entity_location_evidence_refs={
                "parcel-b": ["evidence://parcel/b-correct"],
                "unexpected": ["evidence://parcel/extra-refute"],
                "parcel-a": ["evidence://parcel/a-refute"],
            },
            evidence_refs=["evidence://global"],
        )
        parcel_policy_state = WorldState(
            run_id="parcel-policy-refuted",
            entity_locations={
                "box-a": "in:pickup_shelf",
                "box-b": "in:pickup_shelf",
                "foreign": "in:quarantine_bin",
            },
            entity_confidence={"box-a": 0.95, "box-b": 0.95},
            entity_attributes={
                "box-a": {"label_status": "verified", "condition": "intact"},
                "box-b": {"label_status": "unreadable", "condition": "intact"},
            },
            entity_evidence_refs={
                "box-a": ["evidence://policy/a-correct"],
                "foreign": ["evidence://policy/extra-refute"],
                "box-b": ["evidence://policy/b-refute"],
            },
            entity_location_evidence_refs={
                "box-a": ["evidence://policy/a-correct"],
                "foreign": ["evidence://policy/extra-refute"],
                "box-b": ["evidence://policy/b-refute"],
            },
            evidence_refs=["evidence://global"],
        )

        cases = [
            (
                "object",
                verify_object_in_tray(object_state, "task-object", "red_block", "tray"),
                ["evidence://object/refute"],
            ),
            (
                "kit",
                verify_kit_contents(kit_state, "task-kit", ["part-a", "part-b"]),
                ["evidence://kit/extra-refute", "evidence://kit/a-refute"],
            ),
            (
                "clearance",
                verify_workspace_clearance(clearance_state, "task-clearance"),
                ["evidence://clear/blue-refute"],
            ),
            (
                "parcel-sorting",
                verify_parcel_sorting(
                    parcel_sorting_state,
                    "task-parcel-sorting",
                    parcel_routes={"parcel-a": "pickup", "parcel-b": "pickup"},
                    expected_attributes={
                        "parcel-a": {"label_status": "verified"},
                        "parcel-b": {"label_status": "verified"},
                    },
                ),
                ["evidence://parcel/a-refute", "evidence://parcel/extra-refute"],
            ),
            (
                "parcel-policy",
                verify_parcel_policy(parcel_policy_state, "task-parcel-policy", ["box-a", "box-b"]),
                ["evidence://policy/b-refute", "evidence://policy/extra-refute"],
            ),
        ]

        for name, result, evidence_refs in cases:
            with self.subTest(name=name):
                self.assertEqual(result.status, VerificationStatus.REFUTED)
                self.assertEqual(result.reason_code, ReasonCode.GOAL_NOT_SATISFIED)
                self.assertEqual(result.recovery_hint, RecoveryHint.RETRY_ACTION)
                self.assertEqual(result.evidence_refs, evidence_refs)

    def test_manifest_mismatch_returns_the_parcel_evidence(self) -> None:
        state = WorldState(
            run_id="manifest-mismatch",
            entity_locations={"box-a": "in:pickup_shelf"},
            entity_confidence={"box-a": 0.95},
            entity_attributes={
                "box-a": {
                    "label_status": "verified",
                    "condition": "intact",
                    "barcode": "OBSERVED-A",
                }
            },
            entity_evidence_refs={"box-a": ["frame://box-a"]},
            entity_location_evidence_refs={"box-a": ["frame://box-a"]},
        )

        result = verify_parcel_policy(
            state,
            "task-parcel-policy",
            ["box-a"],
            parcel_manifest={"box-a": {"barcode": "EXPECTED-A"}},
            manifest_id="manifest-1",
        )

        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertIn("manifest_mismatches=['box-a']", result.claim)
        self.assertEqual(result.evidence_refs, ["frame://box-a"])

    def test_verifier_evidence_refs_are_deduplicated_deterministically(self) -> None:
        state_a = WorldState(
            run_id="deterministic-evidence-a",
            entity_locations={"alpha": "on:table", "zeta": "on:table", "unrelated": "on:table"},
            entity_confidence={"alpha": 0.95, "zeta": 0.95},
            entity_evidence_refs={
                "alpha": ["evidence://shared", "evidence://alpha", "evidence://shared"],
                "zeta": ["evidence://zeta", "evidence://shared", "evidence://zeta"],
                "unrelated": ["evidence://unrelated"],
            },
            entity_location_evidence_refs={
                "alpha": ["evidence://shared", "evidence://alpha", "evidence://shared"],
                "zeta": ["evidence://zeta", "evidence://shared", "evidence://zeta"],
                "unrelated": ["evidence://unrelated"],
            },
            evidence_refs=["evidence://global"],
        )
        state_b = WorldState(
            run_id="deterministic-evidence-b",
            entity_locations={"zeta": "on:table", "unrelated": "on:table", "alpha": "on:table"},
            entity_confidence={"zeta": 0.95, "alpha": 0.95},
            entity_evidence_refs={
                "zeta": ["evidence://zeta", "evidence://shared", "evidence://zeta"],
                "unrelated": ["evidence://unrelated"],
                "alpha": ["evidence://shared", "evidence://alpha", "evidence://shared"],
            },
            entity_location_evidence_refs={
                "zeta": ["evidence://zeta", "evidence://shared", "evidence://zeta"],
                "unrelated": ["evidence://unrelated"],
                "alpha": ["evidence://shared", "evidence://alpha", "evidence://shared"],
            },
            evidence_refs=["evidence://global"],
        )

        evidence_lists = [
            verify_inspection_evidence(state, "task-inspection", ["zeta", "alpha"]).evidence_refs
            for state in (state_a, state_b)
        ]

        self.assertEqual(
            evidence_lists,
            [
                ["evidence://shared", "evidence://alpha", "evidence://zeta"],
                ["evidence://shared", "evidence://alpha", "evidence://zeta"],
            ],
        )

    def test_kitting_verifier_checks_all_parts_extras_confidence_and_evidence(self) -> None:
        state = WorldState(
            run_id="kit-run",
            entity_locations={
                "red_block": "in:kit_tray",
                "blue_cylinder": "in:kit_tray",
                "green_gear": "in:kit_tray",
            },
            entity_confidence={"red_block": 0.96, "blue_cylinder": 0.94, "green_gear": 0.91},
            entity_evidence_refs={
                "red_block": ["frame://kit/red"],
                "blue_cylinder": ["frame://kit/blue"],
                "green_gear": ["frame://kit/green"],
            },
            entity_location_evidence_refs={
                "red_block": ["frame://kit/red"],
                "blue_cylinder": ["frame://kit/blue"],
                "green_gear": ["frame://kit/green"],
            },
            evidence_refs=["frame://kit/final"],
        )
        required = ["red_block", "blue_cylinder", "green_gear"]
        self.assertTrue(verify_kit_contents(state, "task-kit-three-parts", required).completed)
        state.entity_locations["wrong_part"] = "in:kit_tray"
        state.entity_location_evidence_refs["wrong_part"] = ["frame://kit/wrong-part"]
        result = verify_kit_contents(state, "task-kit-three-parts", required)
        self.assertFalse(result.completed)
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertIn("wrong_part", result.claim)

        state.entity_locations.pop("wrong_part")
        state.entity_location_evidence_refs.pop("blue_cylinder")
        result = verify_kit_contents(state, "task-kit-three-parts", required)
        self.assertFalse(result.completed)
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("blue_cylinder", result.claim)

        for invalid_required in ([], ["red_block", "red_block"], [""]):
            with self.subTest(required=invalid_required), self.assertRaises(ValueError):
                verify_kit_contents(state, "task-kit-three-parts", invalid_required)
        with self.assertRaises(ValueError):
            verify_kit_contents(state, "task-kit-three-parts", required, confidence_threshold=1.1)

    def test_inspection_and_clearance_verifiers_require_evidence(self) -> None:
        inspection = WorldState(
            run_id="inspect-run",
            entity_locations={"red_block": "on:table", "blue_cylinder": "on:table"},
            entity_confidence={"red_block": 0.95, "blue_cylinder": 0.42},
            entity_evidence_refs={
                "red_block": ["frame://inspect/red"],
                "blue_cylinder": ["frame://inspect/blue"],
            },
            entity_location_evidence_refs={
                "red_block": ["frame://inspect/red"],
                "blue_cylinder": ["frame://inspect/blue"],
            },
            evidence_refs=["frame://inspect/001"],
        )
        result = verify_inspection_evidence(
            inspection,
            "task-inspect-workpieces",
            ["red_block", "blue_cylinder"],
        )
        self.assertFalse(result.completed)
        self.assertEqual(result.reason_code, ReasonCode.CONFIDENCE_BELOW_THRESHOLD)
        self.assertIn("blue_cylinder", result.claim)

        clearance = WorldState(
            run_id="clear-run",
            entity_locations={"blue_cylinder": "in:staging_bin", "red_block": "in:tray"},
            entity_evidence_refs={
                "blue_cylinder": ["frame://clear/blue"],
                "red_block": ["frame://clear/red"],
            },
            entity_location_evidence_refs={
                "blue_cylinder": ["frame://clear/blue"],
                "red_block": ["frame://clear/red"],
            },
            evidence_refs=["frame://clear/final"],
        )
        self.assertTrue(verify_workspace_clearance(clearance, "task-clear-workspace").completed)
        clearance.entity_location_evidence_refs.pop("red_block")
        self.assertFalse(verify_workspace_clearance(clearance, "task-clear-workspace").completed)

    def test_observation_reducer_preserves_parcel_attributes(self) -> None:
        event = WorldEvent(
            event_id="evt-parcel-observe",
            run_id="parcel-run",
            sequence_no=0,
            event_type=WorldEventType.OBSERVATION,
            occurred_at="2026-08-07T00:00:00Z",
            payload={
                "entity_id": "parcel_damaged",
                "entity_type": "parcel",
                "location": "on:intake_table",
                "confidence": 0.92,
                "attributes": {"label_status": "verified", "condition": "damaged"},
            },
            evidence_refs=["frame://parcel/damaged"],
        )
        state = reduce_events("parcel-run", [event])
        self.assertEqual(
            state.entity_attributes["parcel_damaged"],
            {"label_status": "verified", "condition": "damaged"},
        )

    def test_parcel_verifier_checks_attributes_routes_extras_and_evidence(self) -> None:
        state = self.parcel_state()
        result = verify_parcel_sorting(state, "task-sort-parcels")
        self.assertEqual(result.status, VerificationStatus.CONFIRMED)

        state.entity_attributes["parcel_box"].pop("label_status")
        result = verify_parcel_sorting(state, "task-sort-parcels")
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("parcel_box.label_status", result.claim)

        state = self.parcel_state()
        state.entity_attributes["parcel_damaged"]["condition"] = "intact"
        result = verify_parcel_sorting(state, "task-sort-parcels")
        self.assertEqual(result.status, VerificationStatus.REFUTED)

        state = self.parcel_state()
        state.entity_locations["parcel_damaged"] = "in:pickup_shelf"
        result = verify_parcel_sorting(state, "task-sort-parcels")
        self.assertEqual(result.status, VerificationStatus.REFUTED)

        state = self.parcel_state()
        state.entity_locations["unregistered_parcel"] = "in:pickup_shelf"
        state.entity_location_evidence_refs["unregistered_parcel"] = ["frame://parcel/unregistered"]
        result = verify_parcel_sorting(state, "task-sort-parcels")
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertIn("unregistered_parcel", result.claim)

        state = self.parcel_state()
        state.entity_evidence_refs.pop("parcel_envelope")
        self.assertEqual(
            verify_parcel_sorting(state, "task-sort-parcels").status,
            VerificationStatus.INSUFFICIENT_EVIDENCE,
        )

        state = self.parcel_state()
        state.entity_confidence["parcel_box"] = float("nan")
        self.assertEqual(
            verify_parcel_sorting(state, "task-sort-parcels").reason_code,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
        )

    def test_parcel_verifier_rejects_malformed_requirements(self) -> None:
        state = self.parcel_state()
        with self.assertRaises(ValueError):
            verify_parcel_sorting(state, "task-sort-parcels", parcel_routes=[])
        with self.assertRaises(ValueError):
            verify_parcel_sorting(state, "task-sort-parcels", expected_attributes={})
        with self.assertRaises(ValueError):
            verify_parcel_sorting(
                state,
                "task-sort-parcels",
                expected_attributes={
                    "parcel_box": {},
                    "parcel_envelope": {"label_status": "verified"},
                    "parcel_damaged": {"label_status": "verified"},
                },
            )

    def test_parcel_policy_derives_exception_destinations_from_observations(self) -> None:
        manifest = {
            "box-a": {"tracking_id": "TRACK-A"},
            "box-b": {"parcel_uid": "TRACK-B"},
            "box-c": {"tracking_id": "TRACK-C"},
        }
        state = WorldState(
            run_id="parcel-policy-run",
            entity_locations={
                "box-a": "in:pickup_shelf",
                "box-b": "in:quarantine_bin",
                "box-c": "in:quarantine_bin",
            },
            entity_confidence={"box-a": 0.95, "box-b": 0.94, "box-c": 0.93},
            entity_attributes={
                "box-a": {"label_status": "verified", "condition": "intact", "tracking_id": "track a"},
                "box-b": {"label_status": "unreadable", "condition": "intact", "parcel_uid": "TRACK-B"},
                "box-c": {"label_status": "verified", "condition": "damaged", "barcode": "TRACK-C"},
            },
            entity_evidence_refs={
                "box-a": ["frame://policy/a"],
                "box-b": ["frame://policy/b"],
                "box-c": ["frame://policy/c"],
            },
            entity_location_evidence_refs={
                "box-a": ["frame://policy/a"],
                "box-b": ["frame://policy/b"],
                "box-c": ["frame://policy/c"],
            },
        )
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.CONFIRMED)
        self.assertEqual(result.rule_version, "parcel-policy-v2")
        self.assertIn("manifest_id=manifest-7", result.claim)
        self.assertIn("box-b", result.claim)
        self.assertIn("label_unreadable", result.claim)

        manifest["box-a"]["barcode"] = "BARCODE-A"
        state.entity_attributes["box-a"]["barcode"] = "WRONG-BARCODE"
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertEqual(result.reason_code, ReasonCode.GOAL_NOT_SATISFIED)
        self.assertIn("manifest_mismatches=['box-a']", result.claim)
        manifest["box-a"].pop("barcode")
        state.entity_attributes["box-a"].pop("barcode")

        state.entity_locations["box-b"] = "in:pickup_shelf"
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertIn("box-b->in:quarantine_bin", result.claim)

        state.entity_locations["box-b"] = "in:quarantine_bin"
        state.entity_attributes["box-c"].pop("condition")
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("box-c.condition", result.claim)

        state.entity_attributes["box-c"]["condition"] = "damaged"
        state.entity_attributes["box-b"]["tracking_id"] = "\uff34\uff32\uff2b\uff0d duplicate"
        state.entity_attributes["box-c"]["barcode"] = "TRKDUPLICATE"
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertIn("duplicate_identities", result.claim)

        state.entity_attributes["box-c"]["barcode"] = "TRACK-C"
        state.entity_attributes["box-b"] = {"label_status": "unreadable", "condition": "intact"}
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("missing_manifest_identities=['box-b']", result.claim)

        state.entity_attributes["box-b"]["tracking_id"] = "NOT-TRACK-B"
        result = verify_parcel_policy(
            state,
            "task-sort-parcels",
            ["box-a", "box-b", "box-c"],
            parcel_manifest=manifest,
            manifest_id="manifest-7",
        )
        self.assertEqual(result.status, VerificationStatus.REFUTED)
        self.assertIn("manifest_mismatches=['box-b']", result.claim)

        with self.assertRaises(ValueError):
            verify_parcel_policy(state, "task-sort-parcels", ["box-a"], "same", "same")


if __name__ == "__main__":
    unittest.main()
