from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, field_validator
from workbench_contracts import (
    ATTRIBUTE_SCHEMA_VERSION,
    ClockId,
    ReasonCode,
    RecoveryHint,
    VerificationResult,
    VerificationStatus,
    WorldBelief,
    validate_observed_attributes,
)

from .reducer import WorldState

_CANONICAL_STATE_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_UTC_WALL_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")


def _validated_state_hash(value: object) -> str:
    if not isinstance(value, str) or _CANONICAL_STATE_HASH_PATTERN.fullmatch(value) is None:
        raise ValueError("state_hash must be a 64-character lowercase SHA-256 hex digest")
    return value


def _validated_utc_wall_time(value: object) -> str:
    if not isinstance(value, str) or _UTC_WALL_TIME_PATTERN.fullmatch(value) is None:
        raise ValueError("verified_at must be an RFC3339 UTC wall-clock timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError("verified_at must be an RFC3339 UTC wall-clock timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("verified_at must use UTC")
    return value


class VerificationContext(BaseModel):
    """Validated metadata supplied at the World Model verification boundary."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_hash: str
    verified_at: str
    clock_id: ClockId

    @field_validator("state_hash", mode="before")
    @classmethod
    def validate_state_hash(cls, value: object) -> str:
        return _validated_state_hash(value)

    @field_validator("verified_at", mode="before")
    @classmethod
    def validate_verified_at(cls, value: object) -> str:
        return _validated_utc_wall_time(value)

    @field_validator("clock_id", mode="before")
    @classmethod
    def require_wall_clock(cls, value: object) -> ClockId:
        if value not in (ClockId.WALL, ClockId.WALL.value):
            raise ValueError("VerificationContext requires clock_id=wall")
        return ClockId.WALL


NO_EVIDENCE_REF = "system://world-state/no-evidence"
DEFAULT_PARCEL_ROUTES = {
    "parcel_box": "pickup_shelf",
    "parcel_envelope": "pickup_shelf",
    "parcel_damaged": "quarantine_bin",
}
DEFAULT_PARCEL_ATTRIBUTES = {
    "parcel_box": {"label_status": "verified", "condition": "intact"},
    "parcel_envelope": {"label_status": "verified", "condition": "intact"},
    "parcel_damaged": {"label_status": "verified", "condition": "damaged"},
}
PARCEL_IDENTITY_KEYS = ("tracking_id", "barcode", "parcel_uid")
OBJECT_IN_TRAY_RULE_VERSION = "tray-membership-v1"
KIT_CONTENTS_RULE_VERSION = "kit-contents-v1"
INSPECTION_EVIDENCE_RULE_VERSION = "inspection-evidence-v1"
WORKSPACE_CLEARANCE_RULE_VERSION = "workspace-clearance-v1"
PARCEL_SORTING_RULE_VERSION = "parcel-sorting-v1"
PARCEL_POLICY_RULE_VERSION = "parcel-policy-v2"


__all__ = [
    "NO_EVIDENCE_REF",
    "VerificationContext",
    "verify_inspection_evidence",
    "verify_kit_contents",
    "verify_object_in_tray",
    "verify_parcel_policy",
    "verify_parcel_sorting",
    "verify_workspace_clearance",
]


def _entity_evidence(state: WorldState, entity_ids: set[str]) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    for entity_id in sorted(entity_ids):
        for reference in state.entity_evidence_refs.get(entity_id, []):
            if reference not in seen:
                evidence.append(reference)
                seen.add(reference)
    return evidence


def _missing_entity_evidence(state: WorldState, entity_ids: set[str]) -> list[str]:
    return sorted(entity_id for entity_id in entity_ids if not state.entity_evidence_refs.get(entity_id))


def _attribute_evidence(
    state: WorldState,
    attribute_keys: Mapping[str, set[str] | frozenset[str] | list[str] | tuple[str, ...]],
) -> list[str]:
    evidence: list[str] = []
    seen: set[str] = set()
    for entity_id in sorted(attribute_keys):
        attributes = state.entity_attributes.get(entity_id, {})
        metadata_by_key = state.entity_attribute_metadata.get(entity_id, {})
        for attribute_key in sorted(attribute_keys[entity_id]):
            if attribute_key not in attributes:
                continue
            metadata = metadata_by_key.get(attribute_key, {})
            refs = metadata.get("evidence_refs", []) if isinstance(metadata, Mapping) else []
            if not isinstance(refs, list):
                continue
            for reference in refs:
                if isinstance(reference, str) and reference not in seen:
                    evidence.append(reference)
                    seen.add(reference)
    return evidence


def _attribute_quality(
    state: WorldState,
    entity_id: str,
    attribute_key: str,
    confidence_threshold: float,
) -> str:
    attributes = state.entity_attributes.get(entity_id, {})
    if attribute_key not in attributes:
        return "missing"
    metadata = state.entity_attribute_metadata.get(entity_id, {}).get(attribute_key)
    version = state.entity_attribute_schema_versions.get(entity_id)
    if metadata is None:
        return "missing" if version == ATTRIBUTE_SCHEMA_VERSION else "ok"
    if not isinstance(metadata, Mapping):
        return "missing"
    belief = metadata.get("belief")
    if belief in {WorldBelief.STALE.value, WorldBelief.LOST.value}:
        return "expired"
    if belief != WorldBelief.OBSERVED.value:
        return "insufficient"
    evidence_refs = metadata.get("evidence_refs")
    if (
        not isinstance(evidence_refs, list)
        or not evidence_refs
        or any(type(reference) is not str or not reference.strip() for reference in evidence_refs)
    ):
        return "insufficient"
    confidence = metadata.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, int | float):
        return "insufficient"
    if not math.isfinite(float(confidence)) or float(confidence) < confidence_threshold:
        return "low_confidence"
    return "ok"


def _attribute_quality_summary(
    state: WorldState,
    required: Mapping[str, set[str] | frozenset[str] | list[str] | tuple[str, ...]],
    confidence_threshold: float,
) -> tuple[list[str], list[str], list[str], list[str], dict[str, set[str]]]:
    missing: list[str] = []
    expired: list[str] = []
    low_confidence: list[str] = []
    insufficient: list[str] = []
    evidence_keys: dict[str, set[str]] = {}
    for entity_id in sorted(required):
        for attribute_key in sorted(required[entity_id]):
            quality = _attribute_quality(state, entity_id, attribute_key, confidence_threshold)
            if quality != "missing":
                evidence_keys.setdefault(entity_id, set()).add(attribute_key)
            label = f"{entity_id}.{attribute_key}"
            if quality == "missing":
                missing.append(label)
            elif quality == "expired":
                expired.append(label)
            elif quality == "low_confidence":
                low_confidence.append(label)
            elif quality == "insufficient":
                insufficient.append(label)
    return missing, expired, low_confidence, insufficient, evidence_keys


def _append_unique(values: list[str], additions: list[str]) -> None:
    """Append deterministic diagnostics without reporting one defect twice."""
    seen = set(values)
    for value in additions:
        if value not in seen:
            values.append(value)
            seen.add(value)


def _required_entity_set(entity_ids: list[str], label: str) -> set[str]:
    if not entity_ids or any(not isinstance(entity_id, str) or not entity_id.strip() for entity_id in entity_ids):
        raise ValueError(f"{label} requires non-empty entity IDs")
    if len(entity_ids) != len(set(entity_ids)):
        raise ValueError(f"{label} requires unique entity IDs")
    return set(entity_ids)


def _validate_confidence_threshold(value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0.0 <= value <= 1.0:
        raise ValueError("confidence_threshold must be between 0 and 1")


def _normalize_parcel_identity(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"parcel identity {label} must be a non-empty string")
    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[\s-]+", "", normalized)
    if not normalized:
        raise ValueError(f"parcel identity {label} must contain readable characters")
    return normalized


def _validate_parcel_manifest(
    required: set[str],
    parcel_manifest: Mapping[str, Mapping[str, str]] | None,
    manifest_id: str | None,
) -> tuple[str | None, dict[str, dict[str, str]]]:
    if parcel_manifest is None:
        if manifest_id is not None:
            raise ValueError("manifest_id requires parcel_manifest")
        return None, {}
    if not isinstance(manifest_id, str) or not manifest_id.strip():
        raise ValueError("manifest_id must be a non-empty string")
    if not isinstance(parcel_manifest, Mapping):
        raise ValueError("parcel_manifest must be a mapping")
    if set(parcel_manifest) != required:
        raise ValueError("parcel_manifest must contain exactly the required parcel IDs")

    expected_identities: dict[str, dict[str, str]] = {}
    seen: dict[str, tuple[str, str]] = {}
    for parcel_id, attributes in parcel_manifest.items():
        if not isinstance(attributes, Mapping):
            raise ValueError("parcel manifest entries must be mappings")
        unsupported = set(attributes) - set(PARCEL_IDENTITY_KEYS)
        if unsupported:
            raise ValueError(f"parcel manifest contains unsupported identity keys: {', '.join(sorted(unsupported))}")
        expected_identities[parcel_id] = {}
        for key in PARCEL_IDENTITY_KEYS:
            if attributes.get(key) is None:
                continue
            identity = _normalize_parcel_identity(attributes[key], key)
            previous = seen.get(identity)
            if previous is not None and previous[0] != parcel_id:
                raise ValueError(
                    f"duplicate parcel manifest identity: {previous[0]}.{previous[1]} and {parcel_id}.{key}"
                )
            seen[identity] = (parcel_id, key)
            expected_identities[parcel_id][key] = identity
        if not expected_identities[parcel_id]:
            raise ValueError(f"parcel manifest requires an identity for {parcel_id}")
    return manifest_id.strip(), expected_identities


def _normalized_request_value(value: object) -> object:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("request semantics mappings require string keys")
        return {key: _normalized_request_value(value[key]) for key in sorted(value)}
    if isinstance(value, set | frozenset):
        normalized_items = [_normalized_request_value(item) for item in value]
        return sorted(
            normalized_items,
            key=lambda item: json.dumps(
                item,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    if isinstance(value, list | tuple):
        return [_normalized_request_value(item) for item in value]
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("request semantics numbers must be finite")
        return value
    raise TypeError(f"unsupported request semantics value: {type(value).__name__}")


def _normalized_request_semantics(request_semantics: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(request_semantics, Mapping):
        raise TypeError("request_semantics must be a mapping")
    normalized = _normalized_request_value(request_semantics)
    if not isinstance(normalized, dict):
        raise TypeError("request_semantics must normalize to a mapping")
    return normalized


def _verification_id(
    *,
    run_id: str,
    task_id: str,
    state_hash: str,
    claim: str,
    status: VerificationStatus,
    reason_code: ReasonCode | None,
    evidence_refs: list[str],
    recovery_hint: RecoveryHint,
    rule_version: str,
    verifier_kind: str,
    request_semantics: Mapping[str, object],
) -> str:
    if not isinstance(verifier_kind, str) or not verifier_kind.strip():
        raise ValueError("verifier_kind must be a non-empty string")
    material = {
        "verifier_kind": verifier_kind,
        "request_semantics": _normalized_request_semantics(request_semantics),
        "run_id": run_id,
        "task_id": task_id,
        "state_hash": _validated_state_hash(state_hash),
        "claim": claim,
        "status": status.value,
        "reason_code": None if reason_code is None else reason_code.value,
        "evidence_refs": list(evidence_refs),
        "recovery_hint": recovery_hint.value,
        "rule_version": rule_version,
    }
    canonical_bytes = json.dumps(
        material,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return f"ver-{hashlib.sha256(canonical_bytes).hexdigest()}"


def _default_verification_context(state: WorldState) -> VerificationContext:
    def canonicalize(value: object) -> object:
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) for key in value):
                raise TypeError("WorldState mappings require string keys")
            return {key: canonicalize(value[key]) for key in sorted(value)}
        if isinstance(value, (list, tuple)):
            return [canonicalize(item) for item in value]
        if isinstance(value, (set, frozenset)):
            normalized = [canonicalize(item) for item in value]
            return sorted(
                normalized,
                key=lambda item: json.dumps(
                    item,
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ),
            )
        if isinstance(value, float):
            if math.isfinite(value):
                return value
            return {"__nonfinite_float__": value.hex()}
        if value is None or isinstance(value, (str, bool, int)):
            return value
        if hasattr(value, "value") and isinstance(value.value, (str, int, float, bool)):
            return canonicalize(value.value)
        raise TypeError(f"unsupported WorldState value: {type(value).__name__}")

    try:
        material = json.dumps(
            canonicalize(state.model_dump(mode="python", exclude={"freshness_evaluated"})),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("WorldState cannot produce verification metadata") from error
    return VerificationContext(
        state_hash=hashlib.sha256(material).hexdigest(),
        verified_at="1970-01-01T00:00:00Z",
        clock_id=ClockId.WALL,
    )


def _resolved_context(state: WorldState, context: VerificationContext | None) -> VerificationContext:
    if context is None:
        return _default_verification_context(state)
    if not isinstance(context, VerificationContext):
        raise TypeError("context must be a VerificationContext")
    return context


def _expired_support(state: WorldState, entity_ids: set[str]) -> list[str]:
    expired: list[str] = []
    for entity_id in sorted(entity_ids):
        entity_belief = state.entity_beliefs.get(entity_id)
        location_belief = state.entity_location_beliefs.get(entity_id)
        if entity_belief in {WorldBelief.STALE, WorldBelief.LOST} or location_belief in {
            WorldBelief.STALE,
            WorldBelief.LOST,
        }:
            expired.append(entity_id)
    return expired


def _result(
    state: WorldState,
    task_id: str,
    claim: str,
    status: VerificationStatus,
    reason_code: ReasonCode,
    recovery_hint: RecoveryHint,
    rule_version: str,
    supporting_entity_ids: set[str],
    *,
    verifier_kind: str = "world_model",
    request_semantics: Mapping[str, object] | None = None,
    context: VerificationContext | None = None,
    supporting_attribute_keys: Mapping[str, set[str] | frozenset[str] | list[str] | tuple[str, ...]] | None = None,
) -> VerificationResult:
    resolved_context = _resolved_context(state, context)
    if not state.freshness_evaluated and status in {
        VerificationStatus.CONFIRMED,
        VerificationStatus.REFUTED,
    }:
        status = VerificationStatus.INSUFFICIENT_EVIDENCE
        reason_code = ReasonCode.STALE_OBSERVATION
        recovery_hint = RecoveryHint.RE_OBSERVE
        claim = f"{claim}; freshness_not_evaluated"
    expired_support = _expired_support(state, supporting_entity_ids)
    if expired_support:
        status = VerificationStatus.INSUFFICIENT_EVIDENCE
        reason_code = ReasonCode.STALE_OBSERVATION
        recovery_hint = RecoveryHint.RE_OBSERVE
        claim = f"{claim}; stale_or_lost={expired_support}"
    evidence_refs = _entity_evidence(state, supporting_entity_ids)
    if supporting_attribute_keys:
        for reference in _attribute_evidence(state, supporting_attribute_keys):
            if reference not in evidence_refs:
                evidence_refs.append(reference)
    evidence_refs = evidence_refs or [NO_EVIDENCE_REF]
    semantics = {} if request_semantics is None else request_semantics
    verification_id = _verification_id(
        verifier_kind=verifier_kind,
        request_semantics=semantics,
        run_id=state.run_id,
        task_id=task_id,
        state_hash=resolved_context.state_hash,
        claim=claim,
        status=status,
        reason_code=reason_code,
        evidence_refs=evidence_refs,
        recovery_hint=recovery_hint,
        rule_version=rule_version,
    )
    return VerificationResult(
        verification_id=verification_id,
        run_id=state.run_id,
        task_id=task_id,
        claim=claim,
        status=status,
        reason_code=reason_code,
        evidence_refs=evidence_refs,
        recovery_hint=recovery_hint,
        verified_at=resolved_context.verified_at,
        clock_id=resolved_context.clock_id,
        rule_version=rule_version,
    )


def verify_object_in_tray(
    state: WorldState,
    task_id: str,
    object_id: str,
    tray_id: str,
    *,
    context: VerificationContext | None = None,
) -> VerificationResult:
    if not isinstance(object_id, str) or not object_id.strip() or not isinstance(tray_id, str) or not tray_id.strip():
        raise ValueError("object_id and tray_id must be non-empty")
    expected_location = f"in:{tray_id}"
    actual_location = state.entity_locations.get(object_id)
    if actual_location is None:
        status = VerificationStatus.INSUFFICIENT_EVIDENCE
        reason_code = ReasonCode.TARGET_NOT_OBSERVED
        recovery_hint = RecoveryHint.RE_OBSERVE
        claim = f"{object_id} inside {tray_id}: never observed"
    elif actual_location == expected_location and not state.entity_evidence_refs.get(object_id):
        status = VerificationStatus.INSUFFICIENT_EVIDENCE
        reason_code = ReasonCode.EVIDENCE_MISSING
        recovery_hint = RecoveryHint.RE_OBSERVE
        claim = f"{object_id} inside {tray_id}: relation has no evidence"
    elif actual_location == expected_location:
        status = VerificationStatus.CONFIRMED
        reason_code = ReasonCode.GOAL_SATISFIED
        recovery_hint = RecoveryHint.NONE
        claim = f"{object_id} inside {tray_id}"
    else:
        status = VerificationStatus.REFUTED
        reason_code = ReasonCode.GOAL_NOT_SATISFIED
        recovery_hint = RecoveryHint.RETRY_ACTION
        claim = f"{object_id} inside {tray_id}: found at {actual_location}"
    return _result(
        state,
        task_id,
        claim,
        status,
        reason_code,
        recovery_hint,
        OBJECT_IN_TRAY_RULE_VERSION,
        {object_id},
        verifier_kind="object_in_tray",
        request_semantics={"object_id": object_id, "tray_id": tray_id},
        context=context,
    )


def verify_kit_contents(
    state: WorldState,
    task_id: str,
    required_object_ids: list[str],
    tray_id: str = "kit_tray",
    confidence_threshold: float = 0.8,
    *,
    context: VerificationContext | None = None,
) -> VerificationResult:
    required = _required_entity_set(required_object_ids, "kitting")
    if not isinstance(tray_id, str) or not tray_id.strip():
        raise ValueError("tray_id must be non-empty")
    _validate_confidence_threshold(confidence_threshold)
    expected_location = f"in:{tray_id}"
    unobserved = sorted(object_id for object_id in required if object_id not in state.entity_locations)
    misplaced = sorted(
        object_id
        for object_id in required
        if object_id in state.entity_locations and state.entity_locations[object_id] != expected_location
    )
    extras = sorted(
        object_id
        for object_id, location in state.entity_locations.items()
        if location == expected_location and object_id not in required
    )
    low_confidence = sorted(
        object_id for object_id in required if state.entity_confidence.get(object_id, 0.0) < confidence_threshold
    )
    missing_evidence = _missing_entity_evidence(state, required)
    claim = (
        f"kit in {tray_id}: unobserved={unobserved}; misplaced={misplaced}; extras={extras}; "
        f"low_confidence={low_confidence}; missing_evidence={missing_evidence}"
    )
    if unobserved:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.TARGET_NOT_OBSERVED, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(unobserved)
    elif low_confidence:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = set(low_confidence)
    elif missing_evidence:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.EVIDENCE_MISSING, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(missing_evidence)
    elif misplaced or extras:
        outcome = (VerificationStatus.REFUTED, ReasonCode.GOAL_NOT_SATISFIED, RecoveryHint.RETRY_ACTION)
        supporting_entity_ids = set(misplaced) | set(extras)
    else:
        outcome = (VerificationStatus.CONFIRMED, ReasonCode.GOAL_SATISFIED, RecoveryHint.NONE)
        supporting_entity_ids = required
    return _result(
        state,
        task_id,
        claim,
        *outcome,
        KIT_CONTENTS_RULE_VERSION,
        supporting_entity_ids,
        verifier_kind="kit_contents",
        request_semantics={
            "required_object_ids": sorted(required),
            "tray_id": tray_id,
            "confidence_threshold": float(confidence_threshold),
        },
        context=context,
    )


def verify_inspection_evidence(
    state: WorldState,
    task_id: str,
    required_entity_ids: list[str],
    confidence_threshold: float = 0.8,
    *,
    context: VerificationContext | None = None,
) -> VerificationResult:
    required = _required_entity_set(required_entity_ids, "inspection")
    _validate_confidence_threshold(confidence_threshold)
    unobserved = sorted(entity_id for entity_id in required if entity_id not in state.entity_locations)
    low_confidence = sorted(
        entity_id for entity_id in required if state.entity_confidence.get(entity_id, 0.0) < confidence_threshold
    )
    missing_evidence = _missing_entity_evidence(state, required)
    claim = f"inspection: unobserved={unobserved}; low_confidence={low_confidence}; missing_evidence={missing_evidence}"
    if unobserved:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.TARGET_NOT_OBSERVED, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(unobserved)
    elif low_confidence:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = set(low_confidence)
    elif missing_evidence:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.EVIDENCE_MISSING, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(missing_evidence)
    else:
        outcome = (VerificationStatus.CONFIRMED, ReasonCode.GOAL_SATISFIED, RecoveryHint.NONE)
        supporting_entity_ids = required
    return _result(
        state,
        task_id,
        claim,
        *outcome,
        INSPECTION_EVIDENCE_RULE_VERSION,
        supporting_entity_ids,
        verifier_kind="inspection_evidence",
        request_semantics={
            "required_entity_ids": sorted(required),
            "confidence_threshold": float(confidence_threshold),
        },
        context=context,
    )


def verify_workspace_clearance(
    state: WorldState,
    task_id: str,
    *,
    context: VerificationContext | None = None,
) -> VerificationResult:
    expected = {"blue_cylinder": "in:staging_bin", "red_block": "in:tray"}
    unobserved = sorted(entity_id for entity_id in expected if entity_id not in state.entity_locations)
    unmet_entity_ids = sorted(
        entity_id
        for entity_id, location in expected.items()
        if entity_id in state.entity_locations and state.entity_locations[entity_id] != location
    )
    unmet = [f"{entity_id}->{expected[entity_id]}" for entity_id in unmet_entity_ids]
    missing_evidence = _missing_entity_evidence(state, set(expected))
    claim = f"workspace clearance: unobserved={unobserved}; unmet={unmet}; missing_evidence={missing_evidence}"
    if unobserved:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.TARGET_NOT_OBSERVED, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(unobserved)
    elif missing_evidence:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.EVIDENCE_MISSING, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(missing_evidence)
    elif unmet_entity_ids:
        outcome = (VerificationStatus.REFUTED, ReasonCode.GOAL_NOT_SATISFIED, RecoveryHint.RETRY_ACTION)
        supporting_entity_ids = set(unmet_entity_ids)
    else:
        outcome = (VerificationStatus.CONFIRMED, ReasonCode.GOAL_SATISFIED, RecoveryHint.NONE)
        supporting_entity_ids = set(expected)
    return _result(
        state,
        task_id,
        claim,
        *outcome,
        WORKSPACE_CLEARANCE_RULE_VERSION,
        supporting_entity_ids,
        verifier_kind="workspace_clearance",
        request_semantics={},
        context=context,
    )


def verify_parcel_sorting(
    state: WorldState,
    task_id: str,
    parcel_routes: dict[str, str] | None = None,
    expected_attributes: dict[str, dict[str, str]] | None = None,
    confidence_threshold: float = 0.8,
    *,
    context: VerificationContext | None = None,
) -> VerificationResult:
    route_input = DEFAULT_PARCEL_ROUTES if parcel_routes is None else parcel_routes
    if not isinstance(route_input, Mapping):
        raise ValueError("parcel sorting routes must be a mapping")
    routes = dict(route_input)
    required = _required_entity_set(list(routes), "parcel sorting")
    if any(not isinstance(destination, str) or not destination.strip() for destination in routes.values()):
        raise ValueError("parcel sorting requires non-empty destinations")
    attributes = DEFAULT_PARCEL_ATTRIBUTES if expected_attributes is None else expected_attributes
    if not isinstance(attributes, Mapping):
        raise ValueError("parcel attribute requirements must be a mapping")
    if set(attributes) != required:
        raise ValueError("parcel attribute requirements must match routed parcels")
    if any(
        not isinstance(requirements, Mapping)
        or not requirements
        or any(
            not isinstance(key, str) or not key.strip() or not isinstance(value, str) or not value.strip()
            for key, value in requirements.items()
        )
        for requirements in attributes.values()
    ):
        raise ValueError("each parcel requires non-empty string attribute requirements")
    normalized_attributes: dict[str, dict[str, str]] = {}
    for parcel_id in sorted(required):
        try:
            normalized_attributes[parcel_id] = validate_observed_attributes(
                dict(attributes[parcel_id]),
                entity_type="parcel",
            )
        except ValueError as error:
            raise ValueError(f"invalid parcel attribute requirements for {parcel_id!r}: {error}") from error
    routes = {parcel_id: routes[parcel_id] for parcel_id in sorted(routes)}
    attributes = normalized_attributes
    _validate_confidence_threshold(confidence_threshold)

    unobserved = sorted(parcel_id for parcel_id in required if parcel_id not in state.entity_locations)
    low_confidence = sorted(
        parcel_id
        for parcel_id in required
        if not math.isfinite(state.entity_confidence.get(parcel_id, 0.0))
        or state.entity_confidence.get(parcel_id, 0.0) < confidence_threshold
    )
    missing_evidence = _missing_entity_evidence(state, required)
    missing_attributes: list[str] = []
    attribute_mismatches: list[str] = []
    expired_attributes: list[str] = []
    low_confidence_attributes: list[str] = []
    insufficient_attributes: list[str] = []
    missing_attribute_entities: set[str] = set()
    attribute_mismatch_entities: set[str] = set()
    attribute_quality_requirements = {parcel_id: set(attributes[parcel_id]) for parcel_id in sorted(required)}
    (
        missing_attribute_metadata,
        expired_attributes,
        low_confidence_attributes,
        insufficient_attributes,
        attribute_evidence_keys,
    ) = _attribute_quality_summary(
        state,
        attribute_quality_requirements,
        confidence_threshold,
    )
    _append_unique(missing_attributes, missing_attribute_metadata)
    missing_attribute_entities.update(item.partition(".")[0] for item in missing_attribute_metadata)
    expired_attribute_entities = {item.partition(".")[0] for item in expired_attributes}
    low_confidence_attribute_entities = {item.partition(".")[0] for item in low_confidence_attributes}
    insufficient_attribute_entities = {item.partition(".")[0] for item in insufficient_attributes}
    for parcel_id in sorted(required):
        observed = state.entity_attributes.get(parcel_id, {})
        for key, expected_value in attributes[parcel_id].items():
            if key not in observed:
                _append_unique(missing_attributes, [f"{parcel_id}.{key}"])
                missing_attribute_entities.add(parcel_id)
            elif observed[key] != expected_value:
                attribute_mismatches.append(f"{parcel_id}.{key}={observed[key]}")
                attribute_mismatch_entities.add(parcel_id)
    misrouted_entity_ids = sorted(
        parcel_id
        for parcel_id, destination in routes.items()
        if parcel_id in state.entity_locations and state.entity_locations[parcel_id] != f"in:{destination}"
    )
    misrouted = [f"{parcel_id}->{routes[parcel_id]}" for parcel_id in misrouted_entity_ids]
    managed_locations = {f"in:{destination}" for destination in routes.values()}
    extras = sorted(
        entity_id
        for entity_id, location in state.entity_locations.items()
        if location in managed_locations and entity_id not in required
    )
    claim = (
        f"parcel sorting: unobserved={unobserved}; low_confidence={low_confidence}; "
        f"missing_evidence={missing_evidence}; missing_attributes={missing_attributes}; "
        f"expired_attributes={expired_attributes}; low_confidence_attributes={low_confidence_attributes}; "
        f"insufficient_attributes={insufficient_attributes}; attribute_mismatches={attribute_mismatches}; "
        f"misrouted={misrouted}; extras={extras}"
    )
    if unobserved:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.TARGET_NOT_OBSERVED, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(unobserved)
    elif low_confidence:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = set(low_confidence)
    elif low_confidence_attributes:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = low_confidence_attribute_entities
    elif expired_attributes:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.STALE_OBSERVATION,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = expired_attribute_entities
    elif missing_evidence or missing_attributes or insufficient_attributes:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.EVIDENCE_MISSING, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(missing_evidence) | missing_attribute_entities | insufficient_attribute_entities
    elif attribute_mismatches or misrouted or extras:
        outcome = (VerificationStatus.REFUTED, ReasonCode.GOAL_NOT_SATISFIED, RecoveryHint.RETRY_ACTION)
        supporting_entity_ids = attribute_mismatch_entities | set(misrouted_entity_ids) | set(extras)
    else:
        outcome = (VerificationStatus.CONFIRMED, ReasonCode.GOAL_SATISFIED, RecoveryHint.NONE)
        supporting_entity_ids = required
    return _result(
        state,
        task_id,
        claim,
        *outcome,
        PARCEL_SORTING_RULE_VERSION,
        supporting_entity_ids,
        verifier_kind="parcel_sorting",
        request_semantics={
            "parcel_routes": routes,
            "expected_attributes": attributes,
            "confidence_threshold": float(confidence_threshold),
        },
        context=context,
        supporting_attribute_keys=attribute_evidence_keys,
    )


def verify_parcel_policy(
    state: WorldState,
    task_id: str,
    parcel_ids: list[str],
    pickup_shelf_id: str = "pickup_shelf",
    quarantine_bin_id: str = "quarantine_bin",
    confidence_threshold: float = 0.8,
    parcel_manifest: Mapping[str, Mapping[str, str]] | None = None,
    manifest_id: str | None = None,
    *,
    context: VerificationContext | None = None,
) -> VerificationResult:
    """Verify routes derived from observed parcel attributes, never caller claims."""
    required = _required_entity_set(parcel_ids, "parcel policy")
    if not isinstance(pickup_shelf_id, str) or not pickup_shelf_id.strip():
        raise ValueError("pickup_shelf_id must be non-empty")
    if not isinstance(quarantine_bin_id, str) or not quarantine_bin_id.strip():
        raise ValueError("quarantine_bin_id must be non-empty")
    if pickup_shelf_id == quarantine_bin_id:
        raise ValueError("pickup and quarantine destinations must be different")
    _validate_confidence_threshold(confidence_threshold)
    normalized_manifest_id, expected_identities = _validate_parcel_manifest(required, parcel_manifest, manifest_id)

    attribute_quality_requirements: dict[str, set[str]] = {
        parcel_id: {"label_status", "condition"} for parcel_id in sorted(required)
    }
    if normalized_manifest_id is not None:
        for parcel_id in sorted(required):
            observed_attributes = state.entity_attributes.get(parcel_id, {})
            if not isinstance(observed_attributes, Mapping):
                continue
            expected_keys = set(expected_identities[parcel_id])
            matching_keys = expected_keys & set(observed_attributes)
            if not matching_keys:
                matching_keys = set(PARCEL_IDENTITY_KEYS) & set(observed_attributes)
            attribute_quality_requirements[parcel_id].update(matching_keys)
    (
        missing_attribute_metadata,
        expired_attributes,
        low_confidence_attributes,
        insufficient_attributes,
        attribute_evidence_keys,
    ) = _attribute_quality_summary(
        state,
        attribute_quality_requirements,
        confidence_threshold,
    )

    unobserved = sorted(parcel_id for parcel_id in required if parcel_id not in state.entity_locations)
    low_confidence = sorted(
        parcel_id
        for parcel_id in required
        if not math.isfinite(state.entity_confidence.get(parcel_id, 0.0))
        or state.entity_confidence.get(parcel_id, 0.0) < confidence_threshold
    )
    missing_evidence = _missing_entity_evidence(state, required)
    missing_attributes: list[str] = []
    missing_manifest_identities: list[str] = []
    manifest_mismatches: list[str] = []
    duplicate_identities: list[str] = []
    _append_unique(missing_attributes, missing_attribute_metadata)
    missing_attribute_entities: set[str] = set()
    duplicate_identity_entities: set[str] = set()
    missing_attribute_entities.update(item.partition(".")[0] for item in missing_attribute_metadata)
    expired_attribute_entities = {item.partition(".")[0] for item in expired_attributes}
    low_confidence_attribute_entities = {item.partition(".")[0] for item in low_confidence_attributes}
    insufficient_attribute_entities = {item.partition(".")[0] for item in insufficient_attributes}
    seen_identities: dict[str, tuple[str, str]] = {}
    decisions: dict[str, str] = {}
    decision_reasons: dict[str, str] = {}
    for parcel_id in sorted(required):
        observed = state.entity_attributes.get(parcel_id, {})
        if not isinstance(observed, Mapping):
            _append_unique(
                missing_attributes,
                [f"{parcel_id}.label_status", f"{parcel_id}.condition"],
            )
            missing_attribute_entities.add(parcel_id)
            if normalized_manifest_id is not None:
                missing_manifest_identities.append(parcel_id)
            continue
        label_status = observed.get("label_status")
        condition = observed.get("condition")
        if not isinstance(label_status, str) or not label_status.strip():
            _append_unique(missing_attributes, [f"{parcel_id}.label_status"])
            missing_attribute_entities.add(parcel_id)
        if not isinstance(condition, str) or not condition.strip():
            _append_unique(missing_attributes, [f"{parcel_id}.condition"])
            missing_attribute_entities.add(parcel_id)
        observed_identities: dict[str, str] = {}
        for identity_key in PARCEL_IDENTITY_KEYS:
            identity_value = observed.get(identity_key)
            if identity_value is None:
                continue
            try:
                identity = _normalize_parcel_identity(identity_value, identity_key)
            except ValueError:
                _append_unique(missing_attributes, [f"{parcel_id}.{identity_key}"])
                missing_attribute_entities.add(parcel_id)
                continue
            observed_identities[identity_key] = identity
            previous = seen_identities.get(identity)
            if previous is not None and previous[0] != parcel_id:
                duplicate_identities.append(
                    f"{previous[0]}.{previous[1]}={parcel_id}.{identity_key} ({str(identity_value).strip()})"
                )
                duplicate_identity_entities.update({previous[0], parcel_id})
            else:
                seen_identities[identity] = (parcel_id, identity_key)
        if normalized_manifest_id is not None:
            if not observed_identities:
                missing_manifest_identities.append(parcel_id)
            elif any(
                key in expected_identities[parcel_id] and expected_identities[parcel_id][key] != identity
                for key, identity in observed_identities.items()
            ) or set(expected_identities[parcel_id].values()).isdisjoint(observed_identities.values()):
                manifest_mismatches.append(parcel_id)
        if (
            not isinstance(label_status, str)
            or not label_status.strip()
            or not isinstance(condition, str)
            or not condition.strip()
        ):
            continue
        label_status = label_status.strip().lower()
        condition = condition.strip().lower()
        if label_status == "verified" and condition == "intact":
            decisions[parcel_id] = f"in:{pickup_shelf_id}"
            decision_reasons[parcel_id] = "verified_intact"
        else:
            decisions[parcel_id] = f"in:{quarantine_bin_id}"
            reasons = []
            if label_status != "verified":
                reasons.append(f"label_{label_status}")
            if condition != "intact":
                reasons.append(f"condition_{condition}")
            decision_reasons[parcel_id] = "+".join(reasons)
    misrouted_entity_ids = sorted(
        parcel_id
        for parcel_id, expected_location in decisions.items()
        if state.entity_locations.get(parcel_id) != expected_location
    )
    misrouted = [f"{parcel_id}->{decisions[parcel_id]}" for parcel_id in misrouted_entity_ids]
    managed_locations = {f"in:{pickup_shelf_id}", f"in:{quarantine_bin_id}"}
    extras = sorted(
        entity_id
        for entity_id, location in state.entity_locations.items()
        if location in managed_locations and entity_id not in required
    )
    claim = (
        f"parcel policy: manifest_id={normalized_manifest_id}; decisions={decisions}; "
        f"reasons={decision_reasons}; unobserved={unobserved}; "
        f"low_confidence={low_confidence}; missing_evidence={missing_evidence}; "
        f"missing_attributes={missing_attributes}; missing_manifest_identities={missing_manifest_identities}; "
        f"expired_attributes={expired_attributes}; low_confidence_attributes={low_confidence_attributes}; "
        f"insufficient_attributes={insufficient_attributes}; manifest_mismatches={manifest_mismatches}; "
        f"duplicate_identities={duplicate_identities}; "
        f"misrouted={misrouted}; extras={extras}"
    )
    if unobserved:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.TARGET_NOT_OBSERVED, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = set(unobserved)
    elif low_confidence:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = set(low_confidence)
    elif low_confidence_attributes:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.CONFIDENCE_BELOW_THRESHOLD,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = low_confidence_attribute_entities
    elif expired_attributes:
        outcome = (
            VerificationStatus.INSUFFICIENT_EVIDENCE,
            ReasonCode.STALE_OBSERVATION,
            RecoveryHint.RE_OBSERVE,
        )
        supporting_entity_ids = expired_attribute_entities
    elif missing_evidence or missing_attributes or missing_manifest_identities or insufficient_attributes:
        outcome = (VerificationStatus.INSUFFICIENT_EVIDENCE, ReasonCode.EVIDENCE_MISSING, RecoveryHint.RE_OBSERVE)
        supporting_entity_ids = (
            set(missing_evidence)
            | missing_attribute_entities
            | set(missing_manifest_identities)
            | insufficient_attribute_entities
        )
    elif manifest_mismatches or duplicate_identities or misrouted or extras:
        outcome = (VerificationStatus.REFUTED, ReasonCode.GOAL_NOT_SATISFIED, RecoveryHint.RETRY_ACTION)
        supporting_entity_ids = (
            set(manifest_mismatches) | duplicate_identity_entities | set(misrouted_entity_ids) | set(extras)
        )
    else:
        outcome = (VerificationStatus.CONFIRMED, ReasonCode.GOAL_SATISFIED, RecoveryHint.NONE)
        supporting_entity_ids = required
    return _result(
        state,
        task_id,
        claim,
        *outcome,
        PARCEL_POLICY_RULE_VERSION,
        supporting_entity_ids,
        verifier_kind="parcel_policy",
        request_semantics={
            "parcel_ids": sorted(required),
            "pickup_shelf_id": pickup_shelf_id,
            "quarantine_bin_id": quarantine_bin_id,
            "confidence_threshold": float(confidence_threshold),
            "parcel_manifest": (None if parcel_manifest is None else expected_identities),
            "manifest_id": normalized_manifest_id,
        },
        context=context,
        supporting_attribute_keys=attribute_evidence_keys,
    )
