from __future__ import annotations

import hashlib
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from workbench_contracts import (
    ClockId,
    Pose,
    WorldBelief,
    WorldEntity,
    WorldEvent,
    WorldEventType,
    WorldRelation,
    WorldRelationPredicate,
)
from workbench_contracts import WorldState as ContractWorldState

from .aging import (
    ObservationAgingBoundary,
    ObservationFreshnessPolicy,
    age_world_state,
    comparable_wall_observation_is_older,
)
from .event_payloads import WorldEventPayloadValidationError, normalize_world_event


class WorldState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    entity_types: dict[str, str] = Field(default_factory=dict)
    entity_poses: dict[str, Any] = Field(default_factory=dict)
    entity_last_observed_at: dict[str, str] = Field(default_factory=dict)
    entity_observation_clock_ids: dict[str, str] = Field(default_factory=dict)
    entity_observation_sources: dict[str, str] = Field(default_factory=dict)
    entity_beliefs: dict[str, WorldBelief] = Field(default_factory=dict)
    entity_locations: dict[str, str] = Field(default_factory=dict)
    entity_location_evidence_refs: dict[str, list[str]] = Field(default_factory=dict)
    entity_location_last_observed_at: dict[str, str] = Field(default_factory=dict)
    entity_location_clock_ids: dict[str, str] = Field(default_factory=dict)
    entity_location_sources: dict[str, str] = Field(default_factory=dict)
    entity_location_beliefs: dict[str, WorldBelief] = Field(default_factory=dict)
    entity_confidence: dict[str, float] = Field(default_factory=dict)
    entity_attributes: dict[str, dict[str, str]] = Field(default_factory=dict)
    entity_attribute_baselines: dict[str, bool] = Field(default_factory=dict)
    entity_evidence_refs: dict[str, list[str]] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    freshness_evaluated: bool = Field(default=True, exclude=True, repr=False)
    applied_event_ids: list[str] = Field(default_factory=list)


def _append_entity_evidence(state: WorldState, entity_id: str, evidence_refs: list[str]) -> None:
    entity_evidence = state.entity_evidence_refs.setdefault(entity_id, [])
    entity_evidence.extend(reference for reference in evidence_refs if reference not in entity_evidence)


def _update_location(state: WorldState, entity_id: str, location: str, evidence_refs: list[str]) -> None:
    if state.entity_locations.get(entity_id) != location:
        state.entity_evidence_refs[entity_id] = list(dict.fromkeys(evidence_refs))
        state.entity_location_evidence_refs[entity_id] = list(dict.fromkeys(evidence_refs))
    else:
        _append_entity_evidence(state, entity_id, evidence_refs)
        location_evidence = state.entity_location_evidence_refs.setdefault(entity_id, [])
        location_evidence.extend(reference for reference in evidence_refs if reference not in location_evidence)
    state.entity_locations[entity_id] = location


def _replace_observation_metadata(
    values: dict[str, str],
    entity_id: str,
    payload: dict[str, Any],
    field_name: str,
) -> None:
    if field_name not in payload:
        return
    value = payload[field_name]
    if field_name == "clock_id" and isinstance(value, ClockId):
        value = value.value
    if type(value) is str and value.strip():
        values[entity_id] = value


def _observation_is_provably_older(state: WorldState, entity_id: str, payload: dict[str, Any]) -> bool:
    if entity_id not in state.entity_last_observed_at:
        return False
    return comparable_wall_observation_is_older(
        current_observed_at=state.entity_last_observed_at.get(entity_id),
        current_clock_id=state.entity_observation_clock_ids.get(entity_id),
        incoming_observed_at=payload.get("observed_at"),
        incoming_clock_id=payload.get("clock_id"),
    )


def _has_attribute_baseline(state: WorldState, entity_id: str) -> bool:
    return state.entity_attribute_baselines.get(entity_id, entity_id in state.entity_attributes)


