"""Bounded observed-attribute vocabulary shared by producers and consumers."""

from __future__ import annotations

import json
import math
from copy import deepcopy
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, WithJsonSchema, field_validator
from pydantic.json_schema import SkipJsonSchema

MAX_ATTRIBUTE_COUNT: Final = 32
MAX_ATTRIBUTE_KEY_LENGTH: Final = 64
MAX_ATTRIBUTE_VALUE_LENGTH: Final = 256
MAX_ATTRIBUTES_JSON_BYTES: Final = 4096
MAX_ATTRIBUTE_METADATA_JSON_BYTES: Final = 16 * 1024
MAX_ATTRIBUTE_METADATA_TEXT_LENGTH: Final = 256
MAX_ATTRIBUTE_EVIDENCE_REF_COUNT: Final = 32
MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH: Final = 256
ATTRIBUTE_SCHEMA_VERSION: Final = "observed-attributes-v1"
LEGACY_ATTRIBUTE_MIGRATION_VERSION: Final = "legacy-observed-attributes-v0"
ATTRIBUTE_PRINTABLE_PATTERN: Final = r"^[^\u0000-\u001f\u007f-\u009f]+$"
ATTRIBUTE_BELIEF_VALUES: Final = frozenset({"observed", "inferred", "stale", "lost"})
ATTRIBUTE_SCHEMA_VERSIONS: Final = frozenset({ATTRIBUTE_SCHEMA_VERSION, LEGACY_ATTRIBUTE_MIGRATION_VERSION})

GENERIC_ATTRIBUTE_KEYS = frozenset({"colour", "presence", "identity", "orientation"})

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
    GENERIC_ATTRIBUTE_KEYS | PARCEL_ATTRIBUTE_KEYS | APPLIANCE_ATTRIBUTE_KEYS | MANAGED_SLOT_ATTRIBUTE_KEYS
)

ATTRIBUTE_ENUMS = MappingProxyType(
    {
        "label_status": frozenset({"verified", "unreadable", "missing", "unknown"}),
        "condition": frozenset({"intact", "damaged", "unknown"}),
        "door_state": frozenset({"open", "closed", "unknown"}),
        "rack_state": frozenset({"open", "closed", "unknown"}),
        "slot_state": frozenset({"empty", "occupied", "blocked", "unknown"}),
        "slot_occupancy": frozenset({"empty", "occupied", "blocked", "unknown"}),
        "presence": frozenset({"present", "absent", "unknown"}),
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

AttributeBelief = Literal["observed", "inferred", "stale", "lost"]


class ObservedAttributeMetadata(BaseModel):
    """Evidence metadata attached to one observed attribute value."""

    model_config = ConfigDict(extra="forbid", strict=True)

    observed_at: str
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_refs: list[str] = Field(min_length=1)
    belief: AttributeBelief
    clock_id: Literal["monotonic", "wall"] = "monotonic"
    source: str | SkipJsonSchema[None] = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )

    @field_validator("observed_at", mode="before")
    @classmethod
    def normalize_observed_at(cls, value: object) -> str:
        return _validate_metadata_text(value, "attribute metadata observed_at")

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: object) -> float:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("attribute metadata confidence must be a JSON number")
        confidence = float(value)
        if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
            raise ValueError("attribute metadata confidence must be finite and between 0 and 1")
        return confidence

    @field_validator("evidence_refs", mode="before")
    @classmethod
    def validate_evidence_refs(cls, value: object) -> object:
        return validate_attribute_evidence_refs(value, field_name="attribute metadata evidence_refs")

    @field_validator("clock_id", mode="before")
    @classmethod
    def normalize_clock_id(cls, value: object) -> str:
        if isinstance(value, StrEnum):
            value = value.value
        if type(value) is not str or value not in {"monotonic", "wall"}:
            raise ValueError("attribute metadata clock_id must be monotonic or wall")
        return value

    @field_validator("source", mode="before")
    @classmethod
    def reject_null_source(cls, value: object) -> object:
        if value is None:
            raise ValueError("attribute metadata source may be omitted but cannot be null")
        return _validate_metadata_text(value, "attribute metadata source")


