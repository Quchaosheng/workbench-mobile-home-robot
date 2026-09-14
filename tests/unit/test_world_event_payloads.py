from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

from workbench_contracts import (
    ATTRIBUTE_SCHEMA_VERSION,
    ActionOutcome,
    ActionResult,
    WorldEvent,
    WorldEventType,
)
from workbench_world_model.event_payloads import (
    MAX_ATTRIBUTE_KEY_LENGTH,
    MAX_ATTRIBUTE_VALUE_LENGTH,
    MAX_ATTRIBUTES_JSON_BYTES,
    WorldEventPayloadValidationError,
    normalize_action_result_payload,
    normalize_world_event,
)


def observation_event(payload: dict[str, object]) -> WorldEvent:
    return WorldEvent(
        event_id="evt-observation",
        run_id="run-001",
        sequence_no=1,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-25T00:00:00Z",
        payload=payload,
        evidence_refs=["frame://observation/001"],
    )


def valid_observation_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "observation_id": "obs-001",
        "run_id": "run-001",
        "entity_id": "red_block",
        "entity_type": "parcel",
        "location": "on:table",
        "confidence": 0.9,
        "attributes": {"condition": "intact"},
        "attributes_schema_version": ATTRIBUTE_SCHEMA_VERSION,
        "observed_at": "2026-08-25T00:00:00Z",
        "source": "test-camera",
        "pose": {"opaque_metadata": "preserved"},
    }
    payload.update(updates)
    return payload


def supported_parcel_attributes() -> dict[str, str]:
    return {
        "barcode": "BARCODE-001",
        "colour": "red",
        "condition": "intact",
        "identity": "parcel-001",
        "label_status": "verified",
        "orientation": "upright",
        "parcel_uid": "UID-001",
        "presence": "present",
        "tracking_id": "TRACK-001",
    }


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
        "started_at": "2026-08-25T00:00:01Z",
        "ended_at": "2026-08-25T00:00:02Z",
        "clock_id": "monotonic",
        "retry_count": 0,
        "entity_id": "red_block",
        "resulting_location": "in:tray",
        "evidence_refs": ["mcu-frame-001"],
    }
    payload.update(updates)
    return payload


def action_result_event(
    payload: dict[str, object] | None = None,
    *,
    run_id: str = "run-001",
    evidence_refs: list[str] | None = None,
) -> WorldEvent:
    return WorldEvent(
        event_id="evt-action-result",
        run_id=run_id,
        sequence_no=1,
        event_type=WorldEventType.ACTION_RESULT,
        occurred_at="2026-08-25T00:00:02Z",
        payload=action_result_payload() if payload is None else payload,
        evidence_refs=["mcu-frame-001"] if evidence_refs is None else evidence_refs,
    )


def test_valid_observation_payload_is_normalized_without_losing_metadata() -> None:
    original = valid_observation_payload(confidence=1)

    normalized = normalize_world_event(observation_event(original))

    assert normalized.payload["confidence"] == 1.0
    assert isinstance(normalized.payload["confidence"], float)
    assert normalized.payload["attributes"] == {"condition": "intact"}
    assert normalized.payload["pose"] == {"opaque_metadata": "preserved"}
    assert original["confidence"] == 1


@pytest.mark.parametrize("confidence", [0, 0.0, 0.5, 1, 1.0])
def test_observation_confidence_boundaries_are_accepted(confidence: int | float) -> None:
    normalized = normalize_world_event(observation_event(valid_observation_payload(confidence=confidence)))

    assert normalized.payload["confidence"] == float(confidence)


@pytest.mark.parametrize(
    "confidence",
    [float("nan"), float("inf"), float("-inf"), -0.01, 1.01, True, "0.9", None],
    ids=["nan", "positive-infinity", "negative-infinity", "below-zero", "above-one", "bool", "string", "null"],
)
def test_observation_confidence_failures_are_rejected(confidence: object) -> None:
    with pytest.raises(WorldEventPayloadValidationError, match="confidence"):
        normalize_world_event(observation_event(valid_observation_payload(confidence=confidence)))


def test_observation_missing_confidence_is_rejected() -> None:
    payload = valid_observation_payload()
    payload.pop("confidence")

    with pytest.raises(WorldEventPayloadValidationError, match="confidence"):
        normalize_world_event(observation_event(payload))


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"entity_id": 123}, "entity_id"),
        ({"entity_id": ""}, "entity_id"),
        ({"entity_id": "   "}, "entity_id"),
        ({"location": ["bad"]}, "location"),
        ({"location": ""}, "location"),
        ({"location": "   "}, "location"),
    ],
    ids=["integer-id", "empty-id", "blank-id", "container-location", "empty-location", "blank-location"],
)
def test_observation_identity_and_location_are_strict(updates: dict[str, object], message: str) -> None:
    with pytest.raises(WorldEventPayloadValidationError, match=message):
        normalize_world_event(observation_event(valid_observation_payload(**updates)))


