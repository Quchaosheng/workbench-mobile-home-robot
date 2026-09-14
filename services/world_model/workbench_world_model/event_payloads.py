"""Typed validation for state-affecting WorldEvent payloads."""

from __future__ import annotations

import math
from copy import deepcopy
from typing import Any

from pydantic import ConfigDict, Field, ValidationError, model_validator
from workbench_contracts import (
    ATTRIBUTE_SCHEMA_VERSION,
    LEGACY_ATTRIBUTE_MIGRATION_VERSION,
    MAX_ATTRIBUTE_COUNT,
    MAX_ATTRIBUTE_EVIDENCE_REF_COUNT,
    MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH,
    MAX_ATTRIBUTE_KEY_LENGTH,
    MAX_ATTRIBUTE_METADATA_JSON_BYTES,
    MAX_ATTRIBUTE_VALUE_LENGTH,
    MAX_ATTRIBUTES_JSON_BYTES,
    ActionOutcome,
    ActionResult,
    AttributeUpdateMode,
    ClockId,
    DeviceState,
    DispatchState,
    WorldEvent,
    WorldEventType,
    legacy_attribute_keys_allowed,
    materialize_attribute_metadata,
    validate_attribute_evidence_refs,
    validate_observed_attributes,
)

_ATTRIBUTE_SCHEMA_VERSION_FIELD = "attributes_schema_version"
_MISSING = object()

__all__ = [
    "MAX_ATTRIBUTES_JSON_BYTES",
    "MAX_ATTRIBUTE_COUNT",
    "MAX_ATTRIBUTE_EVIDENCE_REF_COUNT",
    "MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH",
    "MAX_ATTRIBUTE_KEY_LENGTH",
    "MAX_ATTRIBUTE_METADATA_JSON_BYTES",
    "MAX_ATTRIBUTE_VALUE_LENGTH",
    "TypedActionResult",
    "WorldEventPayloadValidationError",
    "normalize_action_result_payload",
    "normalize_world_event",
]


class WorldEventPayloadValidationError(ValueError):
    """A state-affecting WorldEvent payload is malformed or inconsistent."""


def _strict_non_blank_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip():
        raise WorldEventPayloadValidationError(f"{field_name} must be a non-empty string")
    return value


def _normalize_attributes(
    value: object,
    *,
    entity_type: object | None = None,
    allow_unknown_keys: bool = False,
) -> dict[str, str]:
    try:
        return validate_observed_attributes(
            value,
            entity_type=entity_type if entity_type is not None else None,
            allow_unknown_keys=allow_unknown_keys,
        )
    except ValueError as error:
        raise WorldEventPayloadValidationError(str(error)) from error


def _normalize_attributes_mode(value: object) -> str:
    if isinstance(value, AttributeUpdateMode):
        return value.value
    if type(value) is not str:
        raise WorldEventPayloadValidationError("attributes_mode must be a string enum value")
    try:
        return AttributeUpdateMode(value).value
    except ValueError as error:
        raise WorldEventPayloadValidationError("attributes_mode must be 'complete' or 'partial'") from error


def _normalize_attribute_schema_version(value: object) -> str:
    if type(value) is not str:
        raise WorldEventPayloadValidationError(f"{_ATTRIBUTE_SCHEMA_VERSION_FIELD} must be a string enum value")
    if value not in {ATTRIBUTE_SCHEMA_VERSION, LEGACY_ATTRIBUTE_MIGRATION_VERSION}:
        raise WorldEventPayloadValidationError(
            f"{_ATTRIBUTE_SCHEMA_VERSION_FIELD} must be {ATTRIBUTE_SCHEMA_VERSION!r} "
            f"or {LEGACY_ATTRIBUTE_MIGRATION_VERSION!r}"
        )
    return value


def _looks_like_legacy_attribute_payload(payload: dict[str, Any]) -> bool:
    # Old reducer events carried only entity/location/confidence. Once a
    # producer supplies modern identity/timing fields, omission of the
    # explicit version is treated as malformed rather than silently migrated.
    return not any(field in payload for field in ("entity_type", "observed_at", "clock_id", "source"))