def _apply_observed_attributes(state: WorldState, entity_id: str, payload: dict[str, Any]) -> None:
    if "attributes" not in payload:
        return
    attributes = dict(payload["attributes"])
    mode = payload.get("attributes_mode", "complete")
    if mode == "partial":
        if not _has_attribute_baseline(state, entity_id):
            raise WorldEventPayloadValidationError(
                f"partial attributes for entity_id {entity_id!r} require a prior complete attributes baseline"
            )
        merged = dict(state.entity_attributes.get(entity_id, {}))
        merged.update(attributes)
        state.entity_attributes[entity_id] = merged
    elif mode == "complete":
        state.entity_attributes[entity_id] = attributes
    else:
        raise WorldEventPayloadValidationError("attributes_mode must be 'complete' or 'partial'")
    state.entity_attribute_baselines[entity_id] = True


def _is_modern_observation(payload: dict[str, Any]) -> bool:
    return "entity_type" in payload


def apply_event(state: WorldState, event: WorldEvent) -> WorldState:
    """Apply one ordered event. Re-applying an event is idempotent."""
    event = normalize_world_event(event)
    if event.event_id in state.applied_event_ids:
        return state

    if event.event_type is WorldEventType.OBSERVATION:
        entity_id = event.payload["entity_id"]
        incoming_entity_type = event.payload.get("entity_type")
        existing_entity_type = state.entity_types.get(entity_id)
        if (
            incoming_entity_type is not None
            and existing_entity_type is not None
            and existing_entity_type != "legacy"
            and existing_entity_type != incoming_entity_type
        ):
            raise ValueError(
                f"entity_id {entity_id!r} has conflicting entity_type values "
                f"{existing_entity_type!r} and {incoming_entity_type!r}"
            )
        if event.payload.get("attributes_mode") == "partial" and not _has_attribute_baseline(state, entity_id):
            raise WorldEventPayloadValidationError(
                f"partial attributes for entity_id {entity_id!r} require a prior complete attributes baseline"
            )

    next_state = state.model_copy(deep=True)
    next_state.applied_event_ids.append(event.event_id)
    next_state.evidence_refs.extend(event.evidence_refs)

    if event.event_type is WorldEventType.OBSERVATION:
        entity_id = event.payload["entity_id"]
        if _is_modern_observation(event.payload):
            next_state.freshness_evaluated = False
        if _observation_is_provably_older(state, entity_id, event.payload):
            return next_state

        incoming_entity_type = event.payload.get("entity_type")
        if incoming_entity_type is not None:
            next_state.entity_types[entity_id] = incoming_entity_type
        else:
            next_state.entity_types.setdefault(entity_id, "legacy")
        if "pose" in event.payload:
            next_state.entity_poses[entity_id] = event.payload["pose"]
        _replace_observation_metadata(next_state.entity_last_observed_at, entity_id, event.payload, "observed_at")
        _replace_observation_metadata(
            next_state.entity_observation_clock_ids,
            entity_id,
            event.payload,
            "clock_id",
        )
        _replace_observation_metadata(
            next_state.entity_observation_sources,
            entity_id,
            event.payload,
            "source",
        )
        next_state.entity_beliefs[entity_id] = WorldBelief.OBSERVED
        location = event.payload.get("location")
        if location is not None:
            _update_location(next_state, entity_id, location, event.evidence_refs)
            for values, field_name in (
                (next_state.entity_location_last_observed_at, "observed_at"),
                (next_state.entity_location_clock_ids, "clock_id"),
                (next_state.entity_location_sources, "source"),
            ):
                _replace_observation_metadata(values, entity_id, event.payload, field_name)
            next_state.entity_location_beliefs[entity_id] = WorldBelief.OBSERVED
        next_state.entity_confidence[entity_id] = event.payload["confidence"]
        _apply_observed_attributes(next_state, entity_id, event.payload)
        if location is None:
            _append_entity_evidence(next_state, entity_id, event.evidence_refs)

    return next_state


