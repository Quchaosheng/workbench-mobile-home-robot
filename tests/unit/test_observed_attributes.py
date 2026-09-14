from __future__ import annotations

import json
import sys
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs" / "contracts"),
    str(ROOT / "services" / "perception"),
    str(ROOT / "services" / "world_model"),
]

from workbench_contracts import (
    ATTRIBUTE_SCHEMA_VERSION,
    LEGACY_ATTRIBUTE_MIGRATION_VERSION,
    AttributeUpdateMode,
    ClockId,
    Observation,
    WorldEvent,
    WorldEventType,
    attribute_keys_for_entity_type,
    legacy_attribute_keys_allowed,
    validate_attribute_metadata_map,
    validate_observed_attributes,
)
from workbench_contracts import (
    WorldState as ContractWorldState,
)
from workbench_perception import CalibrationRecord, ObservationIngestionAdapter
from workbench_world_model import (
    FreshnessThresholds,
    ObservationAgingBoundary,
    ObservationFreshnessPolicy,
    create_world_state_snapshot,
    reduce_events,
    verify_parcel_policy,
    verify_parcel_sorting,
)
from workbench_world_model.event_payloads import (
    WorldEventPayloadValidationError,
)
from workbench_world_model.reducer import WorldState


def _metadata(
    *,
    observed_at: str = "2026-08-25T00:00:00Z",
    confidence: float = 0.95,
    evidence_refs: list[str] | None = None,
    belief: str = "observed",
    clock_id: str = "wall",
    source: str = "camera-01",
) -> dict[str, object]:
    return {
        "observed_at": observed_at,
        "confidence": confidence,
        "evidence_refs": ["frame://attribute/001"] if evidence_refs is None else evidence_refs,
        "belief": belief,
        "clock_id": clock_id,
        "source": source,
    }


def _observation_event(
    event_id: str,
    sequence_no: int,
    *,
    entity_id: str = "parcel-a",
    entity_type: str = "parcel",
    attributes: dict[str, str] | None = None,
    attributes_mode: str = "complete",
    attributes_schema_version: str = ATTRIBUTE_SCHEMA_VERSION,
    attribute_metadata: dict[str, dict[str, object]] | None = None,
    observed_at: str = "2026-08-25T00:00:00Z",
    source: str = "camera-01",
    confidence: float = 0.95,
    clock_id: ClockId = ClockId.WALL,
    location: str = "in:pickup_shelf",
) -> WorldEvent:
    payload: dict[str, object] = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "location": location,
        "confidence": confidence,
        "observed_at": observed_at,
        "clock_id": clock_id.value,
        "source": source,
    }
    if attributes is not None:
        payload.update(
            {
                "attributes": attributes,
                "attributes_mode": attributes_mode,
                "attributes_schema_version": attributes_schema_version,
            }
        )
        if attribute_metadata is not None:
            payload["attribute_metadata"] = attribute_metadata
    return WorldEvent(
        event_id=event_id,
        run_id="run-001",
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at=observed_at,
        payload=payload,
        clock_id=clock_id,
        evidence_refs=[f"frame://{event_id}"],
    )


def _schema_registry() -> Registry:
    resources = []
    for path in sorted((ROOT / "interfaces" / "json_schema").glob("*.schema.json")):
        resources.append((path.name, Resource.from_contents(json.loads(path.read_text(encoding="utf-8")))))
    return Registry().with_resources(resources)


def _observation_model_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "observation_id": "obs-001",
        "run_id": "run-001",
        "entity_id": "parcel-a",
        "entity_type": "parcel",
        "pose": {
            "frame_id": "workbench",
            "position": {"x": 0.2, "y": 0.1, "z": 0.02},
            "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
        },
        "confidence": 0.95,
        "observed_at": "2026-08-25T00:00:00Z",
        "clock_id": "wall",
        "source": "camera-01",
        "evidence_refs": ["frame://observation/001"],
        "attributes": {"condition": "intact"},
        "attributes_mode": "complete",
        "attributes_schema_version": ATTRIBUTE_SCHEMA_VERSION,
        "attribute_metadata": {"condition": _metadata()},
    }
    payload.update(updates)
    return payload


