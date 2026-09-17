"""Deterministic snapshot replay tests ported from PR 347.

The snapshot API landed on main via #340. These tests pin the parts of that
behaviour that were previously only covered on the PR branch: permutation
insensitivity, canonical hash material, fail-closed location handling, and
cross-process hash stability.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from itertools import permutations
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

from workbench_contracts import ClockId, WorldEvent, WorldEventType
from workbench_contracts import WorldState as ContractWorldState
from workbench_world_model import (
    canonical_world_state_bytes,
    create_world_state_snapshot,
    reduce_events,
)
from workbench_world_model.reducer import WorldState as ReducerWorldState


def observation_event(
    event_id: str,
    sequence_no: int,
    *,
    entity_id: str,
    entity_type: str = "block",
    location: str | None = None,
    confidence: float = 0.9,
    occurred_at: str | None = None,
    pose: dict[str, object] | None = None,
    evidence_refs: list[str] | None = None,
) -> WorldEvent:
    payload: dict[str, object] = {
        "entity_id": entity_id,
        "entity_type": entity_type,
        "confidence": confidence,
    }
    if location is not None:
        payload["location"] = location
    if pose is not None:
        payload["pose"] = pose
    return WorldEvent(
        event_id=event_id,
        run_id="run-snapshot",
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at=occurred_at or f"2026-08-26T00:00:{sequence_no:02d}Z",
        payload=payload,
        evidence_refs=evidence_refs or [f"frame://{event_id}"],
    )


def repository_schema_registry() -> Registry:
    schema_dir = ROOT / "interfaces" / "json_schema"
    return Registry().with_resources(
        [
            (
                path.name,
                Resource.from_contents(json.loads(path.read_text(encoding="utf-8"))),
            )
            for path in sorted(schema_dir.glob("*.schema.json"))
        ]
    )


def test_snapshot_is_contract_valid_ordered_and_preserves_internal_reducer() -> None:
    pose = {
        "frame_id": "table",
        "position": {"x": 0.2, "y": 0.1, "z": 0.02},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }
    events = [
        observation_event(
            "evt-tray",
            2,
            entity_id="tray",
            entity_type="tray",
            location="on:table",
            occurred_at="2026-08-26T00:00:01Z",
        ),
        observation_event(
            "evt-block-table",
            0,
            entity_id="red_block",
            location="on:table",
            occurred_at="2026-08-26T00:00:09Z",
            pose=pose,
        ),
        observation_event(
            "evt-block-tray",
            1,
            entity_id="red_block",
            location="in:tray",
            occurred_at="2026-08-26T00:00:05Z",
        ),
    ]

    internal = reduce_events("run-snapshot", events)
    snapshot = create_world_state_snapshot("run-snapshot", events)
    payload = snapshot.model_dump(mode="json")

    assert type(internal) is ReducerWorldState
    assert type(snapshot) is ContractWorldState
    assert internal.entity_locations["red_block"] == "in:tray"
    assert [entity.entity_id for entity in snapshot.entities] == ["red_block", "tray"]
    assert [(relation.subject_id, relation.predicate.value, relation.object_id) for relation in snapshot.relations] == [
        ("red_block", "inside", "tray"),
        ("tray", "on_top_of", "table"),
    ]
    assert snapshot.sequence_no == 2
    assert snapshot.reduced_at == "2026-08-26T00:00:01Z"
    assert snapshot.entities[0].pose is not None
    assert len(snapshot.state_hash) == 64
    assert snapshot.state_hash.isascii()
    assert snapshot.state_hash.islower()
    assert json.loads(snapshot.model_dump_json()) == payload
    assert "pose" not in next(entity for entity in payload["entities"] if entity["entity_id"] == "tray")
    assert all(entity["last_observed_at"] is None for entity in payload["entities"])
    assert ContractWorldState.model_validate_json(json.dumps(payload)) == snapshot

    schema = json.loads((ROOT / "interfaces" / "json_schema" / "world_state.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator(schema, registry=repository_schema_registry()).validate(payload)


def test_every_input_permutation_and_exact_duplicate_has_the_same_hash() -> None:
    events = [
        observation_event("evt-a", 0, entity_id="zeta"),
        observation_event("evt-b", 1, entity_id="alpha", location="in:tray"),
        observation_event("evt-c", 2, entity_id="tray", entity_type="tray"),
    ]
    events.append(events[1].model_copy(deep=True))

    hashes = {create_world_state_snapshot("run-snapshot", list(order)).state_hash for order in permutations(events)}

    assert len(hashes) == 1


def test_canonical_bytes_have_exact_fields_and_exclude_snapshot_metadata() -> None:
    event = observation_event(
        "evt-canonical",
        0,
        entity_id="alpha",
        confidence=0.5,
        occurred_at="2026-08-26T10:00:00Z",
    )
    snapshot = create_world_state_snapshot("run-snapshot", [event])
    expected = (
        b'{"entities":[{"belief":"observed","confidence":0.5,"entity_id":"alpha",'
        b'"entity_type":"block","evidence_refs":["frame://evt-canonical"],'
        b'"last_observed_at":null}],'
        b'"relations":[],"run_id":"run-snapshot","sequence_no":0}'
    )

    assert canonical_world_state_bytes(snapshot) == expected
    assert snapshot.state_hash == hashlib.sha256(expected).hexdigest()

    metadata_variant = snapshot.model_copy(
        update={
            "reduced_at": "2099-12-31T23:59:59Z",
            "clock_id": ClockId.WALL,
        }
    )
    assert canonical_world_state_bytes(metadata_variant) == expected
    assert hashlib.sha256(canonical_world_state_bytes(metadata_variant)).hexdigest() == snapshot.state_hash
    assert set(json.loads(expected)) == {"run_id", "sequence_no", "entities", "relations"}
    assert "clock_id" not in expected.decode("utf-8")
    assert "reduced_at" not in expected.decode("utf-8")
    assert "state_hash" not in expected.decode("utf-8")


def test_schema_visible_semantic_change_changes_hash_but_reduced_at_does_not() -> None:
    base = observation_event(
        "evt-semantic",
        0,
        entity_id="alpha",
        confidence=0.5,
        occurred_at="2026-08-26T10:00:00Z",
    )
    semantic_change = base.model_copy(
        update={"payload": {**base.payload, "confidence": 0.6}},
        deep=True,
    )
    metadata_change = base.model_copy(update={"occurred_at": "2030-01-01T00:00:00Z"})

    base_snapshot = create_world_state_snapshot("run-snapshot", [base])
    semantic_snapshot = create_world_state_snapshot("run-snapshot", [semantic_change])
    metadata_snapshot = create_world_state_snapshot("run-snapshot", [metadata_change])

    assert semantic_snapshot.state_hash != base_snapshot.state_hash
    assert metadata_snapshot.reduced_at != base_snapshot.reduced_at
    assert metadata_snapshot.state_hash == base_snapshot.state_hash


def test_optional_pose_and_location_are_not_invented() -> None:
    snapshot = create_world_state_snapshot(
        "run-snapshot",
        [observation_event("evt-minimal", 0, entity_id="minimal")],
    )
    entity = snapshot.entities[0]

    assert entity.pose is None
    assert snapshot.relations == []


def test_relation_evidence_excludes_later_non_location_observations() -> None:
    events = [
        observation_event(
            "evt-location",
            0,
            entity_id="red_block",
            location="in:tray",
            evidence_refs=["location-proof"],
        ),
        observation_event(
            "evt-appearance",
            1,
            entity_id="red_block",
            evidence_refs=["appearance-only"],
        ),
    ]

    snapshot = create_world_state_snapshot("run-snapshot", events)

    assert snapshot.entities[0].evidence_refs == ["location-proof", "appearance-only"]
    assert snapshot.relations[0].evidence_refs == ["location-proof"]


def test_relation_evidence_switches_on_location_change_and_accumulates_for_same_location() -> None:
    events = [
        observation_event(
            "evt-old-location",
            0,
            entity_id="red_block",
            location="in:tray",
            evidence_refs=["old-location-proof"],
        ),
        observation_event(
            "evt-old-confirmation",
            1,
            entity_id="red_block",
            location="in:tray",
            evidence_refs=["old-location-confirmation"],
        ),
        observation_event(
            "evt-new-location",
            2,
            entity_id="red_block",
            location="on:table",
            evidence_refs=["new-location-proof"],
        ),
        observation_event(
            "evt-new-confirmation",
            3,
            entity_id="red_block",
            location="on:table",
            evidence_refs=["new-location-confirmation"],
        ),
    ]

    snapshot = create_world_state_snapshot("run-snapshot", events)
    relation = snapshot.relations[0]

    assert relation.predicate.value == "on_top_of"
    assert relation.object_id == "table"
    assert relation.evidence_refs == ["new-location-proof", "new-location-confirmation"]


@pytest.mark.parametrize("location", ["table", "inside:tray", "in:", "on:"])
def test_unrepresentable_non_empty_location_fails_snapshot_creation(location: str) -> None:
    event = observation_event("evt-location", 0, entity_id="alpha", location=location)

    with pytest.raises(ValueError, match="location"):
        create_world_state_snapshot("run-snapshot", [event])


def test_empty_stream_keeps_internal_behavior_but_snapshot_fails_closed() -> None:
    assert reduce_events("run-snapshot", []) == ReducerWorldState(run_id="run-snapshot")

    with pytest.raises(ValueError, match="empty"):
        create_world_state_snapshot("run-snapshot", [])


def test_non_finite_pose_fails_canonical_snapshot_creation() -> None:
    pose = {
        "frame_id": "table",
        "position": {"x": float("nan"), "y": 0.0, "z": 0.0},
        "orientation": {"x": 0.0, "y": 0.0, "z": 0.0, "w": 1.0},
    }
    event = observation_event("evt-non-finite", 0, entity_id="alpha", pose=pose)

    with pytest.raises(ValueError, match="finite"):
        create_world_state_snapshot("run-snapshot", [event])


def test_two_independent_python_processes_produce_the_same_hash() -> None:
    script = """
from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model import create_world_state_snapshot
events = [
    WorldEvent(
        event_id="evt-zeta",
        run_id="run-process",
        sequence_no=1,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-26T00:00:01Z",
        payload={"entity_id": "zeta", "entity_type": "block", "location": "on:table", "confidence": 0.9},
        evidence_refs=["frame-zeta"],
    ),
    WorldEvent(
        event_id="evt-alpha",
        run_id="run-process",
        sequence_no=0,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-26T00:00:00Z",
        payload={"entity_id": "alpha", "entity_type": "block", "location": "in:tray", "confidence": 0.8},
        evidence_refs=["frame-alpha"],
    ),
]
print(create_world_state_snapshot("run-process", events).state_hash)
"""
    python_path = os.pathsep.join(
        [
            str(ROOT / "libs/contracts"),
            str(ROOT / "services/world_model"),
        ]
    )
    hashes = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = python_path
        hashes.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                text=True,
            ).strip()
        )

    assert hashes[0] == hashes[1]
    assert len(hashes[0]) == 64
