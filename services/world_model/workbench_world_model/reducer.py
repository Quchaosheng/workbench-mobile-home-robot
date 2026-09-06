import hashlib
import json
from typing import Any

from pydantic import BaseModel, Field
from workbench_contracts import (
    Pose,
    WorldEvent,
    WorldEventType,
    WorldStateBelief,
    WorldStateEntity,
    WorldStateRelation,
    WorldStateRelationPredicate,
)
from workbench_contracts import WorldState as ContractWorldState

from .event_payloads import normalize_world_event


class WorldState(BaseModel):
    run_id: str
    entity_types: dict[str, str] = Field(default_factory=dict)
    entity_poses: dict[str, Any] = Field(default_factory=dict)
    entity_last_observed_at: dict[str, str | None] = Field(default_factory=dict)
    entity_locations: dict[str, str] = Field(default_factory=dict)
    entity_location_evidence_refs: dict[str, list[str]] = Field(default_factory=dict)
    entity_confidence: dict[str, float] = Field(default_factory=dict)
    entity_attributes: dict[str, dict[str, str]] = Field(default_factory=dict)
    entity_evidence_refs: dict[str, list[str]] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
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


def apply_event(state: WorldState, event: WorldEvent) -> WorldState:
    """Apply one ordered event. Re-applying an event is idempotent."""
    event = normalize_world_event(event)
    if event.event_id in state.applied_event_ids:
        return state

    if event.event_type is WorldEventType.OBSERVATION:
        entity_id = event.payload["entity_id"]
        entity_type = event.payload["entity_type"]
        existing_entity_type = state.entity_types.get(entity_id)
        if existing_entity_type is not None and existing_entity_type != entity_type:
            raise ValueError(
                f"entity_id {entity_id!r} has conflicting entity_type values "
                f"{existing_entity_type!r} and {entity_type!r}"
            )

    next_state = state.model_copy(deep=True)
    next_state.applied_event_ids.append(event.event_id)
    next_state.evidence_refs.extend(event.evidence_refs)

    if event.event_type is WorldEventType.OBSERVATION:
        entity_id = event.payload["entity_id"]
        next_state.entity_types[entity_id] = event.payload["entity_type"]
        if "pose" in event.payload:
            next_state.entity_poses[entity_id] = event.payload["pose"]
        if "observed_at" in event.payload:
            next_state.entity_last_observed_at[entity_id] = event.payload["observed_at"]
        location = event.payload.get("location")
        if location is not None:
            _update_location(next_state, entity_id, location, event.evidence_refs)
        next_state.entity_confidence[entity_id] = event.payload["confidence"]
        attributes = event.payload.get("attributes")
        if attributes is not None:
            next_state.entity_attributes[entity_id] = attributes
        if location is None:
            _append_entity_evidence(next_state, entity_id, event.evidence_refs)

    return next_state


def _canonical_event_content(event: WorldEvent) -> str:
    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _validated_event_stream(run_id: str, events: list[WorldEvent]) -> list[WorldEvent]:
    if not run_id:
        raise ValueError("run_id must be non-empty")

    content_by_event_id: dict[str, str] = {}
    event_id_by_sequence: dict[int, str] = {}
    entity_type_by_entity_id: dict[str, str] = {}
    unique_events: list[WorldEvent] = []

    for event in events:
        if event.run_id != run_id:
            raise ValueError(f"event run_id {event.run_id!r} does not match requested run_id {run_id!r}")
        event = normalize_world_event(event)

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

        if event.event_type is WorldEventType.OBSERVATION:
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

    return sorted(unique_events, key=lambda item: item.sequence_no)


def reduce_events(run_id: str, events: list[WorldEvent]) -> WorldState:
    """Preflight the full stream, fold exact duplicates, then replay by ascending sequence_no."""
    ordered_events = _validated_event_stream(run_id, events)
    state = WorldState(run_id=run_id)
    for event in ordered_events:
        state = apply_event(state, event)
    return state


def _relation_from_location(
    entity_id: str,
    location: str,
    evidence_refs: list[str],
) -> WorldStateRelation:
    location_kind, separator, object_id = location.partition(":")
    if not separator or not object_id or any(character.isspace() for character in object_id):
        raise ValueError(f"location {location!r} for entity_id {entity_id!r} is not canonical")
    if location_kind == "in":
        predicate = WorldStateRelationPredicate.INSIDE
    elif location_kind == "on":
        predicate = WorldStateRelationPredicate.ON_TOP_OF
    else:
        raise ValueError(f"location {location!r} for entity_id {entity_id!r} cannot be represented")
    return WorldStateRelation(
        subject_id=entity_id,
        predicate=predicate,
        object_id=object_id,
        belief=WorldStateBelief.OBSERVED,
        evidence_refs=list(evidence_refs),
    )


def canonical_world_state_bytes(snapshot: ContractWorldState) -> bytes:
    """Return canonical hash material, excluding snapshot metadata fields."""
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
        canonical_json = json.dumps(
            material,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return canonical_json.encode("utf-8")
    except (UnicodeError, ValueError) as error:
        raise ValueError("snapshot hash material must contain only finite UTF-8 JSON values") from error


def create_world_state_snapshot(run_id: str, events: list[WorldEvent]) -> ContractWorldState:
    """Project validated ordered events into the public WorldState contract."""
    ordered_events = _validated_event_stream(run_id, events)
    if not ordered_events:
        raise ValueError("cannot create a WorldState snapshot from an empty event stream")

    reduced_at = ordered_events[-1].occurred_at
    if not reduced_at.strip():
        raise ValueError("snapshot reduced_at boundary must be a non-empty occurred_at value")

    internal_state = WorldState(run_id=run_id)
    for event in ordered_events:
        internal_state = apply_event(internal_state, event)

    entities: list[WorldStateEntity] = []
    for entity_id in sorted(internal_state.entity_types):
        values: dict[str, Any] = {
            "entity_id": entity_id,
            "entity_type": internal_state.entity_types[entity_id],
            "belief": WorldStateBelief.OBSERVED,
            "confidence": internal_state.entity_confidence.get(entity_id),
            "evidence_refs": list(internal_state.entity_evidence_refs.get(entity_id, [])),
        }
        if entity_id in internal_state.entity_poses:
            values["pose"] = Pose.model_validate(internal_state.entity_poses[entity_id])
        if entity_id in internal_state.entity_last_observed_at:
            values["last_observed_at"] = internal_state.entity_last_observed_at[entity_id]
        entities.append(WorldStateEntity.model_validate(values))

    relations = sorted(
        (
            _relation_from_location(
                entity_id,
                location,
                internal_state.entity_location_evidence_refs.get(entity_id, []),
            )
            for entity_id, location in internal_state.entity_locations.items()
        ),
        key=lambda relation: (
            relation.subject_id,
            relation.predicate.value,
            relation.object_id,
        ),
    )
    snapshot = ContractWorldState(
        run_id=run_id,
        sequence_no=ordered_events[-1].sequence_no,
        state_hash="0" * 64,
        entities=entities,
        relations=relations,
        reduced_at=reduced_at,
    )
    state_hash = hashlib.sha256(canonical_world_state_bytes(snapshot)).hexdigest()
    return snapshot.model_copy(update={"state_hash": state_hash})