def test_attribute_vocabulary_scopes_and_legacy_policy_are_finite() -> None:
    assert "colour" in attribute_keys_for_entity_type("block")
    assert "door_state" in attribute_keys_for_entity_type("washer")
    assert "rack_state" in attribute_keys_for_entity_type("dishwasher")
    assert "slot_occupancy" in attribute_keys_for_entity_type("managed-slot")
    assert "condition" in attribute_keys_for_entity_type("parcel")
    assert "door_state" not in attribute_keys_for_entity_type("parcel")
    assert legacy_attribute_keys_allowed("legacy") is False

    with pytest.raises(ValueError, match="not supported for entity_type"):
        validate_observed_attributes({"door_state": "open"}, entity_type="parcel")

    assert validate_observed_attributes({"condition": "intact", "label_status": "verified"}, entity_type="parcel") == {
        "condition": "intact",
        "label_status": "verified",
    }


def test_attribute_count_size_and_metadata_evidence_bounds_fail_closed() -> None:
    too_many = {f"custom-{index}": "x" for index in range(33)}
    with pytest.raises(ValueError, match="at most 32"):
        validate_observed_attributes(too_many, allow_unknown_keys=True)

    too_large = {f"custom-{index}": "x" * 256 for index in range(32)}
    with pytest.raises(ValueError, match="4096 bytes"):
        validate_observed_attributes(too_large, allow_unknown_keys=True)

    with pytest.raises(ValueError, match="duplicates"):
        validate_attribute_metadata_map(
            {"condition": _metadata(evidence_refs=["same", "same"])},
            attribute_keys={"condition"},
        )
    with pytest.raises(ValueError, match="finite"):
        validate_attribute_metadata_map(
            {"condition": _metadata(confidence=float("nan"))},
            attribute_keys={"condition"},
        )


def test_observation_model_requires_modern_metadata_but_accepts_explicit_legacy() -> None:
    missing_metadata = _observation_model_payload()
    missing_metadata.pop("attribute_metadata")
    with pytest.raises(ValidationError, match="modern Observation attributes require attribute_metadata"):
        Observation.model_validate_json(json.dumps(missing_metadata))

    legacy = _observation_model_payload(attributes_schema_version=LEGACY_ATTRIBUTE_MIGRATION_VERSION)
    legacy.pop("attribute_metadata")
    assert (
        Observation.model_validate_json(json.dumps(legacy)).attributes_schema_version
        == LEGACY_ATTRIBUTE_MIGRATION_VERSION
    )


def test_observation_schema_rejects_modern_missing_metadata_and_accepts_sparse_entity() -> None:
    registry = _schema_registry()
    observation_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "observation.schema.json").read_text(encoding="utf-8")
    )
    observation = _observation_model_payload()
    observation.pop("attribute_metadata")
    assert list(Draft202012Validator(observation_schema, registry=registry).iter_errors(observation))

    world_state_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "world_state.schema.json").read_text(encoding="utf-8")
    )
    sparse = {
        "run_id": "run-001",
        "sequence_no": 1,
        "state_hash": "0" * 64,
        "entities": [{"entity_id": "tray", "entity_type": "tray", "belief": "observed"}],
        "reduced_at": "2026-08-25T00:00:00Z",
    }
    assert list(Draft202012Validator(world_state_schema, registry=registry).iter_errors(sparse)) == []

    invalid_entity = deepcopy(sparse)
    invalid_entity["entities"][0].update(
        {
            "attributes": {"colour": "blue"},
            "attributes_schema_version": ATTRIBUTE_SCHEMA_VERSION,
        }
    )
    assert list(Draft202012Validator(world_state_schema, registry=registry).iter_errors(invalid_entity))


def test_ingestion_materializes_modern_attribute_metadata_before_strict_model_validation() -> None:
    now = datetime(2026, 8, 25, 0, 0, 1, tzinfo=UTC)
    adapter = ObservationIngestionAdapter(
        [
            CalibrationRecord(
                camera_id="camera-01",
                revision="cal-v1",
                source_frame="camera",
                target_frame="workbench",
                clock_id=ClockId.WALL,
            )
        ],
        now=lambda _clock_id: now,
    )
    record = _observation_model_payload()
    record["pose"] = {
        "frame_id": "camera",
        "position": {"x": 0.2, "y": 0.1, "z": 0.02},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }
    record.pop("attribute_metadata")
    event = adapter.ingest(
        record,
        camera_id="camera-01",
        calibration_revision="cal-v1",
        pose_units="m",
        sequence_no=0,
    )
    assert event.payload["attribute_metadata"]["condition"]["evidence_refs"] == ["frame://observation/001"]
    assert event.payload["attribute_metadata"]["condition"]["belief"] == "observed"