def _canonical_event_content(event: WorldEvent) -> str:
    try:
        return json.dumps(
            event.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("event content must contain finite UTF-8 JSON values") from error


def _validated_event_stream(run_id: str, events: list[WorldEvent]) -> list[WorldEvent]:
    if not run_id:
        raise ValueError("run_id must be non-empty")

    content_by_event_id: dict[str, str] = {}
    event_id_by_sequence: dict[int, str] = {}
    entity_type_by_entity_id: dict[str, str] = {}
    unique_events: list[WorldEvent] = []

    for candidate in events:
        if not isinstance(candidate, WorldEvent):
            raise TypeError("events must contain WorldEvent values")
        if candidate.run_id != run_id:
            raise ValueError(f"event run_id {candidate.run_id!r} does not match requested run_id {run_id!r}")
        event = normalize_world_event(candidate)

        event_content = _canonical_event_content(event)
        previous_content = content_by_event_id.get(event.event_id)
        if previous_content is not None:
            if previous_content != event_content:
                raise ValueError(f"event_id {event.event_id!r} is duplicated with different complete content")
            continue

        sequence_owner = event_id_by_sequence.get(event.sequence_no)
        if sequence_owner is not None:
            raise ValueError(
                f"sequence_no {event.sequence_no} is shared by event_id {sequence_owner!r} and {event.event_id!r}"
            )

        if event.event_type is WorldEventType.OBSERVATION and "entity_type" in event.payload:
            entity_id = event.payload["entity_id"]
            entity_type = event.payload["entity_type"]
            previous_entity_type = entity_type_by_entity_id.get(entity_id)
            if previous_entity_type is not None and previous_entity_type != entity_type:
                raise ValueError(
                    f"entity_id {entity_id!r} has conflicting entity_type values "
                    f"{previous_entity_type!r} and {entity_type!r}"
                )
            entity_type_by_entity_id[entity_id] = entity_type

        content_by_event_id[event.event_id] = event_content
        event_id_by_sequence[event.sequence_no] = event.event_id
        unique_events.append(event)

    ordered_events = sorted(unique_events, key=lambda item: item.sequence_no)
    complete_baselines: set[str] = set()
    for event in ordered_events:
        if event.event_type is not WorldEventType.OBSERVATION or "attributes" not in event.payload:
            continue
        entity_id = event.payload["entity_id"]
        if event.payload.get("attributes_mode", "complete") == "partial" and entity_id not in complete_baselines:
            raise WorldEventPayloadValidationError(
                f"partial attributes for entity_id {entity_id!r} require a prior complete attributes baseline"
            )
        complete_baselines.add(entity_id)
    return ordered_events


def _apply_aging_boundary(
    state: WorldState,
    freshness_policy: ObservationFreshnessPolicy | None,
    aging_boundary: ObservationAgingBoundary | None,
) -> WorldState:
    if (freshness_policy is None) != (aging_boundary is None):
        raise ValueError("freshness_policy and aging_boundary must be supplied together")
    if freshness_policy is None or aging_boundary is None:
        return state
    return age_world_state(
        state,
        freshness_policy=freshness_policy,
        aging_boundary=aging_boundary,
    )


def reduce_events(
    run_id: str,
    events: list[WorldEvent],
    *,
    freshness_policy: ObservationFreshnessPolicy | None = None,
    aging_boundary: ObservationAgingBoundary | None = None,
) -> WorldState:
    """Preflight the stream, fold exact duplicates, then replay by sequence."""
    ordered_events = _validated_event_stream(run_id, events)
    state = WorldState(run_id=run_id)
    for event in ordered_events:
        state = apply_event(state, event)
    return _apply_aging_boundary(state, freshness_policy, aging_boundary)


def _relation_from_location(
    entity_id: str,
    location: str,
    evidence_refs: list[str],
    belief: WorldBelief = WorldBelief.OBSERVED,
) -> WorldRelation:
    location_kind, separator, object_id = location.partition(":")
    if not separator or not object_id.strip():
        raise ValueError(f"location {location!r} for entity_id {entity_id!r} is not canonical")
    if location_kind == "in":
        predicate = WorldRelationPredicate.INSIDE
    elif location_kind == "on":
        predicate = WorldRelationPredicate.ON_TOP_OF
    else:
        raise ValueError(f"location {location!r} for entity_id {entity_id!r} cannot be represented")
    return WorldRelation(
        subject_id=entity_id,
        predicate=predicate,
        object_id=object_id,
        belief=belief,
        evidence_refs=list(evidence_refs),
    )


def canonical_world_state_bytes(snapshot: ContractWorldState) -> bytes:
    """Return canonical semantic hash material without snapshot metadata."""
    if not isinstance(snapshot, ContractWorldState):
        raise TypeError("snapshot must be a contract WorldState")
    entities = sorted(snapshot.entities, key=lambda entity: entity.entity_id)
    relations = sorted(
        snapshot.relations,
        key=lambda relation: (
            relation.subject_id,
            relation.predicate.value,
            relation.object_id,
        ),
    )
    material = {
        "run_id": snapshot.run_id,
        "sequence_no": snapshot.sequence_no,
        "entities": [entity.model_dump(mode="json") for entity in entities],
        "relations": [relation.model_dump(mode="json") for relation in relations],
    }
    try:
        return json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, UnicodeError, ValueError) as error:
        raise ValueError("snapshot hash material must contain only finite UTF-8 JSON values") from error


