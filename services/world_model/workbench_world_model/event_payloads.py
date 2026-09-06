"""Typed validation for state-affecting WorldEvent payloads."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from typing import Any

from pydantic import ConfigDict, Field, ValidationError, model_validator
from workbench_contracts import (
    ActionOutcome,
    ActionResult,
    ClockId,
    DeviceState,
    DispatchState,
    WorldEvent,
    WorldEventType,
)

MAX_ATTRIBUTE_COUNT = 32
MAX_ATTRIBUTE_KEY_LENGTH = 64
MAX_ATTRIBUTE_VALUE_LENGTH = 256
MAX_ATTRIBUTES_JSON_BYTES = 4096


class WorldEventPayloadValidationError(ValueError):
    """A state-affecting WorldEvent payload is malformed or inconsistent."""


def _strict_non_blank_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise WorldEventPayloadValidationError(f"{field_name} must be a non-empty string")
    return value


def _normalize_attributes(value: object) -> dict[str, str]:
    if type(value) is not dict:
        raise WorldEventPayloadValidationError("attributes must be a string-to-string mapping")
    if len(value) > MAX_ATTRIBUTE_COUNT:
        raise WorldEventPayloadValidationError(f"attributes must contain at most {MAX_ATTRIBUTE_COUNT} entries")

    normalized: dict[str, str] = {}
    for key, item in value.items():
        if type(key) is not str or not key.strip():
            raise WorldEventPayloadValidationError("attributes keys must be non-empty strings")
        if len(key) > MAX_ATTRIBUTE_KEY_LENGTH:
            raise WorldEventPayloadValidationError(
                f"attributes keys must be at most {MAX_ATTRIBUTE_KEY_LENGTH} characters"
            )
        if type(item) is not str or not item.strip():
            raise WorldEventPayloadValidationError("attributes values must be non-empty strings")
        if len(item) > MAX_ATTRIBUTE_VALUE_LENGTH:
            raise WorldEventPayloadValidationError(
                f"attributes values must be at most {MAX_ATTRIBUTE_VALUE_LENGTH} characters"
            )
        normalized[key] = item

    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except UnicodeEncodeError as error:
        raise WorldEventPayloadValidationError("attributes must be valid UTF-8 JSON strings") from error
    if len(encoded) > MAX_ATTRIBUTES_JSON_BYTES:
        raise WorldEventPayloadValidationError(
            f"attributes canonical UTF-8 JSON must be at most {MAX_ATTRIBUTES_JSON_BYTES} bytes"
        )
    return normalized


def _normalize_observation_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = deepcopy(payload)

    if "entity_id" not in normalized:
        raise WorldEventPayloadValidationError("entity_id is required")
    normalized["entity_id"] = _strict_non_blank_string(normalized["entity_id"], "entity_id")

    if "entity_type" not in normalized:
        raise WorldEventPayloadValidationError("entity_type is required")
    normalized["entity_type"] = _strict_non_blank_string(normalized["entity_type"], "entity_type")

    if "location" in normalized:
        normalized["location"] = _strict_non_blank_string(normalized["location"], "location")

    if "confidence" not in normalized:
        raise WorldEventPayloadValidationError("confidence is required")
    confidence = normalized["confidence"]
    if type(confidence) not in {int, float}:
        raise WorldEventPayloadValidationError("confidence must be a JSON number")
    confidence_value = float(confidence)
    if not math.isfinite(confidence_value) or not 0.0 <= confidence_value <= 1.0:
        raise WorldEventPayloadValidationError("confidence must be finite and between 0 and 1")
    normalized["confidence"] = confidence_value

    if "attributes" in normalized:
        normalized["attributes"] = _normalize_attributes(normalized["attributes"])

    return normalized


class TypedActionResult(ActionResult):
    """World Model's strict specialization of the shared ActionResult model."""

    model_config = ConfigDict(extra="forbid")

    outcome: ActionOutcome = Field(strict=False)
    dispatch_state: DispatchState = Field(strict=False)
    device_state: DeviceState = Field(strict=False)
    clock_id: ClockId = Field(default=ClockId.MONOTONIC, strict=False)

    @model_validator(mode="before")
    @classmethod
    def validate_strict_json_fields(cls, value: object) -> object:
        if type(value) is not dict:
            raise ValueError("ActionResult payload must be an object")

        for field_name in ("result_id", "action_id", "run_id"):
            field_value = value.get(field_name)
            if type(field_value) is not str or not field_value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

        for field_name in ("started_at", "ended_at"):
            field_value = value.get(field_name)
            if type(field_value) is not str or not field_value.strip():
                raise ValueError(f"{field_name} must be a non-empty string")

        for field_name in ("outcome", "dispatch_state", "device_state", "clock_id"):
            if field_name in value and not isinstance(value[field_name], str):
                raise ValueError(f"{field_name} must be a string enum value")

        error_code = value.get("error_code")
        if error_code is not None and (type(error_code) is not int):
            raise ValueError("error_code must be an integer or null")

        error_reason = value.get("error_reason")
        if error_reason is not None and type(error_reason) is not str:
            raise ValueError("error_reason must be a string or null")

        retry_count = value.get("retry_count", 0)
        if type(retry_count) is not int:
            raise ValueError("retry_count must be an integer")

        for field_name in ("entity_id", "resulting_location"):
            field_value = value.get(field_name)
            if field_value is not None and (type(field_value) is not str or not field_value.strip()):
                raise ValueError(f"{field_name} must be a non-empty string or null")

        evidence_refs = value.get("evidence_refs", [])
        if type(evidence_refs) is not list or any(type(reference) is not str for reference in evidence_refs):
            raise ValueError("evidence_refs must be a list of strings")

        return value

    @model_validator(mode="after")
    def validate_spatial_claim(self) -> TypedActionResult:
        has_entity = self.entity_id is not None
        has_location = self.resulting_location is not None
        if self.outcome is ActionOutcome.COMPLETED:
            if has_entity != has_location:
                raise ValueError(
                    "completed ActionResult entity_id and resulting_location must both be present or both be null"
                )
        elif has_location:
            raise ValueError("non-completed ActionResult cannot declare resulting_location")
        return self