def test_complete_replaces_and_partial_merges_attributes_with_timestamp_ordering() -> None:
    first = _observation_event(
        "obs-1",
        1,
        attributes={"label_status": "verified", "condition": "intact"},
        observed_at="2026-08-25T00:00:10Z",
    )
    partial = _observation_event(
        "obs-2",
        2,
        attributes={"condition": "damaged"},
        attributes_mode=AttributeUpdateMode.PARTIAL.value,
        observed_at="2026-08-25T00:00:11Z",
    )
    older_complete = _observation_event(
        "obs-3",
        3,
        attributes={"condition": "intact"},
        observed_at="2026-08-25T00:00:09Z",
    )
    state = reduce_events("run-001", [first, partial, older_complete])

    assert state.entity_attributes["parcel-a"] == {"label_status": "verified", "condition": "damaged"}
    assert state.entity_attribute_metadata["parcel-a"]["condition"]["observed_at"] == "2026-08-25T00:00:11Z"


def test_older_partial_update_is_ignored_per_attribute_and_partial_without_baseline_fails() -> None:
    baseline = _observation_event(
        "obs-base",
        1,
        attributes={"condition": "intact", "label_status": "verified"},
        observed_at="2026-08-25T00:00:10Z",
    )
    older_partial = _observation_event(
        "obs-old-partial",
        2,
        attributes={"condition": "damaged"},
        attributes_mode="partial",
        observed_at="2026-08-25T00:00:09Z",
    )
    state = reduce_events("run-001", [baseline, older_partial])
    assert state.entity_attributes["parcel-a"]["condition"] == "intact"

    with pytest.raises(WorldEventPayloadValidationError, match="prior complete"):
        reduce_events(
            "run-001",
            [
                _observation_event(
                    "obs-partial-only",
                    1,
                    attributes={"condition": "damaged"},
                    attributes_mode="partial",
                )
            ],
        )


def test_legacy_attributes_are_explicitly_migratable_but_unknown_keys_still_fail() -> None:
    legacy = WorldEvent(
        event_id="legacy-1",
        run_id="run-001",
        sequence_no=1,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-25T00:00:00Z",
        payload={
            "entity_id": "parcel-a",
            "location": "in:pickup_shelf",
            "confidence": 0.9,
            "attributes": {"condition": "intact"},
        },
        evidence_refs=["legacy://frame/1"],
    )
    state = reduce_events("run-001", [legacy])
    assert state.entity_types["parcel-a"] == "legacy"
    assert state.entity_attribute_schema_versions["parcel-a"] == LEGACY_ATTRIBUTE_MIGRATION_VERSION

    invalid = legacy.model_copy(
        update={"event_id": "legacy-invalid", "payload": {**legacy.payload, "attributes": {"unknown": "x"}}}
    )
    with pytest.raises(WorldEventPayloadValidationError, match="not supported"):
        reduce_events("run-001", [invalid])


def test_action_result_does_not_create_or_refresh_observed_attributes() -> None:
    observation = _observation_event(
        "obs-1",
        1,
        attributes={"condition": "intact"},
    )
    action = WorldEvent(
        event_id="action-1",
        run_id="run-001",
        sequence_no=2,
        event_type=WorldEventType.ACTION_RESULT,
        occurred_at="2026-08-25T00:00:02Z",
        payload={
            "result_id": "result-1",
            "action_id": "action-1",
            "run_id": "run-001",
            "outcome": "completed",
            "dispatch_state": "sent",
            "device_state": "confirmed",
            "started_at": "2026-08-25T00:00:01Z",
            "ended_at": "2026-08-25T00:00:02Z",
            "clock_id": "wall",
            "retry_count": 0,
            "entity_id": "parcel-a",
            "resulting_location": "in:quarantine_bin",
            "evidence_refs": ["action://1"],
        },
        evidence_refs=["action://1"],
    )
    before = reduce_events("run-001", [observation])
    after = reduce_events("run-001", [observation, action])
    assert after.entity_attributes == before.entity_attributes
    assert after.entity_attribute_metadata == before.entity_attribute_metadata
    assert "action://1" not in after.entity_evidence_refs["parcel-a"]