AttributeMetadata = ObservedAttributeMetadata


def _printable_text(value: str) -> bool:
    return value.isprintable()


def _validate_metadata_text(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8") from error
    if len(value) > MAX_ATTRIBUTE_METADATA_TEXT_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_ATTRIBUTE_METADATA_TEXT_LENGTH} characters")
    if len(encoded) > MAX_ATTRIBUTE_METADATA_TEXT_LENGTH * 4:
        raise ValueError(f"{label} must be at most {MAX_ATTRIBUTE_METADATA_TEXT_LENGTH * 4} UTF-8 bytes")
    if not _printable_text(value) or value != value.strip():
        raise ValueError(f"{label} must contain printable text without surrounding whitespace")
    return value


def _validate_evidence_reference(value: object, label: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    try:
        encoded = value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError(f"{label} must be valid UTF-8") from error
    if len(value) > MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH} characters")
    if len(encoded) > MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH:
        raise ValueError(f"{label} must be at most {MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH} UTF-8 bytes")
    if not _printable_text(value) or value != value.strip():
        raise ValueError(f"{label} must contain printable text without surrounding whitespace")
    return value


def validate_attribute_evidence_refs(
    value: object,
    *,
    field_name: str = "attribute evidence_refs",
    require_non_empty: bool = True,
) -> list[str]:
    """Validate the bounded, ordered evidence list attached to one attribute."""
    if type(value) is not list:
        raise ValueError(f"{field_name} must be a list of strings")
    if require_non_empty and not value:
        raise ValueError(f"{field_name} must be a non-empty list of strings")
    if len(value) > MAX_ATTRIBUTE_EVIDENCE_REF_COUNT:
        raise ValueError(f"{field_name} must contain at most {MAX_ATTRIBUTE_EVIDENCE_REF_COUNT} entries")
    normalized: list[str] = []
    for reference in value:
        normalized.append(_validate_evidence_reference(reference, f"{field_name} item"))
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{field_name} must not contain duplicates")
    return normalized


def _validate_attribute_key(
    key: object,
    *,
    applicable: frozenset[str] | None,
    allow_unknown_keys: bool,
) -> str:
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
    if applicable is not None and key not in applicable and not allow_unknown_keys:
        raise ValueError(f"attributes key {key!r} is not supported for entity_type")
    return key


def _applicable_keys(entity_type: str | None) -> frozenset[str] | None:
    if entity_type is None:
        return None
    normalized = entity_type.strip().casefold().replace("-", "_")
    # Legacy reducer events do not carry an entity type.  The reducer records
    # that uncertainty explicitly as ``legacy``; migration may therefore use
    # any key from the finite vocabulary, but it must never bypass the
    # vocabulary itself.
    if normalized == "legacy":
        return SUPPORTED_ATTRIBUTE_KEYS
    if normalized in _PARCEL_ENTITY_TYPES:
        return GENERIC_ATTRIBUTE_KEYS | PARCEL_ATTRIBUTE_KEYS
    if normalized in _APPLIANCE_ENTITY_TYPES:
        return GENERIC_ATTRIBUTE_KEYS | APPLIANCE_ATTRIBUTE_KEYS
    if normalized in _DOOR_ONLY_ENTITY_TYPES:
        return GENERIC_ATTRIBUTE_KEYS | frozenset({"door_state"})
    if normalized in _RACK_ENTITY_TYPES:
        return GENERIC_ATTRIBUTE_KEYS | frozenset({"rack_state"})
    if normalized in _SLOT_ENTITY_TYPES:
        return GENERIC_ATTRIBUTE_KEYS | MANAGED_SLOT_ATTRIBUTE_KEYS
    return GENERIC_ATTRIBUTE_KEYS


