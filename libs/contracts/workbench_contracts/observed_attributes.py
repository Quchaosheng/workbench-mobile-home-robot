"""Bounded observed-attribute vocabulary shared by producers and consumers."""

from __future__ import annotations

import json
from copy import deepcopy
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final

from pydantic import BeforeValidator, WithJsonSchema

MAX_ATTRIBUTE_COUNT: Final = 32
MAX_ATTRIBUTE_KEY_LENGTH: Final = 64
MAX_ATTRIBUTE_VALUE_LENGTH: Final = 256
MAX_ATTRIBUTES_JSON_BYTES: Final = 4096

PARCEL_ATTRIBUTE_KEYS = frozenset(
    {
        "label_status",
        "condition",
        "tracking_id",
        "barcode",
        "parcel_uid",
    }
)
APPLIANCE_ATTRIBUTE_KEYS = frozenset({"door_state", "rack_state"})
MANAGED_SLOT_ATTRIBUTE_KEYS = frozenset({"slot_state", "slot_occupancy"})
SUPPORTED_ATTRIBUTE_KEYS = frozenset(
    PARCEL_ATTRIBUTE_KEYS | APPLIANCE_ATTRIBUTE_KEYS | MANAGED_SLOT_ATTRIBUTE_KEYS
)

ATTRIBUTE_ENUMS = MappingProxyType(
    {
        "label_status": frozenset({"verified", "unreadable", "missing", "unknown"}),
        "condition": frozenset({"intact", "damaged", "unknown"}),
        "door_state": frozenset({"open", "closed", "unknown"}),
        "rack_state": frozenset({"open", "closed", "unknown"}),
        "slot_state": frozenset({"empty", "occupied", "blocked", "unknown"}),
        "slot_occupancy": frozenset({"empty", "occupied", "blocked", "unknown"}),
    }
)

_PARCEL_ENTITY_TYPES = frozenset(
    {
        "parcel",
        "parcel_box",
        "parcel_envelope",
        "box",
        "envelope",
        "package",
    }
)
_DOOR_ONLY_ENTITY_TYPES = frozenset(
    {
        "washer",
        "washing_machine",
        "washer_door",
        "dishwasher_door",
    }
)
_APPLIANCE_ENTITY_TYPES = frozenset({"appliance", "dishwasher"})
_RACK_ENTITY_TYPES = frozenset({"rack", "dishwasher_rack", "rack_fixture"})
_SLOT_ENTITY_TYPES = frozenset({"slot", "managed_slot", "rack_slot", "dishwasher_slot"})


class AttributeUpdateMode(StrEnum):
    """How an Observation's attributes update the existing entity map."""

    COMPLETE = "complete"
    PARTIAL = "partial"


ObservedAttributesMode = AttributeUpdateMode
AttributesMode = AttributeUpdateMode


def _printable_text(value: str) -> bool:
    return value.isprintable()


def _applicable_keys(entity_type: str | None) -> frozenset[str] | None:
    if entity_type is None:
        return None
    normalized = entity_type.strip().casefold().replace("-", "_")
    if normalized in _PARCEL_ENTITY_TYPES:
        return PARCEL_ATTRIBUTE_KEYS
    if normalized in _APPLIANCE_ENTITY_TYPES:
        return APPLIANCE_ATTRIBUTE_KEYS
    if normalized in _DOOR_ONLY_ENTITY_TYPES:
        return frozenset({"door_state"})
    if normalized in _RACK_ENTITY_TYPES:
        return frozenset({"rack_state"})
    if normalized in _SLOT_ENTITY_TYPES:
        return MANAGED_SLOT_ATTRIBUTE_KEYS
    return None


