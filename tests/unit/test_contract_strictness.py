import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libs" / "contracts"))

from workbench_contracts import (
    ActionResult,
    ActionType,
    EmotionIntent,
    Observation,
    Orientation,
    Pose,
    Position,
    ScenarioManifest,
    SemanticAction,
    TaskGraph,
    TaskStep,
    VerificationResult,
    VerificationStatus,
    WorldEntity,
    WorldEvent,
    WorldEventType,
    WorldRelation,
    WorldState,
    WorldStateBelief,
    WorldStateEntity,
)

CANONICAL_MODELS = (
    Position,
    Orientation,
    Pose,
    Observation,
    SemanticAction,
    ActionResult,
    WorldEvent,
    EmotionIntent,
    TaskStep,
    TaskGraph,
    VerificationResult,
    ScenarioManifest,
    WorldEntity,
    WorldRelation,
    WorldState,
)


def valid_pose() -> Pose:
    return Pose(
        frame_id="table",
        position=Position(x=0.2, y=0.1, z=0.02),
        orientation=Orientation(x=0.0, y=0.0, z=0.0, w=1.0),
    )


def valid_observation(**updates: object) -> Observation:
    values: dict[str, object] = {
        "observation_id": "obs-001",
        "run_id": "run-001",
        "entity_id": "red_block",
        "entity_type": "block",
        "pose": valid_pose(),
        "confidence": 0.98,
        "observed_at": "2026-08-25T00:00:00Z",
        "source": "test_camera",
        "evidence_refs": ["camera-frame-001"],
    }
    return Observation(**(values | updates))


@pytest.mark.parametrize("model", CANONICAL_MODELS, ids=lambda model: model.__name__)
def test_canonical_runtime_models_share_fail_closed_configuration(model: type) -> None:
    assert model.model_config["strict"] is True
    assert model.model_config["extra"] == "forbid"


def test_unknown_fields_are_rejected_at_top_level_and_nested_boundaries() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SemanticAction(
            action_id="act-001",
            action_type=ActionType.OBSERVE,
            unexpected=True,
        )

    payload = json.loads((ROOT / "interfaces" / "examples" / "observation-red-block.json").read_text())
    payload["pose"]["position"]["unexpected"] = 0
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Observation.model_validate_json(json.dumps(payload))


@pytest.mark.parametrize(
    "operation",
    [
        lambda: Position.model_validate({"x": "1", "y": 2.0, "z": 3.0}),
        lambda: Position.model_validate_json('{"x":"1","y":2.0,"z":3.0}'),
        lambda: WorldEvent(
            event_id="evt-001",
            run_id="run-001",
            sequence_no=True,
            event_type=WorldEventType.OBSERVATION,
            occurred_at="2026-08-25T00:00:00Z",
            payload={},
        ),
        lambda: ScenarioManifest(
            scenario_id="scenario-001",
            seed=True,
            task_id="task-001",
            world_version="v1",
            fault_type="none",
            timeout_s=120,
            oracle_allowed=False,
        ),
        lambda: SemanticAction.model_validate({"action_id": "act-001", "action_type": "observe"}),
    ],
    ids=["python-number-string", "json-number-string", "bool-as-sequence", "bool-as-seed", "python-enum-string"],
)
def test_implicit_python_and_json_coercion_is_rejected(operation) -> None:
    with pytest.raises(ValidationError):
        operation()


def test_schema_declared_empty_collections_are_rejected() -> None:
    with pytest.raises(ValidationError, match="steps"):
        TaskGraph(task_id="task-empty", goal="test", steps=[], planner="test", model_route="template")

    with pytest.raises(ValidationError, match="evidence_refs"):
        valid_observation(evidence_refs=[])

    with pytest.raises(ValidationError, match="evidence_refs"):
        VerificationResult(
            verification_id="verification-001",
            run_id="run-001",
            task_id="task-001",
            claim="insufficient evidence",
            status=VerificationStatus.INSUFFICIENT_EVIDENCE,
            evidence_refs=[],
            verified_at="2026-08-25T00:00:00Z",
        )


def test_observation_hamming_matches_schema_minimum() -> None:
    with pytest.raises(ValidationError, match="hamming"):
        valid_observation(hamming=-1)

    repository_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "observation.schema.json").read_text(encoding="utf-8")
    )
    model_schema = Observation.model_json_schema()
    integer_branch = next(
        branch for branch in model_schema["properties"]["hamming"]["anyOf"] if branch.get("type") == "integer"
    )
    assert integer_branch["minimum"] == repository_schema["properties"]["hamming"]["minimum"] == 0


@pytest.mark.parametrize("field_name", ["pose", "confidence"])
def test_world_state_entity_optional_fields_reject_explicit_null(field_name: str) -> None:
    with pytest.raises(ValidationError, match=field_name):
        WorldStateEntity(
            entity_id="red_block",
            entity_type="block",
            belief=WorldStateBelief.OBSERVED,
            **{field_name: None},
        )


def test_world_state_clock_id_rejects_explicit_null() -> None:
    with pytest.raises(ValidationError, match="clock_id"):
        WorldState(
            run_id="run-001",
            sequence_no=0,
            state_hash="0" * 64,
            entities=[],
            reduced_at="2026-08-26T00:00:00Z",
            clock_id=None,
        )