def test_snapshot_hash_changes_when_observed_attributes_change() -> None:
    red = _observation_event("obs-red", 1, entity_id="block", entity_type="block", attributes={"colour": "red"})
    blue = _observation_event("obs-blue", 1, entity_id="block", entity_type="block", attributes={"colour": "blue"})
    red_snapshot = create_world_state_snapshot("run-001", [red])
    blue_snapshot = create_world_state_snapshot("run-001", [blue])

    assert red_snapshot.state_hash != blue_snapshot.state_hash
    assert red_snapshot.entities[0].attributes == {"colour": "red"}
    assert blue_snapshot.entities[0].attributes == {"colour": "blue"}


def test_attribute_aging_reaches_stale_then_lost_on_explicit_boundary() -> None:
    event = _observation_event(
        "obs-aged",
        1,
        observed_at="2026-08-25T00:00:00Z",
        attributes={"condition": "intact"},
    )
    policy = ObservationFreshnessPolicy(
        rules={("camera-01", "parcel"): FreshnessThresholds(stale_after_s=5, lost_after_s=10)}
    )
    stale = reduce_events(
        "run-001",
        [event],
        freshness_policy=policy,
        aging_boundary=ObservationAgingBoundary("2026-08-25T00:00:07Z", ClockId.WALL),
    )
    lost = reduce_events(
        "run-001",
        [event],
        freshness_policy=policy,
        aging_boundary=ObservationAgingBoundary("2026-08-25T00:00:12Z", ClockId.WALL),
    )
    assert stale.entity_attribute_metadata["parcel-a"]["condition"]["belief"] == "stale"
    assert lost.entity_attribute_metadata["parcel-a"]["condition"]["belief"] == "lost"


def test_verifiers_use_attribute_evidence_and_distinguish_quality_failures() -> None:
    state = WorldState(
        run_id="run-001",
        entity_locations={"parcel-a": "in:pickup"},
        entity_confidence={"parcel-a": 0.95},
        entity_evidence_refs={"parcel-a": ["frame://entity"]},
        entity_attributes={"parcel-a": {"label_status": "verified", "condition": "intact"}},
        entity_attribute_schema_versions={"parcel-a": ATTRIBUTE_SCHEMA_VERSION},
        entity_attribute_metadata={
            "parcel-a": {
                "label_status": _metadata(evidence_refs=["frame://label"], confidence=0.95),
                "condition": _metadata(evidence_refs=["frame://condition"], confidence=0.95),
            }
        },
    )
    confirmed = verify_parcel_sorting(
        state,
        "task-sort",
        parcel_routes={"parcel-a": "pickup"},
        expected_attributes={"parcel-a": {"label_status": "verified", "condition": "intact"}},
    )
    assert confirmed.status.value == "confirmed"
    assert confirmed.evidence_refs == ["frame://entity", "frame://condition", "frame://label"]

    state.entity_attribute_metadata["parcel-a"]["condition"]["confidence"] = 0.2
    low_confidence = verify_parcel_sorting(
        state,
        "task-sort",
        parcel_routes={"parcel-a": "pickup"},
        expected_attributes={"parcel-a": {"label_status": "verified", "condition": "intact"}},
    )
    assert low_confidence.reason_code.value == "confidence_below_threshold"

    state.entity_attribute_metadata["parcel-a"]["condition"]["confidence"] = 0.95
    state.entity_attribute_metadata["parcel-a"]["condition"]["belief"] = "stale"
    stale = verify_parcel_policy(state, "task-policy", ["parcel-a"])
    assert stale.reason_code.value == "stale_observation"


def test_verifier_default_context_is_finite_even_when_internal_state_contains_nan() -> None:
    state = WorldState(
        run_id="run-001",
        entity_locations={"block": "in:tray"},
        entity_confidence={"block": float("nan")},
        entity_evidence_refs={"block": ["frame://block"]},
    )
    from workbench_world_model.verifier import verify_object_in_tray

    verification = verify_object_in_tray(state, "task-object", "block", "tray")
    assert verification.verification_id.startswith("ver-")
    assert len(verification.verification_id) == 68


def test_public_world_state_model_accepts_reducer_snapshot() -> None:
    event = _observation_event("obs-contract", 1, entity_id="block", entity_type="block", attributes={"colour": "red"})
    snapshot = create_world_state_snapshot("run-001", [event])
    assert isinstance(snapshot, ContractWorldState)
    assert ContractWorldState.model_validate_json(snapshot.model_dump_json()) == snapshot