def create_world_state_snapshot(
    run_id: str,
    events: list[WorldEvent],
    *,
    freshness_policy: ObservationFreshnessPolicy | None = None,
    aging_boundary: ObservationAgingBoundary | None = None,
) -> ContractWorldState:
    """Project a validated ordered stream into the public WorldState contract."""
    ordered_events = _validated_event_stream(run_id, events)
    if not ordered_events:
        raise ValueError("cannot create a WorldState snapshot from an empty event stream")

    reduced_at = ordered_events[-1].occurred_at
    if not reduced_at.strip():
        raise ValueError("snapshot reduced_at boundary must be a non-empty occurred_at value")

    internal_state = WorldState(run_id=run_id)
    for event in ordered_events:
        internal_state = apply_event(internal_state, event)
    internal_state = _apply_aging_boundary(internal_state, freshness_policy, aging_boundary)

    entities: list[WorldEntity] = []
    for entity_id in sorted(internal_state.entity_types):
        values: dict[str, Any] = {
            "entity_id": entity_id,
            "entity_type": internal_state.entity_types[entity_id],
            "belief": internal_state.entity_beliefs.get(entity_id, WorldBelief.OBSERVED),
            "evidence_refs": list(internal_state.entity_evidence_refs.get(entity_id, [])),
        }
        if entity_id in internal_state.entity_poses:
            values["pose"] = Pose.model_validate(internal_state.entity_poses[entity_id])
        if entity_id in internal_state.entity_confidence:
            values["confidence"] = internal_state.entity_confidence[entity_id]
        if entity_id in internal_state.entity_last_observed_at:
            values["last_observed_at"] = internal_state.entity_last_observed_at[entity_id]
        if entity_id in internal_state.entity_attributes:
            values["attributes"] = dict(internal_state.entity_attributes[entity_id])
        entities.append(WorldEntity.model_validate(values))

    relations = sorted(
        (
            _relation_from_location(
                entity_id,
                location,
                internal_state.entity_location_evidence_refs.get(entity_id, []),
                internal_state.entity_location_beliefs.get(entity_id, WorldBelief.OBSERVED),
            )
            for entity_id, location in internal_state.entity_locations.items()
        ),
        key=lambda relation: (
            relation.subject_id,
            relation.predicate.value,
            relation.object_id,
        ),
    )
    snapshot_values: dict[str, Any] = {
        "run_id": run_id,
        "sequence_no": ordered_events[-1].sequence_no,
        "state_hash": "0" * 64,
        "entities": entities,
        "relations": relations,
        "reduced_at": reduced_at,
    }
    if aging_boundary is not None:
        snapshot_values["reduced_at"] = aging_boundary.as_of
        snapshot_values["clock_id"] = aging_boundary.clock_id
    snapshot = ContractWorldState.model_validate(snapshot_values)
    state_hash = hashlib.sha256(canonical_world_state_bytes(snapshot)).hexdigest()
    return snapshot.model_copy(update={"state_hash": state_hash})


__all__ = [
    "WorldState",
    "apply_event",
    "canonical_world_state_bytes",
    "create_world_state_snapshot",
    "reduce_events",
]