def validate_observed_attributes(
    value: object,
    *,
    entity_type: str | None = None,
    allow_unknown_keys: bool = False,
) -> dict[str, str]:
    """Validate and detach one bounded observed-attribute mapping."""
    if type(value) is not dict:
        raise ValueError("attributes must be a string-to-string mapping")
    if len(value) > MAX_ATTRIBUTE_COUNT:
        raise ValueError(f"attributes must contain at most {MAX_ATTRIBUTE_COUNT} entries")
    if entity_type is not None and (type(entity_type) is not str or not entity_type.strip()):
        raise ValueError("attributes entity_type must be a non-empty string")

    applicable = _applicable_keys(entity_type)
    normalized: dict[str, str] = {}
    for key, item in value.items():
        if type(key) is not str or not key.strip():
            raise ValueError("attributes keys must be non-empty strings")
        try:
            key.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("attributes must be valid UTF-8 JSON strings") from error
        if len(key) > MAX_ATTRIBUTE_KEY_LENGTH:
            raise ValueError(f"attributes keys must be at most {MAX_ATTRIBUTE_KEY_LENGTH} characters")
        if not _printable_text(key) or key != key.strip():
            raise ValueError("attributes keys must contain printable text without surrounding whitespace")
        if key not in SUPPORTED_ATTRIBUTE_KEYS and not allow_unknown_keys:
            raise ValueError(f"attributes key {key!r} is not supported")
        if applicable is not None and key not in applicable:
            raise ValueError(f"attributes key {key!r} is not supported for entity_type {entity_type!r}")
        if type(item) is not str or not item.strip():
            raise ValueError("attributes values must be non-empty strings")
        try:
            item.encode("utf-8")
        except UnicodeEncodeError as error:
            raise ValueError("attributes must be valid UTF-8 JSON strings") from error
        if len(item) > MAX_ATTRIBUTE_VALUE_LENGTH:
            raise ValueError(f"attributes values must be at most {MAX_ATTRIBUTE_VALUE_LENGTH} characters")
        if item != item.strip() or not _printable_text(item):
            raise ValueError("attributes values must contain printable text without surrounding whitespace")
        allowed_values = ATTRIBUTE_ENUMS.get(key)
        if allowed_values is not None and item not in allowed_values:
            raise ValueError(f"attributes value {item!r} is not valid for key {key!r}")
        normalized[key] = item

    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as error:
        raise ValueError("attributes must be valid UTF-8 JSON strings") from error
    if len(encoded) > MAX_ATTRIBUTES_JSON_BYTES:
        raise ValueError(
            f"attributes canonical UTF-8 JSON must be at most {MAX_ATTRIBUTES_JSON_BYTES} bytes"
        )
    return normalized


def observed_attributes_json_schema() -> dict[str, object]:
    """Return the schema fragment for the bounded attribute map."""
    properties: dict[str, dict[str, object]] = {}
    for key in sorted(SUPPORTED_ATTRIBUTE_KEYS):
        definition: dict[str, object] = {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_ATTRIBUTE_VALUE_LENGTH,
            "pattern": "^[ -~]+$",
        }
        allowed_values = ATTRIBUTE_ENUMS.get(key)
        if allowed_values is not None:
            definition["enum"] = sorted(allowed_values)
        properties[key] = definition
    return {
        "type": "object",
        "description": (
            "Finite observed attributes. Keys are limited to the documented vocabulary; "
            "canonical UTF-8 JSON is limited to 4096 bytes."
        ),
        "maxProperties": MAX_ATTRIBUTE_COUNT,
        "properties": properties,
        "additionalProperties": False,
    }


OBSERVED_ATTRIBUTES_JSON_SCHEMA = observed_attributes_json_schema()


def _pydantic_validate_observed_attributes(value: object) -> dict[str, str]:
    return validate_observed_attributes(value)


ObservedAttributes = Annotated[
    dict[str, str],
    BeforeValidator(_pydantic_validate_observed_attributes),
    WithJsonSchema(deepcopy(OBSERVED_ATTRIBUTES_JSON_SCHEMA)),
]


def attribute_keys_for_entity_type(entity_type: str) -> frozenset[str] | None:
    """Return the restricted vocabulary for a recognized entity type."""
    return _applicable_keys(entity_type)


__all__ = [
    "APPLIANCE_ATTRIBUTE_KEYS",
    "ATTRIBUTE_ENUMS",
    "MANAGED_SLOT_ATTRIBUTE_KEYS",
    "MAX_ATTRIBUTES_JSON_BYTES",
    "MAX_ATTRIBUTE_COUNT",
    "MAX_ATTRIBUTE_KEY_LENGTH",
    "MAX_ATTRIBUTE_VALUE_LENGTH",
    "OBSERVED_ATTRIBUTES_JSON_SCHEMA",
    "PARCEL_ATTRIBUTE_KEYS",
    "SUPPORTED_ATTRIBUTE_KEYS",
    "AttributeUpdateMode",
    "AttributesMode",
    "ObservedAttributes",
    "ObservedAttributesMode",
    "attribute_keys_for_entity_type",
    "observed_attributes_json_schema",
    "validate_observed_attributes",
]
