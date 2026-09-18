"""Property suite: policy rejection is closed under raw-control smuggling (Issue #89).

The boundary under test is ``workbench_agent_runtime.policy_validator``.  It
promises that a raw-control identifier is denied wherever it can act as a
parameter name -- as a mapping key, as an identifier-shaped string value, joined
by a separator, camel-cased, or decoded out of a JSON string -- and that an
ordinary prose value which merely mentions the same words is still allowed.

That second half is what a naive substring check gets wrong, so the generator
emits both directions and asserts the decision follows the shape of the payload
rather than the presence of a word:

* a mapping key carrying a raw token is denied;
* a string value that is exactly one identifier carrying a raw token is denied;
* a prose value carrying the same word inside a sentence is allowed.
"""

from __future__ import annotations

import pytest
from workbench_agent_runtime.policy_validator import PolicyValidator, PolicyViolation
from workbench_agent_runtime.tool_registry import ToolRegistry
from workbench_agent_runtime.tool_schemas import TOOL_SCHEMAS
from workbench_contracts import ActionType, SemanticAction, TaskGraph, TaskStep

from ._generator import SEEDS, Rng, assert_corpus_clean, report_for, run_corpus, write_archive_if_requested

SUITE = "policy_rejection"
MIN_CASES = 3 * 120

POLICY_VERSION = "property-policy-v1"

# Identifier-shaped spellings.  Every one of these must be denied, whether it
# arrives as a mapping key or as a whole string value.
IDENTIFIER_SPELLINGS = (
    "joint_velocity",
    "jointVelocity",
    "JOINTVELOCITY",
    "Joint.Velocity",
    "torque-limit",
    "torqueLimit",
    "firmware_mode",
    "firmwareMode",
    "joint-velocity",
    "arm.joint_velocity",
    "joint=1",
)

# Spellings that only a decoder can see, so a check that never decodes a JSON
# string would miss them.
ENCODED_SPELLINGS = (
    '{"joint\\u005fvelocity":1}',
    '{"torque_limit": 3.0}',
)

# Prose that must survive.  A word-boundary-free substring check denies these,
# which would make the validator reject an operator's explanation.
PROSE_SPELLINGS = (
    "joint velocity",
    "velocity profile",
    "joint//velocity",
    "joint: 1",
    "note about velocity",
    "This explanation mentions joint behaviour, velocity profiles, torque margins "
    "and firmware safety without encoding a control parameter.",
    "Firmware: safety remains outside this policy.",
    "Joint: a mechanical connection, not a command.",
)


def registry(kind: type) -> ToolRegistry:
    schema = dict(TOOL_SCHEMAS[ActionType.STOP])
    schema["optional_params"] = schema["optional_params"] | frozenset({"payload"})
    schema["param_types"] = {**schema["param_types"], "payload": kind}
    built = ToolRegistry(load_defaults=False)
    built.register(ActionType.STOP, schema)
    return built


def validator(kind: type) -> PolicyValidator:
    return PolicyValidator(
        registry=registry(kind),
        policy_config={"policy_version": POLICY_VERSION, "high_impact_actions": frozenset()},
    )


def graph(parameters: dict) -> TaskGraph:
    action = SemanticAction(action_id="act-property", action_type=ActionType.STOP, parameters=parameters)
    return TaskGraph(
        task_id="task-property-policy",
        goal="exercise the raw-control boundary",
        steps=[TaskStep(step_id="step-property", action=action)],
        planner="property",
        model_route="template",
    )


def decide(parameters: dict, kind: type) -> tuple[bool, str | None]:
    report = validator(kind).check(graph(parameters))
    reason = report.decisions[0].reason_code.value if report.decisions else None
    return report.is_valid, reason


def cases() -> list[dict]:
    generated: list[dict] = []
    for seed in SEEDS:
        rng = Rng(seed)
        for _ in range(120):
            generated.append(
                {
                    "kind": "denied_key",
                    "parameters": {"payload": [{rng.choice(IDENTIFIER_SPELLINGS): 1}]},
                    "kind_hint": "list",
                }
            )
            generated.append(
                {
                    "kind": "denied_value",
                    "parameters": {"payload": rng.choice(IDENTIFIER_SPELLINGS)},
                    "kind_hint": "str",
                }
            )
            generated.append(
                {
                    "kind": "allowed_prose",
                    "parameters": {"payload": rng.choice(PROSE_SPELLINGS)},
                    "kind_hint": "str",
                }
            )
    return generated


def check(case: dict) -> None:
    kind = str if case["kind_hint"] == "str" else list
    valid, reason = decide(case["parameters"], kind)

    if case["kind"].startswith("denied"):
        assert not valid, f"raw-control payload was accepted: {case['parameters']!r}"
        assert reason == "raw_control_parameter", f"wrong reason {reason!r} for {case['parameters']!r}"
        return
    assert valid, f"prose payload was denied: {case['parameters']!r} reason={reason!r}"


def test_generated_raw_control_payloads_are_denied_and_prose_is_allowed() -> None:
    report = run_corpus(SUITE, cases(), check)
    assert_corpus_clean(report)
    assert report.case_count >= MIN_CASES, f"corpus shrank to {report.case_count} cases"
    assert report.corpus_digest


def test_every_identifier_spelling_is_denied_as_a_value_and_as_a_key() -> None:
    for spelling in (*IDENTIFIER_SPELLINGS, *ENCODED_SPELLINGS):
        valid, reason = decide({"payload": spelling}, str)
        assert not valid, f"value was accepted: {spelling!r}"
        assert reason == "raw_control_parameter"

        valid, reason = decide({"payload": [{spelling: 1}]}, list)
        assert not valid, f"key was accepted: {spelling!r}"
        assert reason == "raw_control_parameter"


def test_every_prose_spelling_survives() -> None:
    for spelling in PROSE_SPELLINGS:
        valid, reason = decide({"payload": spelling}, str)
        assert valid, f"prose was denied: {spelling!r} reason={reason!r}"


def test_enforce_raises_instead_of_returning_a_denial() -> None:
    with pytest.raises(PolicyViolation):
        validator(str).enforce(graph({"payload": "joint_velocity"}))


def test_a_non_string_mapping_key_fails_closed() -> None:
    valid, reason = decide({"payload": [{("torque_limit",): 1}]}, list)
    assert not valid
    assert reason == "policy_input_malformed"


def test_excessive_nesting_fails_closed_structurally() -> None:
    payload: object = "safe"
    for _ in range(200):
        payload = [payload]

    valid, reason = decide({"payload": payload}, list)
    assert not valid
    assert reason == "policy_input_malformed"


def test_the_corpus_is_reproducible_for_a_fixed_seed() -> None:
    write_archive_if_requested()
    first = cases()
    second = cases()
    assert [case["parameters"] for case in first] == [case["parameters"] for case in second]
    assert report_for(SUITE).seeds == SEEDS