def _normalize_observation_payload(
    payload: dict[str, Any],
    *,
    event_occurred_at: object,
    event_clock_id: object,
    event_evidence_refs: object,
) -> dict[str, Any]:
    normalized = deepcopy(payload)

    if "entity_id" not in normalized:
        raise WorldEventPayloadValidationError("entity_id is required")
    normalized["entity_id"] = _strict_non_blank_string(normalized["entity_id"], "entity_id")

    for field_name in ("entity_type", "observed_at", "source"):
        if field_name in normalized:
            if normalized[field_name] is None:
                raise WorldEventPayloadValidationError(f"{field_name} may be omitted but cannot be null")
            normalized[field_name] = _strict_non_blank_string(normalized[field_name], field_name)

    if "clock_id" in normalized and normalized["clock_id"] is None:
        raise WorldEventPayloadValidationError("clock_id may be omitted but cannot be null")

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

    attribute_fields = {
        "attributes_mode",
        _ATTRIBUTE_SCHEMA_VERSION_FIELD,
        "attribute_metadata",
    }
    if "attributes" not in normalized and attribute_fields.intersection(normalized):
        raise WorldEventPayloadValidationError("attribute metadata and version fields require attributes")

    if "attributes" not in normalized:
        return normalized

    raw_version = normalized.get(_ATTRIBUTE_SCHEMA_VERSION_FIELD, _MISSING)
    if raw_version is _MISSING:
        if not _looks_like_legacy_attribute_payload(normalized):
            raise WorldEventPayloadValidationError(
                f"{_ATTRIBUTE_SCHEMA_VERSION_FIELD} is required for modern attributes"
            )
        version = LEGACY_ATTRIBUTE_MIGRATION_VERSION
    else:
        version = _normalize_attribute_schema_version(raw_version)
    is_legacy_version = version == LEGACY_ATTRIBUTE_MIGRATION_VERSION
    allow_legacy = is_legacy_version and legacy_attribute_keys_allowed(normalized.get("entity_type"))
    normalized[_ATTRIBUTE_SCHEMA_VERSION_FIELD] = version

    if "attributes_mode" in normalized:
        if normalized["attributes_mode"] is None:
            raise WorldEventPayloadValidationError("attributes_mode may be omitted but cannot be null")
        normalized["attributes_mode"] = _normalize_attributes_mode(normalized["attributes_mode"])
    else:
        normalized["attributes_mode"] = AttributeUpdateMode.COMPLETE.value

    if normalized["attributes"] is None:
        raise WorldEventPayloadValidationError(
            "attributes may be omitted but cannot be null; attributes must be a string-to-string mapping"
        )

    normalized["attributes"] = _normalize_attributes(
        normalized["attributes"],
        entity_type=normalized.get("entity_type"),
        allow_unknown_keys=allow_legacy,
    )

    try:
        normalized_event_evidence_refs = validate_attribute_evidence_refs(
            event_evidence_refs,
            field_name="event evidence_refs",
            require_non_empty=False,
        )
    except (TypeError, ValueError) as error:
        raise WorldEventPayloadValidationError(str(error)) from error

    raw_clock_id = normalized.get("clock_id", event_clock_id)
    if raw_clock_id is None:
        raw_clock_id = ClockId.MONOTONIC
    if isinstance(raw_clock_id, ClockId):
        raw_clock_id = raw_clock_id.value
    if type(raw_clock_id) is not str or raw_clock_id not in {ClockId.MONOTONIC.value, ClockId.WALL.value}:
        raise WorldEventPayloadValidationError("attribute metadata clock_id must be monotonic or wall")
    event_clock_value = event_clock_id.value if isinstance(event_clock_id, ClockId) else event_clock_id
    if event_clock_value is not None and raw_clock_id != event_clock_value:
        raise WorldEventPayloadValidationError("attribute metadata clock_id must match the enclosing WorldEvent")
    normalized["clock_id"] = raw_clock_id

    if not is_legacy_version:
        for field_name in ("entity_type", "observed_at", "source"):
            if field_name not in normalized:
                raise WorldEventPayloadValidationError(f"modern attributes require event-level {field_name}")
        if not normalized_event_evidence_refs:
            raise WorldEventPayloadValidationError("modern attributes require event-level evidence_refs")

    observed_at = normalized.get("observed_at", event_occurred_at)
    source = normalized.get("source")
    normalized["observed_at"] = observed_at
    if source is not None:
        normalized["source"] = source
    if "attribute_metadata" in normalized and normalized["attribute_metadata"] is None:
        raise WorldEventPayloadValidationError("attribute_metadata may be omitted but cannot be null")
    try:
        normalized["attribute_metadata"] = materialize_attribute_metadata(
            normalized["attributes"],
            normalized.get("attribute_metadata"),
            observed_at=observed_at,
            confidence=normalized["confidence"],
            evidence_refs=normalized_event_evidence_refs,
            clock_id=raw_clock_id,
            source=source,
            entity_type=normalized.get("entity_type"),
            allow_unknown_keys=allow_legacy,
        )
    except (TypeError, ValueError) as error:
        raise WorldEventPayloadValidationError(str(error)) from error

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
        payload = _normalize_observation_payload(
            event.payload,
            event_occurred_at=event.occurred_at,
            event_clock_id=event.clock_id,
            event_evidence_refs=event.evidence_refs,
        )
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