def normalize_action_result_payload(
    payload: object,
    *,
    event_run_id: object,
    event_evidence_refs: object | None = None,
    expected_action_id: object | None = None,
) -> ActionResult:
    """Validate ActionResult fields and their enclosing event correlations."""

    try:
        adapted = TypedActionResult.model_validate(payload)
        result = ActionResult.model_validate(adapted.model_dump(mode="python"))
    except ValidationError as error:
        raise WorldEventPayloadValidationError(f"invalid ActionResult payload: {error}") from error

    if result.run_id != event_run_id:
        raise WorldEventPayloadValidationError("ActionResult run_id must match the enclosing WorldEvent run_id")
    if expected_action_id is not None and result.action_id != expected_action_id:
        raise WorldEventPayloadValidationError("ActionResult action_id must match the Motion ExecutionEvent action_id")
    if event_evidence_refs is not None and result.evidence_refs != event_evidence_refs:
        raise WorldEventPayloadValidationError(
            "ActionResult evidence_refs must match the enclosing WorldEvent evidence_refs"
        )
    return result


def normalize_world_event(
    event: WorldEvent,
    *,
    expected_action_id: object | None = None,
) -> WorldEvent:
    """Return a detached event with validated, normalized state-affecting payload."""

    if event.event_type is WorldEventType.OBSERVATION:
        payload = _normalize_observation_payload(event.payload)
    elif event.event_type is WorldEventType.ACTION_RESULT:
        payload = normalize_action_result_payload(
            event.payload,
            event_run_id=event.run_id,
            event_evidence_refs=event.evidence_refs,
            expected_action_id=expected_action_id,
        ).model_dump(mode="json")
    else:
        return event.model_copy(deep=True)

    return event.model_copy(update={"payload": payload}, deep=True)