def test_world_state_optional_non_null_fields_are_omitted_and_schema_valid() -> None:
    entity = WorldStateEntity(
        entity_id="red_block",
        entity_type="block",
        belief=WorldStateBelief.OBSERVED,
        last_observed_at=None,
    )
    state = WorldState(
        run_id="run-001",
        sequence_no=0,
        state_hash="0" * 64,
        entities=[entity],
        reduced_at="2026-08-26T00:00:00Z",
    )

    payload = json.loads(state.model_dump_json())
    serialized_entity = payload["entities"][0]

    assert payload == state.model_dump(mode="json")
    assert "pose" not in serialized_entity
    assert "confidence" not in serialized_entity
    assert "clock_id" not in payload
    assert serialized_entity["last_observed_at"] is None

    repository_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "world_state.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(repository_schema).validate(payload)


def test_world_state_last_observed_at_accepts_explicit_null() -> None:
    entity = WorldStateEntity(
        entity_id="red_block",
        entity_type="block",
        belief=WorldStateBelief.OBSERVED,
        last_observed_at=None,
    )

    assert entity.model_dump(mode="json")["last_observed_at"] is None


def world_state_entity_type_payload(entity_type: object) -> dict[str, object]:
    return {
        "run_id": "run-entity-type-boundary",
        "sequence_no": 0,
        "state_hash": "0" * 64,
        "entities": [{"entity_id": "red_block", "entity_type": entity_type, "belief": WorldStateBelief.OBSERVED}],
        "reduced_at": "2026-09-06T00:00:00Z",
    }


@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize(
    "entity_type",
    ["block", "", "   ", "\t\r\n", "\u00a0", " 方块 "],
    ids=["normal", "empty", "spaces", "ascii-whitespace", "unicode-whitespace", "unicode-with-spaces"],
)
def test_world_state_entity_type_matches_schema_for_all_strings(entity_type: str, mode: str) -> None:
    # The public contract accepts strings as-is. Observation ingestion owns
    # the stronger non-blank policy; it must not leak into the shared model.
    payload = world_state_entity_type_payload(entity_type)
    repository_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "world_state.schema.json").read_text(encoding="utf-8")
    )
    validator = Draft202012Validator(repository_schema)
    validator.validate(payload)
    if mode == "python":
        state = WorldState.model_validate(payload)
    else:
        state = WorldState.model_validate_json(json.dumps(payload))
    assert type(state.entities[0]) is WorldEntity is WorldStateEntity
    assert state.entities[0].entity_type == entity_type

    serialized = json.loads(state.model_dump_json())
    assert serialized["entities"][0]["entity_type"] == entity_type
    validator.validate(serialized)
    assert WorldState.model_validate_json(json.dumps(serialized)) == state


@pytest.mark.parametrize("mode", ["python", "json"])
@pytest.mark.parametrize(
    "entity_type",
    [None, 123, 0.5, True, [], {}],
    ids=["null", "integer", "float", "boolean", "array", "object"],
)
def test_world_state_entity_type_matches_schema_for_non_strings(entity_type: object, mode: str) -> None:
    payload = world_state_entity_type_payload(entity_type)
    repository_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "world_state.schema.json").read_text(encoding="utf-8")
    )
    errors = list(Draft202012Validator(repository_schema).iter_errors(payload))
    assert len(errors) == 1
    assert list(errors[0].path) == ["entities", 0, "entity_type"]
    with pytest.raises(ValidationError) as error:
        if mode == "python":
            WorldState.model_validate(payload)
        else:
            WorldState.model_validate_json(json.dumps(payload))
    assert [entry["loc"] for entry in error.value.errors()] == [("entities", 0, "entity_type")]


def test_task_graph_steps_matches_schema_minimum() -> None:
    repository_schema = json.loads(
        (ROOT / "interfaces" / "json_schema" / "task_graph.schema.json").read_text(encoding="utf-8")
    )
    model_schema = TaskGraph.model_json_schema()
    assert model_schema["properties"]["steps"]["minItems"] == repository_schema["properties"]["steps"]["minItems"]


@pytest.mark.parametrize(
    ("example_name", "model"),
    [
        ("action-result-place-confirmed.json", ActionResult),
        ("observation-red-block.json", Observation),
        ("scenario-normal-001.json", ScenarioManifest),
        ("semantic-action-place.json", SemanticAction),
        ("verification-insufficient-evidence.json", VerificationResult),
        ("world-state-block-in-tray.json", WorldState),
    ],
)
def test_valid_repository_examples_round_trip_deterministically(example_name: str, model: type) -> None:
    payload = (ROOT / "interfaces" / "examples" / example_name).read_text(encoding="utf-8")

    value = model.model_validate_json(payload)
    first = value.model_dump_json()
    second = value.model_dump_json()

    assert first == second
    assert model.model_validate_json(first) == value


def test_valid_world_event_and_task_graph_json_round_trip() -> None:
    world_event_json = json.dumps(
        {
            "event_id": "evt-001",
            "run_id": "run-001",
            "sequence_no": 0,
            "event_type": "observation",
            "occurred_at": "2026-08-25T00:00:00Z",
            "payload": {},
        }
    )
    task_graph_json = json.dumps(
        {
            "task_id": "task-001",
            "goal": "observe the workbench",
            "steps": [
                {
                    "step_id": "step-001",
                    "action": {"action_id": "act-001", "action_type": "observe"},
                }
            ],
            "planner": "template-v1",
            "model_route": "template",
        }
    )

    for model, payload in ((WorldEvent, world_event_json), (TaskGraph, task_graph_json)):
        value = model.model_validate_json(payload)
        serialized = value.model_dump_json()
        assert model.model_validate_json(serialized) == value