def validate_observed_attributes(
    value: object,
    *,
    entity_type: str | None = None,
    allow_unknown_keys: bool = False,
) -> dict[str, str]:
    """Validate and detach one bounded observed-attribute mapping."""
    if type(value) is not dict:
        raise ValueError("attributes must be a string-to-string mapping")
    if type(allow_unknown_keys) is not bool:
        raise ValueError("allow_unknown_keys must be a boolean")
    if len(value) > MAX_ATTRIBUTE_COUNT:
        raise ValueError(f"attributes must contain at most {MAX_ATTRIBUTE_COUNT} entries")
    if entity_type is not None and (type(entity_type) is not str or not entity_type.strip()):
        raise ValueError("attributes entity_type must be a non-empty string")

    applicable = _applicable_keys(entity_type)
    normalized: dict[str, str] = {}
    for key, item in value.items():
        key = _validate_attribute_key(
            key,
            applicable=applicable,
            allow_unknown_keys=allow_unknown_keys,
        )
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
        raise ValueError(f"attributes canonical UTF-8 JSON must be at most {MAX_ATTRIBUTES_JSON_BYTES} bytes")
    return normalized


def validate_attribute_metadata_map(
    value: object,
    *,
    attribute_keys: set[str] | frozenset[str] | None = None,
    entity_type: str | None = None,
    allow_unknown_keys: bool = False,
    require_observed: bool = False,
    require_complete: bool = False,
    expected_clock_id: object | None = None,
) -> dict[str, dict[str, object]]:
    """Validate and detach metadata for a bounded attribute mapping."""
    if type(value) is not dict:
        raise ValueError("attribute_metadata must be a mapping")
    if type(allow_unknown_keys) is not bool:
        raise ValueError("allow_unknown_keys must be a boolean")
    if type(require_observed) is not bool or type(require_complete) is not bool:
        raise ValueError("metadata completeness flags must be booleans")
    if len(value) > MAX_ATTRIBUTE_COUNT:
        raise ValueError(f"attribute_metadata must contain at most {MAX_ATTRIBUTE_COUNT} entries")
    if attribute_keys is not None:
        if not isinstance(attribute_keys, (set, frozenset)) or any(type(key) is not str for key in attribute_keys):
            raise ValueError("attribute_keys must be a set of strings")
        for key in attribute_keys:
            _validate_attribute_key(
                key,
                applicable=_applicable_keys(entity_type),
                allow_unknown_keys=allow_unknown_keys,
            )
        missing = set(attribute_keys) - set(value)
        if require_complete and missing:
            raise ValueError(f"attribute_metadata is missing keys for attribute values: {sorted(missing)}")

    expected_clock = None
    if expected_clock_id is not None:
        expected_clock = expected_clock_id.value if isinstance(expected_clock_id, StrEnum) else expected_clock_id
        if expected_clock not in {"monotonic", "wall"}:
            raise ValueError("attribute metadata expected clock_id must be monotonic or wall")

    applicable = _applicable_keys(entity_type)
    normalized: dict[str, dict[str, object]] = {}
    for raw_key, raw_metadata in value.items():
        key = _validate_attribute_key(
            raw_key,
            applicable=applicable,
            allow_unknown_keys=allow_unknown_keys,
        )
        if attribute_keys is not None and key not in attribute_keys:
            raise ValueError(f"attribute_metadata contains keys without attribute values: {[key]}")
        try:
            metadata = ObservedAttributeMetadata.model_validate(raw_metadata)
        except (TypeError, ValueError) as error:
            raise ValueError(f"attribute_metadata[{key!r}] is invalid: {error}") from error
        if require_observed and metadata.belief != "observed":
            raise ValueError(f"attribute_metadata[{key!r}] belief must be observed at ingestion")
        if expected_clock is not None and metadata.clock_id != expected_clock:
            raise ValueError(f"attribute_metadata[{key!r}] clock_id must match the Observation clock_id")
        normalized[key] = metadata.model_dump(mode="json", exclude_none=True)
    try:
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeEncodeError) as error:
        raise ValueError("attribute_metadata must be valid UTF-8 JSON") from error
    if len(encoded) > MAX_ATTRIBUTE_METADATA_JSON_BYTES:
        raise ValueError(
            f"attribute_metadata canonical UTF-8 JSON must be at most {MAX_ATTRIBUTE_METADATA_JSON_BYTES} bytes"
        )
    return normalized