def test_observation_without_optional_location_or_attributes_is_valid() -> None:
    payload = valid_observation_payload()
    payload.pop("location")
    payload.pop("attributes")
    payload.pop("attributes_schema_version")

    normalized = normalize_world_event(observation_event(payload))

    assert "location" not in normalized.payload
    assert "attributes" not in normalized.payload


def test_observation_supported_attribute_vocabulary_is_accepted() -> None:
    attributes = supported_parcel_attributes()
    attributes["tracking_id"] = "v" * MAX_ATTRIBUTE_VALUE_LENGTH

    normalized = normalize_world_event(observation_event(valid_observation_payload(attributes=attributes)))

    assert normalized.payload["attributes"] == attributes


def test_observation_attribute_utf8_values_are_preserved() -> None:
    attributes = supported_parcel_attributes()
    attributes["tracking_id"] = "€" * MAX_ATTRIBUTE_VALUE_LENGTH
    encoded = json.dumps(attributes, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    assert len(encoded) < MAX_ATTRIBUTES_JSON_BYTES

    normalized = normalize_world_event(observation_event(valid_observation_payload(attributes=attributes)))

    assert normalized.payload["attributes"] == attributes


def test_observation_attribute_bounds_fail_closed() -> None:
    cases: list[dict[object, object]] = [
        {"unsupported": "value"},
        {"k" * (MAX_ATTRIBUTE_KEY_LENGTH + 1): "v"},
        {"tracking_id": "v" * (MAX_ATTRIBUTE_VALUE_LENGTH + 1)},
        {"": "value"},
        {"   ": "value"},
        {"tracking_id": ""},
        {"tracking_id": "   "},
        {"tracking_id": {"nested": "value"}},
        {"tracking_id": ["value"]},
        {"tracking_id": None},
        {"tracking_id": True},
        {"tracking_id": 1},
        {"tracking_id": "\ud800"},
        {"condition": "not-a-condition"},
    ]

    for attributes in cases:
        with pytest.raises(WorldEventPayloadValidationError, match="attributes"):
            normalize_world_event(observation_event(valid_observation_payload(attributes=attributes)))


@pytest.mark.parametrize(
    "attributes",
    [None, [], "condition=intact"],
    ids=["null", "array", "string"],
)
def test_observation_attributes_must_be_a_mapping(attributes: object) -> None:
    with pytest.raises(WorldEventPayloadValidationError, match="string-to-string mapping"):
        normalize_world_event(observation_event(valid_observation_payload(attributes=attributes)))


@pytest.mark.parametrize(
    "attributes",
    [{1: "value"}, {True: "value"}],
    ids=["integer-key", "boolean-key"],
)
def test_observation_attribute_keys_must_be_strings(attributes: dict[object, object]) -> None:
    with pytest.raises(WorldEventPayloadValidationError, match="keys must be non-empty strings"):
        normalize_world_event(observation_event(valid_observation_payload(attributes=attributes)))


@pytest.mark.parametrize(
    "attributes",
    [{"key": "\ud800"}, {"\ud800": "value"}],
    ids=["surrogate-value", "surrogate-key"],
)
def test_observation_attributes_must_encode_as_utf8(attributes: dict[str, str]) -> None:
    if "key" in attributes:
        attributes = {"tracking_id": attributes["key"]}
    with pytest.raises(WorldEventPayloadValidationError, match="valid UTF-8"):
        normalize_world_event(observation_event(valid_observation_payload(attributes=attributes)))


def test_valid_action_result_payload_is_normalized() -> None:
    normalized = normalize_world_event(action_result_event())

    assert normalized.payload == action_result_payload()


def test_action_result_payload_adapter_returns_strict_canonical_model() -> None:
    result = normalize_action_result_payload(
        action_result_payload(),
        event_run_id="run-001",
        event_evidence_refs=["mcu-frame-001"],
    )

    assert type(result) is ActionResult
    assert result.outcome is ActionOutcome.COMPLETED


@pytest.mark.parametrize(
    ("payload", "run_id", "evidence_refs", "expected_action_id", "message"),
    [
        (action_result_payload(result_id=""), "run-001", ["mcu-frame-001"], None, "result_id"),
        (action_result_payload(action_id="   "), "run-001", ["mcu-frame-001"], None, "action_id"),
        (action_result_payload(run_id=""), "run-001", ["mcu-frame-001"], None, "run_id"),
        (action_result_payload(), "run-other", ["mcu-frame-001"], None, "run_id"),
        (action_result_payload(), "run-001", ["mcu-frame-other"], None, "evidence_refs"),
        (action_result_payload(), "run-001", ["mcu-frame-001"], "act-other", "action_id"),
        (
            action_result_payload(resulting_location=None),
            "run-001",
            ["mcu-frame-001"],
            None,
            "entity_id.*resulting_location",
        ),
        (action_result_payload(entity_id=None), "run-001", ["mcu-frame-001"], None, "entity_id.*resulting_location"),
        (
            action_result_payload(outcome="failed", dispatch_state="sent", device_state="rejected"),
            "run-001",
            ["mcu-frame-001"],
            None,
            "resulting_location",
        ),
        (
            action_result_payload(dispatch_state="not_sent", device_state="unconfirmed"),
            "run-001",
            ["mcu-frame-001"],
            None,
            "completed",
        ),
    ],
    ids=[
        "empty-result-id",
        "blank-action-id",
        "empty-payload-run-id",
        "event-run-mismatch",
        "evidence-mismatch",
        "motion-action-mismatch",
        "completed-missing-location",
        "completed-missing-entity",
        "failed-resulting-location",
        "invalid-completion-state",
    ],
)
def test_action_result_semantics_fail_closed(
    payload: dict[str, object],
    run_id: str,
    evidence_refs: list[str],
    expected_action_id: str | None,
    message: str,
) -> None:
    with pytest.raises(WorldEventPayloadValidationError, match=message):
        normalize_world_event(
            action_result_event(payload, run_id=run_id, evidence_refs=evidence_refs),
            expected_action_id=expected_action_id,
        )


def test_action_result_payload_must_be_an_object() -> None:
    with pytest.raises(WorldEventPayloadValidationError, match="must be an object"):
        normalize_action_result_payload("not-an-object", event_run_id="run-001")


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"unexpected": "value"}, "unexpected"),
        ({"result_id": 1}, "result_id"),
        ({"action_id": True}, "action_id"),
        ({"run_id": ["run-001"]}, "run_id"),
        ({"started_at": ""}, "started_at"),
        ({"ended_at": "   "}, "ended_at"),
        ({"outcome": 1}, "outcome"),
        ({"dispatch_state": True}, "dispatch_state"),
        ({"device_state": None}, "device_state"),
        ({"clock_id": 0}, "clock_id"),
        ({"outcome": "unknown"}, "outcome"),
        ({"error_code": True}, "error_code"),
        ({"error_code": 1.5}, "error_code"),
        ({"error_reason": 404}, "error_reason"),
        ({"retry_count": True}, "retry_count"),
        ({"retry_count": 1.5}, "retry_count"),
        ({"retry_count": -1}, "retry_count"),
        ({"entity_id": 1}, "entity_id"),
        ({"entity_id": ""}, "entity_id"),
        ({"resulting_location": 1}, "resulting_location"),
        ({"resulting_location": "   "}, "resulting_location"),
        ({"evidence_refs": "mcu-frame-001"}, "evidence_refs"),
        ({"evidence_refs": ("mcu-frame-001",)}, "evidence_refs"),
        ({"evidence_refs": [1]}, "evidence_refs"),
    ],
    ids=[
        "extra-field",
        "integer-result-id",
        "boolean-action-id",
        "array-run-id",
        "empty-started-at",
        "blank-ended-at",
        "integer-outcome",
        "boolean-dispatch-state",
        "null-device-state",
        "integer-clock-id",
        "unknown-outcome",
        "boolean-error-code",
        "float-error-code",
        "integer-error-reason",
        "boolean-retry-count",
        "float-retry-count",
        "negative-retry-count",
        "integer-entity-id",
        "empty-entity-id",
        "integer-resulting-location",
        "blank-resulting-location",
        "string-evidence-refs",
        "tuple-evidence-refs",
        "non-string-evidence-ref",
    ],
)
def test_action_result_strict_fields_fail_closed(updates: dict[str, object], message: str) -> None:
    with pytest.raises(WorldEventPayloadValidationError, match=message):
        normalize_world_event(action_result_event(action_result_payload(**updates)))


def test_completed_action_result_without_spatial_claim_is_valid() -> None:
    payload = action_result_payload(entity_id=None, resulting_location=None)

    normalized = normalize_world_event(action_result_event(payload))

    assert normalized.payload["entity_id"] is None
    assert normalized.payload["resulting_location"] is None


def test_non_completed_action_result_without_resulting_location_is_valid() -> None:
    payload = action_result_payload(
        outcome="failed",
        device_state="rejected",
        resulting_location=None,
    )

    normalized = normalize_world_event(action_result_event(payload))

    assert normalized.payload["outcome"] == "failed"
    assert normalized.payload["entity_id"] == "red_block"
    assert normalized.payload["resulting_location"] is None


def test_non_state_event_payload_is_unchanged() -> None:
    event = WorldEvent(
        event_id="evt-fault",
        run_id="run-001",
        sequence_no=1,
        event_type=WorldEventType.FAULT,
        occurred_at="2026-08-25T00:00:00Z",
        payload={"nested": {"opaque": True}},
        evidence_refs=[],
    )

    assert normalize_world_event(event) == event
