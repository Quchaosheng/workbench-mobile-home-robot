from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/agent_runtime"),
]

from workbench_agent_runtime.policy_validator import PolicyValidator
from workbench_agent_runtime.tool_registry import ToolRegistry
from workbench_contracts import (
    ActionOutcome,
    ActionResult,
    ActionType,
    DeviceState,
    DispatchState,
    SemanticAction,
    TaskGraph,
    TaskStep,
)

SCHEMA = json.loads((ROOT / "interfaces/json_schema/semantic_action.schema.json").read_text(encoding="utf-8"))
TARGETS = {
    ActionType.OPEN: "washer_door_fixture",
    ActionType.CLOSE: "dishwasher_rack_fixture",
}


def action_payload(action_type: ActionType, **updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action_id": f"act-{action_type.value}-fixture",
        "action_type": action_type.value,
        "target_id": TARGETS[action_type],
        "parameters": {},
    }
    payload.update(updates)
    return payload


def typed_action(action_type: ActionType, **updates: object) -> SemanticAction:
    payload = action_payload(action_type, **updates)
    return SemanticAction(
        action_id=payload["action_id"],
        action_type=action_type,
        target_id=payload.get("target_id"),
        parameters=payload.get("parameters", {}),
    )


def action_graph(action: SemanticAction) -> TaskGraph:
    return TaskGraph(
        task_id="task-open-close",
        goal="operate the configured articulated fixture",
        steps=[TaskStep(step_id="operate-fixture", action=action)],
        planner="test",
        model_route="template",
    )


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_versioned_examples_are_valid_against_schema_and_model(action_type: ActionType) -> None:
    example_path = ROOT / f"interfaces/examples/semantic-action-{action_type.value}.json"
    raw = example_path.read_text(encoding="utf-8")
    payload = json.loads(raw)

    assert list(Draft202012Validator(SCHEMA).iter_errors(payload)) == []

    action = SemanticAction.model_validate_json(raw)

    assert action.action_type is action_type
    assert action.target_id == TARGETS[action_type]
    assert action.parameters == {}
    assert SemanticAction.model_validate_json(action.model_dump_json()) == action


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_generated_model_exposes_the_same_closed_boundary(action_type: ActionType) -> None:
    generated_schema = SemanticAction.model_json_schema()
    valid = action_payload(action_type)

    assert list(Draft202012Validator(generated_schema).iter_errors(valid)) == []

    for parameters in (
        {"pose": {"frame_id": "map"}},
        {"trajectory": []},
        {"velocity": 0.1},
        {"force": 10.0},
        {"controller": "raw"},
    ):
        assert list(
            Draft202012Validator(generated_schema).iter_errors(action_payload(action_type, parameters=parameters))
        )


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
@pytest.mark.parametrize(
    "updates",
    [
        {"target_id": None},
        {"target_id": ""},
        {"target_id": "   "},
        {"parameters": {"policy": "cautious"}},
        {"parameters": {"policy_profile": "standard"}},
        {"parameters": {"path": "fixture-path"}},
    ],
    ids=["missing-target", "empty-target", "blank-target", "policy-selector", "policy-profile", "path"],
)
def test_python_and_json_ingress_reject_unbounded_action_fields(
    action_type: ActionType,
    updates: dict[str, object],
) -> None:
    payload = action_payload(action_type, **updates)

    with pytest.raises(ValidationError):
        SemanticAction.model_validate({**payload, "action_type": action_type})

    with pytest.raises(ValidationError):
        SemanticAction.model_validate_json(json.dumps(payload))

    assert list(Draft202012Validator(SCHEMA).iter_errors(payload))


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_target_id_is_required_when_omitted(action_type: ActionType) -> None:
    payload = action_payload(action_type)
    payload.pop("target_id")

    with pytest.raises(ValidationError):
        SemanticAction.model_validate({**payload, "action_type": action_type})
    with pytest.raises(ValidationError):
        SemanticAction.model_validate_json(json.dumps(payload))
    assert list(Draft202012Validator(SCHEMA).iter_errors(payload))


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_unknown_top_level_fields_are_rejected(action_type: ActionType) -> None:
    payload = action_payload(action_type, unexpected="value")

    with pytest.raises(ValidationError, match="extra_forbidden"):
        SemanticAction.model_validate({**payload, "action_type": action_type})
    assert list(Draft202012Validator(SCHEMA).iter_errors(payload))


def test_legacy_actions_remain_valid_with_new_action_values() -> None:
    for name in ("semantic-action-place.json", "semantic-action-navigate.json"):
        raw = (ROOT / "interfaces/examples" / name).read_text(encoding="utf-8")
        payload = json.loads(raw)
        assert list(Draft202012Validator(SCHEMA).iter_errors(payload)) == []
        assert SemanticAction.model_validate_json(raw).action_type.value == payload["action_type"]


def test_registry_and_policy_register_both_articulated_actions() -> None:
    registry = ToolRegistry()

    assert ActionType.OPEN in registry.list_all()
    assert ActionType.CLOSE in registry.list_all()
    for action_type in (ActionType.OPEN, ActionType.CLOSE):
        action = typed_action(action_type)
        assert registry.validate(action).is_valid
        report = PolicyValidator(
            registry=registry,
            policy_config={"policy_version": "open-close-policy-v1", "high_impact_actions": frozenset()},
        ).check(action_graph(action))
        assert report.is_valid, report.findings


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_policy_confirmation_can_be_required_without_changing_stop(action_type: ActionType) -> None:
    action = typed_action(action_type, action_id=f"act-{action_type.value}-confirm")
    validator = PolicyValidator(
        policy_config={
            "policy_version": "open-close-policy-v1",
            "high_impact_actions": frozenset({action_type}),
        }
    )

    required = validator.check(action_graph(action))
    confirmed = validator.check(
        action_graph(action),
        confirmed_action_ids=frozenset({action.action_id}),
    )
    stop = SemanticAction(action_id="act-stop", action_type=ActionType.STOP)
    stop_report = validator.check(action_graph(stop))

    assert not required.is_valid
    assert required.decisions[0].reason_code.value == "action_confirmation_required"
    assert confirmed.is_valid
    assert stop_report.is_valid


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_registry_rejects_malformed_lower_boundary_actions(action_type: ActionType) -> None:
    malformed = SemanticAction.model_construct(
        action_id=f"act-{action_type.value}-malformed",
        action_type=action_type,
        target_id=" ",
        parameters={"force": 1.0},
    )

    result = ToolRegistry().validate(malformed)

    assert not result.is_valid
    assert any(error.field == "target_id" for error in result.errors)
    assert any("forbidden keys" in error.message for error in result.errors)


@pytest.mark.parametrize("action_type", [ActionType.OPEN, ActionType.CLOSE], ids=lambda item: item.value)
def test_completed_articulated_action_result_does_not_claim_observation(action_type: ActionType) -> None:
    result = ActionResult(
        result_id=f"result-{action_type.value}",
        action_id=f"act-{action_type.value}-fixture",
        run_id="run-open-close",
        outcome=ActionOutcome.COMPLETED,
        dispatch_state=DispatchState.SENT,
        device_state=DeviceState.CONFIRMED,
        started_at="2026-09-05T00:00:00Z",
        ended_at="2026-09-05T00:00:01Z",
    )

    assert result.entity_id is None
    assert result.resulting_location is None
    assert result.evidence_refs == []