def materialize_attribute_metadata(
    attributes: dict[str, str],
    metadata: object | None,
    *,
    observed_at: object,
    confidence: object,
    evidence_refs: object,
    clock_id: object,
    source: object | None = None,
    entity_type: str | None = None,
    allow_unknown_keys: bool = False,
) -> dict[str, dict[str, object]]:
    """Fill omitted per-attribute metadata from the enclosing observation."""
    validate_observed_attributes(
        attributes,
        entity_type=entity_type,
        allow_unknown_keys=allow_unknown_keys,
    )
    explicit = (
        validate_attribute_metadata_map(
            metadata,
            attribute_keys=set(attributes),
            entity_type=entity_type,
            allow_unknown_keys=allow_unknown_keys,
            require_observed=True,
            expected_clock_id=clock_id,
        )
        if metadata is not None
        else {}
    )
    defaults: dict[str, object] = {
        "observed_at": observed_at,
        "confidence": confidence,
        "evidence_refs": evidence_refs,
        "belief": "observed",
        "clock_id": clock_id.value if isinstance(clock_id, StrEnum) else clock_id,
    }
    if source is not None:
        defaults["source"] = source

    normalized: dict[str, dict[str, object]] = {}
    for key in attributes:
        candidate = dict(defaults)
        candidate.update(explicit.get(key, {}))
        try:
            item = ObservedAttributeMetadata.model_validate(candidate)
        except (TypeError, ValueError) as error:
            raise ValueError(f"attribute_metadata[{key!r}] is invalid: {error}") from error
        if item.belief != "observed":
            raise ValueError(f"attribute_metadata[{key!r}] belief must be observed at ingestion")
        normalized[key] = item.model_dump(mode="json", exclude_none=True)
    validate_attribute_metadata_map(
        normalized,
        attribute_keys=set(attributes),
        entity_type=entity_type,
        allow_unknown_keys=allow_unknown_keys,
        require_observed=True,
        require_complete=True,
        expected_clock_id=clock_id,
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
            "pattern": ATTRIBUTE_PRINTABLE_PATTERN,
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


def observed_attribute_metadata_json_schema() -> dict[str, object]:
    """Return the schema fragment for one attribute's evidence metadata."""
    text_definition = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_ATTRIBUTE_METADATA_TEXT_LENGTH,
        "pattern": ATTRIBUTE_PRINTABLE_PATTERN,
    }
    evidence_definition = {
        "type": "string",
        "minLength": 1,
        "maxLength": MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH,
        "pattern": ATTRIBUTE_PRINTABLE_PATTERN,
    }
    return {
        "type": "object",
        "description": "Timestamp, confidence, belief and evidence for one observed attribute.",
        "additionalProperties": False,
        "properties": {
            "observed_at": text_definition,
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_refs": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_ATTRIBUTE_EVIDENCE_REF_COUNT,
                "items": evidence_definition,
            },
            "belief": {"type": "string", "enum": sorted(ATTRIBUTE_BELIEF_VALUES)},
            "clock_id": {"type": "string", "enum": ["monotonic", "wall"], "default": "monotonic"},
            "source": text_definition,
        },
        "required": ["observed_at", "confidence", "evidence_refs", "belief"],
    }


OBSERVED_ATTRIBUTE_METADATA_JSON_SCHEMA = observed_attribute_metadata_json_schema()


def observed_attribute_metadata_map_json_schema() -> dict[str, object]:
    """Return the finite-key schema fragment for an attribute metadata map."""
    return {
        "type": "object",
        "description": (
            "Metadata keys match the bounded observed-attribute vocabulary; "
            "canonical UTF-8 JSON is limited to 16384 bytes."
        ),
        "maxProperties": MAX_ATTRIBUTE_COUNT,
        "properties": {
            key: deepcopy(OBSERVED_ATTRIBUTE_METADATA_JSON_SCHEMA) for key in sorted(SUPPORTED_ATTRIBUTE_KEYS)
        },
        "additionalProperties": False,
    }


OBSERVED_ATTRIBUTE_METADATA_MAP_JSON_SCHEMA = observed_attribute_metadata_map_json_schema()


def _pydantic_validate_attribute_metadata_map(value: object) -> object:
    # Entity-specific and version-specific checks happen in Observation and
    # WorldEntity. This field-level pass still enforces metadata shape/bounds.
    validate_attribute_metadata_map(value)
    return value


AttributeMetadataMap = Annotated[
    dict[str, ObservedAttributeMetadata],
    BeforeValidator(_pydantic_validate_attribute_metadata_map),
    WithJsonSchema(deepcopy(OBSERVED_ATTRIBUTE_METADATA_MAP_JSON_SCHEMA)),
]


def _pydantic_validate_observed_attributes(value: object) -> dict[str, str]:
    return validate_observed_attributes(value)


ObservedAttributes = Annotated[
    dict[str, str],
    BeforeValidator(_pydantic_validate_observed_attributes),
    WithJsonSchema(deepcopy(OBSERVED_ATTRIBUTES_JSON_SCHEMA)),
]


def _pydantic_validate_versioned_observed_attributes(value: object) -> dict[str, str]:
    # The enclosing model applies entity-type and migration semantics. The
    # field-level validator enforces the bounded vocabulary and value shape.
    return validate_observed_attributes(value)


VersionedObservedAttributes = Annotated[
    dict[str, str],
    BeforeValidator(_pydantic_validate_versioned_observed_attributes),
    WithJsonSchema(deepcopy(OBSERVED_ATTRIBUTES_JSON_SCHEMA)),
]


def attribute_keys_for_entity_type(entity_type: str) -> frozenset[str] | None:
    """Return the restricted vocabulary for a recognized entity type."""
    return _applicable_keys(entity_type)


def legacy_attribute_keys_allowed(entity_type: str | None) -> bool:
    """Return whether a legacy payload may bypass the finite v1 vocabulary.

    Issue #168 uses the legacy marker only to migrate missing metadata and
    schema-version fields. It never grants arbitrary keys; the return value is
    kept as a named policy hook so all consumers apply the same decision.
    """
    del entity_type
    return False


__all__ = [
    "APPLIANCE_ATTRIBUTE_KEYS",
    "ATTRIBUTE_BELIEF_VALUES",
    "ATTRIBUTE_ENUMS",
    "ATTRIBUTE_PRINTABLE_PATTERN",
    "ATTRIBUTE_SCHEMA_VERSION",
    "ATTRIBUTE_SCHEMA_VERSIONS",
    "GENERIC_ATTRIBUTE_KEYS",
    "LEGACY_ATTRIBUTE_MIGRATION_VERSION",
    "MANAGED_SLOT_ATTRIBUTE_KEYS",
    "MAX_ATTRIBUTES_JSON_BYTES",
    "MAX_ATTRIBUTE_COUNT",
    "MAX_ATTRIBUTE_EVIDENCE_REF_COUNT",
    "MAX_ATTRIBUTE_EVIDENCE_REF_LENGTH",
    "MAX_ATTRIBUTE_KEY_LENGTH",
    "MAX_ATTRIBUTE_METADATA_JSON_BYTES",
    "MAX_ATTRIBUTE_METADATA_TEXT_LENGTH",
    "MAX_ATTRIBUTE_VALUE_LENGTH",
    "OBSERVED_ATTRIBUTES_JSON_SCHEMA",
    "OBSERVED_ATTRIBUTE_METADATA_JSON_SCHEMA",
    "OBSERVED_ATTRIBUTE_METADATA_MAP_JSON_SCHEMA",
    "PARCEL_ATTRIBUTE_KEYS",
    "SUPPORTED_ATTRIBUTE_KEYS",
    "AttributeBelief",
    "AttributeMetadata",
    "AttributeMetadataMap",
    "AttributeUpdateMode",
    "AttributesMode",
    "ObservedAttributeMetadata",
    "ObservedAttributes",
    "ObservedAttributesMode",
    "VersionedObservedAttributes",
    "attribute_keys_for_entity_type",
    "legacy_attribute_keys_allowed",
    "materialize_attribute_metadata",
    "observed_attribute_metadata_json_schema",
    "observed_attribute_metadata_map_json_schema",
    "observed_attributes_json_schema",
    "validate_attribute_evidence_refs",
    "validate_attribute_metadata_map",
    "validate_observed_attributes",
]
