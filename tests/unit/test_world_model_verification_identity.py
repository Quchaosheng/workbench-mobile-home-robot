from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

import workbench_world_model.verifier as verifier_module
from workbench_contracts import ClockId, WorldEvent, WorldEventType
from workbench_world_model import (
    VerificationContext,
    create_world_state_snapshot,
    reduce_events,
    verify_inspection_evidence,
    verify_kit_contents,
    verify_object_in_tray,
    verify_parcel_policy,
    verify_parcel_sorting,
    verify_workspace_clearance,
)
from workbench_world_model.reducer import WorldState


def observed_event(
    *,
    location: str = "in:tray",
    evidence_refs: list[str] | None = None,
) -> WorldEvent:
    return WorldEvent(
        event_id="evt-red-block",
        run_id="run-verification-identity",
        sequence_no=1,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-27T12:00:00Z",
        payload={
            "entity_id": "red_block",
            "entity_type": "block",
            "location": location,
            "confidence": 0.98,
        },
        evidence_refs=evidence_refs or ["frame://red-block"],
    )


def verification_inputs(
    *,
    verified_at: str = "2026-08-27T12:34:56.123456Z",
) -> tuple[WorldState, VerificationContext]:
    events = [observed_event()]
    snapshot = create_world_state_snapshot("run-verification-identity", events)
    return (
        reduce_events("run-verification-identity", events),
        VerificationContext(
            state_hash=snapshot.state_hash,
            verified_at=verified_at,
            clock_id="wall",
        ),
    )


def test_identical_semantic_inputs_produce_the_same_verification_id() -> None:
    state, context = verification_inputs()

    first = verify_object_in_tray(state, "task-place", "red_block", "tray", context=context)
    second = verify_object_in_tray(state, "task-place", "red_block", "tray", context=context)

    assert first.verification_id == second.verification_id
    assert first.verification_id.startswith("ver-")
    digest = first.verification_id.removeprefix("ver-")
    assert len(digest) == 64
    assert digest == digest.lower()
    assert all(character in "0123456789abcdef" for character in digest)


def test_two_independent_python_processes_produce_the_same_verification_id() -> None:
    script = """
from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model import (
    VerificationContext,
    create_world_state_snapshot,
    reduce_events,
    verify_object_in_tray,
)
event = WorldEvent(
    event_id="evt-process",
    run_id="run-process",
    sequence_no=1,
    event_type=WorldEventType.OBSERVATION,
    occurred_at="2026-08-27T12:00:00Z",
    payload={
        "entity_id": "red_block",
        "entity_type": "block",
        "location": "in:tray",
        "confidence": 0.98,
    },
    evidence_refs=["frame://process"],
)
events = [event]
snapshot = create_world_state_snapshot("run-process", events)
state = reduce_events("run-process", events)
context = VerificationContext(
    state_hash=snapshot.state_hash,
    verified_at="2026-08-27T12:34:56Z",
    clock_id="wall",
)
print(verify_object_in_tray(
    state,
    "task-place",
    "red_block",
    "tray",
    context=context,
).verification_id)
"""
    python_path = os.pathsep.join(
        [
            str(ROOT / "libs/contracts"),
            str(ROOT / "services/world_model"),
        ]
    )
    identifiers = []
    for seed in ("1", "987654"):
        environment = os.environ.copy()
        environment["PYTHONHASHSEED"] = seed
        environment["PYTHONPATH"] = python_path
        identifiers.append(
            subprocess.check_output(
                [sys.executable, "-c", script],
                cwd=ROOT,
                env=environment,
                text=True,
            ).strip()
        )

    assert identifiers[0] == identifiers[1]


def test_verifier_does_not_emit_the_fabricated_epoch_timestamp() -> None:
    state, context = verification_inputs()

    result = verify_object_in_tray(
        state,
        "task-place",
        "red_block",
        "tray",
        context=context,
    )

    assert result.verified_at == context.verified_at
    assert result.verified_at != "1970-01-01T00:00:00Z"
    assert result.clock_id is ClockId.WALL


def test_verified_at_changes_without_changing_stable_identity() -> None:
    state, first_context = verification_inputs(verified_at="2026-08-27T12:34:56Z")
    _, second_context = verification_inputs(verified_at="2026-08-27T12:35:56.500000Z")

    first = verify_object_in_tray(state, "task-place", "red_block", "tray", context=first_context)
    second = verify_object_in_tray(state, "task-place", "red_block", "tray", context=second_context)

    assert first.verified_at != second.verified_at
    assert first.verification_id == second.verification_id


def request_context(*, state_hash: str = "d" * 64) -> VerificationContext:
    return VerificationContext(
        state_hash=state_hash,
        verified_at="2026-08-27T12:34:56Z",
        clock_id="wall",
    )


def test_inspection_targets_do_not_collide_with_shared_evidence() -> None:
    state = WorldState(
        run_id="run-inspection-request",
        entity_locations={"alpha": "on:table", "beta": "on:table"},
        entity_confidence={"alpha": 0.95, "beta": 0.95},
        entity_evidence_refs={
            "alpha": ["frame://shared"],
            "beta": ["frame://shared"],
        },
    )
    context = request_context()

    alpha = verify_inspection_evidence(state, "task-inspection", ["alpha"], context=context)
    beta = verify_inspection_evidence(state, "task-inspection", ["beta"], context=context)

    assert alpha.claim == beta.claim
    assert alpha.status == beta.status
    assert alpha.evidence_refs == beta.evidence_refs
    assert alpha.verification_id != beta.verification_id


def test_inspection_threshold_changes_identity_even_when_outcome_does_not_change() -> None:
    state = WorldState(
        run_id="run-inspection-threshold",
        entity_locations={"alpha": "on:table"},
        entity_confidence={"alpha": 0.95},
        entity_evidence_refs={"alpha": ["frame://alpha"]},
    )
    context = request_context()

    threshold_08 = verify_inspection_evidence(
        state,
        "task-inspection",
        ["alpha"],
        confidence_threshold=0.8,
        context=context,
    )
    threshold_09 = verify_inspection_evidence(
        state,
        "task-inspection",
        ["alpha"],
        confidence_threshold=0.9,
        context=context,
    )

    assert threshold_08.claim == threshold_09.claim
    assert threshold_08.status == threshold_09.status
    assert threshold_08.evidence_refs == threshold_09.evidence_refs
    assert threshold_08.verification_id != threshold_09.verification_id


def test_object_and_kit_request_parameters_affect_identity() -> None:
    context = request_context()
    object_state = WorldState(
        run_id="run-object-request",
        entity_locations={"alpha": "in:tray-a", "beta": "in:tray-a"},
        entity_evidence_refs={
            "alpha": ["frame://shared"],
            "beta": ["frame://shared"],
        },
    )
    baseline_object = verify_object_in_tray(object_state, "task-object", "alpha", "tray-a", context=context)
    changed_object = verify_object_in_tray(object_state, "task-object", "beta", "tray-a", context=context)
    changed_tray = verify_object_in_tray(object_state, "task-object", "alpha", "tray-b", context=context)

    assert baseline_object.verification_id != changed_object.verification_id
    assert baseline_object.verification_id != changed_tray.verification_id

    kit_state = WorldState(
        run_id="run-kit-request",
        entity_locations={"alpha": "in:kit-tray", "beta": "in:kit-tray"},
        entity_confidence={"alpha": 0.95, "beta": 0.95},
        entity_evidence_refs={
            "alpha": ["frame://shared"],
            "beta": ["frame://shared"],
        },
    )
    baseline_kit = verify_kit_contents(
        kit_state,
        "task-kit",
        ["alpha", "beta"],
        tray_id="kit-tray",
        confidence_threshold=0.8,
        context=context,
    )
    changed_required_ids = verify_kit_contents(
        kit_state,
        "task-kit",
        ["alpha"],
        tray_id="kit-tray",
        confidence_threshold=0.8,
        context=context,
    )
    changed_kit_tray = verify_kit_contents(
        kit_state,
        "task-kit",
        ["alpha", "beta"],
        tray_id="other-tray",
        confidence_threshold=0.8,
        context=context,
    )
    changed_kit_threshold = verify_kit_contents(
        kit_state,
        "task-kit",
        ["alpha", "beta"],
        tray_id="kit-tray",
        confidence_threshold=0.9,
        context=context,
    )

    assert baseline_kit.verification_id != changed_required_ids.verification_id
    assert baseline_kit.verification_id != changed_kit_tray.verification_id
    assert baseline_kit.claim == changed_kit_threshold.claim
    assert baseline_kit.verification_id != changed_kit_threshold.verification_id


def test_parcel_sorting_request_semantics_prevent_real_collisions() -> None:
    state = WorldState(run_id="run-parcel-sorting-request")
    context = request_context()
    base_attributes = {"parcel-a": {"label_status": "verified"}}
    baseline = verify_parcel_sorting(
        state,
        "task-sort",
        parcel_routes={"parcel-a": "pickup-a"},
        expected_attributes=base_attributes,
        confidence_threshold=0.8,
        context=context,
    )
    changed_route = verify_parcel_sorting(
        state,
        "task-sort",
        parcel_routes={"parcel-a": "pickup-b"},
        expected_attributes=base_attributes,
        confidence_threshold=0.8,
        context=context,
    )
    changed_attributes = verify_parcel_sorting(
        state,
        "task-sort",
        parcel_routes={"parcel-a": "pickup-a"},
        expected_attributes={"parcel-a": {"label_status": "unreadable"}},
        confidence_threshold=0.8,
        context=context,
    )
    changed_threshold = verify_parcel_sorting(
        state,
        "task-sort",
        parcel_routes={"parcel-a": "pickup-a"},
        expected_attributes=base_attributes,
        confidence_threshold=0.9,
        context=context,
    )

    assert baseline.claim == changed_route.claim == changed_attributes.claim == changed_threshold.claim
    assert baseline.evidence_refs == changed_route.evidence_refs == changed_attributes.evidence_refs
    assert baseline.verification_id != changed_route.verification_id
    assert baseline.verification_id != changed_attributes.verification_id
    assert baseline.verification_id != changed_threshold.verification_id


def test_parcel_policy_request_semantics_prevent_real_collisions() -> None:
    context = request_context()
    unobserved_state = WorldState(run_id="run-parcel-policy-request")
    baseline = verify_parcel_policy(
        unobserved_state,
        "task-policy",
        ["parcel-a"],
        pickup_shelf_id="pickup-a",
        quarantine_bin_id="quarantine-a",
        confidence_threshold=0.8,
        context=context,
    )
    changed_destinations = verify_parcel_policy(
        unobserved_state,
        "task-policy",
        ["parcel-a"],
        pickup_shelf_id="pickup-b",
        quarantine_bin_id="quarantine-b",
        confidence_threshold=0.8,
        context=context,
    )
    changed_threshold = verify_parcel_policy(
        unobserved_state,
        "task-policy",
        ["parcel-a"],
        pickup_shelf_id="pickup-a",
        quarantine_bin_id="quarantine-a",
        confidence_threshold=0.9,
        context=context,
    )

    assert baseline.claim == changed_destinations.claim == changed_threshold.claim
    assert baseline.verification_id != changed_destinations.verification_id
    assert baseline.verification_id != changed_threshold.verification_id

    manifest_state = WorldState(
        run_id="run-parcel-policy-request",
        entity_locations={"parcel-a": "in:pickup-a"},
        entity_confidence={"parcel-a": 0.95},
        entity_attributes={
            "parcel-a": {
                "label_status": "verified",
                "condition": "intact",
                "tracking_id": "OBSERVED",
            }
        },
        entity_evidence_refs={"parcel-a": ["frame://parcel-a"]},
    )
    manifest_a = verify_parcel_policy(
        manifest_state,
        "task-policy",
        ["parcel-a"],
        pickup_shelf_id="pickup-a",
        quarantine_bin_id="quarantine-a",
        parcel_manifest={"parcel-a": {"tracking_id": "EXPECTED-A"}},
        manifest_id="manifest-shared",
        context=context,
    )
    manifest_b = verify_parcel_policy(
        manifest_state,
        "task-policy",
        ["parcel-a"],
        pickup_shelf_id="pickup-a",
        quarantine_bin_id="quarantine-a",
        parcel_manifest={"parcel-a": {"tracking_id": "EXPECTED-B"}},
        manifest_id="manifest-shared",
        context=context,
    )
    changed_manifest_id = verify_parcel_policy(
        manifest_state,
        "task-policy",
        ["parcel-a"],
        pickup_shelf_id="pickup-a",
        quarantine_bin_id="quarantine-a",
        parcel_manifest={"parcel-a": {"tracking_id": "EXPECTED-A"}},
        manifest_id="manifest-other",
        context=context,
    )
    changed_parcel_ids = verify_parcel_policy(
        WorldState(run_id="run-parcel-policy-request"),
        "task-policy",
        ["parcel-b"],
        pickup_shelf_id="pickup-a",
        quarantine_bin_id="quarantine-a",
        context=context,
    )

    assert manifest_a.claim == manifest_b.claim
    assert manifest_a.verification_id != manifest_b.verification_id
    assert manifest_a.verification_id != changed_manifest_id.verification_id
    assert baseline.verification_id != changed_parcel_ids.verification_id


def test_semantic_collection_and_mapping_order_is_normalized() -> None:
    context = request_context()
    entity_state = WorldState(
        run_id="run-request-order",
        entity_locations={"alpha": "in:kit-tray", "beta": "in:kit-tray"},
        entity_confidence={"alpha": 0.95, "beta": 0.95},
        entity_evidence_refs={
            "alpha": ["frame://alpha"],
            "beta": ["frame://beta"],
        },
    )
    inspection_forward = verify_inspection_evidence(entity_state, "task-inspection", ["alpha", "beta"], context=context)
    inspection_reverse = verify_inspection_evidence(entity_state, "task-inspection", ["beta", "alpha"], context=context)
    kit_forward = verify_kit_contents(entity_state, "task-kit", ["alpha", "beta"], context=context)
    kit_reverse = verify_kit_contents(entity_state, "task-kit", ["beta", "alpha"], context=context)

    assert inspection_forward.verification_id == inspection_reverse.verification_id
    assert kit_forward.verification_id == kit_reverse.verification_id

    empty_state = WorldState(run_id="run-request-order")
    sorting_first = verify_parcel_sorting(
        empty_state,
        "task-sort",
        parcel_routes={"parcel-b": "bin-b", "parcel-a": "bin-a"},
        expected_attributes={
            "parcel-b": {"condition": "intact", "label_status": "verified"},
            "parcel-a": {"condition": "intact", "label_status": "verified"},
        },
        context=context,
    )
    sorting_second = verify_parcel_sorting(
        empty_state,
        "task-sort",
        parcel_routes={"parcel-a": "bin-a", "parcel-b": "bin-b"},
        expected_attributes={
            "parcel-a": {"label_status": "verified", "condition": "intact"},
            "parcel-b": {"label_status": "verified", "condition": "intact"},
        },
        context=context,
    )
    policy_forward = verify_parcel_policy(
        empty_state,
        "task-policy",
        ["parcel-a", "parcel-b"],
        context=context,
    )
    policy_reverse = verify_parcel_policy(
        empty_state,
        "task-policy",
        ["parcel-b", "parcel-a"],
        context=context,
    )

    assert sorting_first.verification_id == sorting_second.verification_id
    assert policy_forward.verification_id == policy_reverse.verification_id


def test_public_verifiers_pass_complete_structured_request_semantics() -> None:
    state = WorldState(run_id="run-structured-request")
    context = request_context()
    original = verifier_module._verification_id
    with patch.object(verifier_module, "_verification_id", wraps=original) as identity:
        verify_object_in_tray(state, "task-object", "alpha", "tray-a", context=context)
        verify_kit_contents(
            state,
            "task-kit",
            ["beta", "alpha"],
            tray_id="kit-tray",
            confidence_threshold=0.85,
            context=context,
        )
        verify_inspection_evidence(
            state,
            "task-inspection",
            ["beta", "alpha"],
            confidence_threshold=0.85,
            context=context,
        )
        verify_workspace_clearance(state, "task-clear", context=context)
        verify_parcel_sorting(
            state,
            "task-sort",
            parcel_routes={"parcel-b": "bin-b", "parcel-a": "bin-a"},
            expected_attributes={
                "parcel-b": {"condition": "damaged", "label_status": "verified"},
                "parcel-a": {"condition": "intact", "label_status": "verified"},
            },
            confidence_threshold=0.85,
            context=context,
        )
        verify_parcel_policy(
            state,
            "task-policy",
            ["parcel-a"],
            pickup_shelf_id="pickup-custom",
            quarantine_bin_id="quarantine-custom",
            confidence_threshold=0.85,
            parcel_manifest={
                "parcel-a": {
                    "tracking_id": " TRACK-001 ",
                    "barcode": "BAR-001",
                }
            },
            manifest_id=" manifest-1 ",
            context=context,
        )

    requests = [(call.kwargs["verifier_kind"], call.kwargs["request_semantics"]) for call in identity.call_args_list]
    assert requests == [
        ("object_in_tray", {"object_id": "alpha", "tray_id": "tray-a"}),
        (
            "kit_contents",
            {
                "confidence_threshold": 0.85,
                "required_object_ids": ["alpha", "beta"],
                "tray_id": "kit-tray",
            },
        ),
        (
            "inspection_evidence",
            {
                "confidence_threshold": 0.85,
                "required_entity_ids": ["alpha", "beta"],
            },
        ),
        ("workspace_clearance", {}),
        (
            "parcel_sorting",
            {
                "confidence_threshold": 0.85,
                "expected_attributes": {
                    "parcel-a": {"condition": "intact", "label_status": "verified"},
                    "parcel-b": {"condition": "damaged", "label_status": "verified"},
                },
                "parcel_routes": {"parcel-a": "bin-a", "parcel-b": "bin-b"},
            },
        ),
        (
            "parcel_policy",
            {
                "confidence_threshold": 0.85,
                "manifest_id": "manifest-1",
                "parcel_ids": ["parcel-a"],
                "parcel_manifest": {
                    "parcel-a": {"barcode": "bar001", "tracking_id": "track001"},
                },
                "pickup_shelf_id": "pickup-custom",
                "quarantine_bin_id": "quarantine-custom",
            },
        ),
    ]


def test_public_verifier_state_hash_rule_and_ordered_evidence_affect_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def inspection_state(evidence_refs: list[str]) -> WorldState:
        return WorldState(
            run_id="run-public-identity-material",
            entity_locations={"alpha": "on:table"},
            entity_confidence={"alpha": 0.95},
            entity_evidence_refs={"alpha": evidence_refs},
        )

    state = inspection_state(["frame://alpha", "frame://beta"])
    baseline = verify_inspection_evidence(state, "task-inspection", ["alpha"], context=request_context())
    state_hash_changed = verify_inspection_evidence(
        state,
        "task-inspection",
        ["alpha"],
        context=request_context(state_hash="e" * 64),
    )
    evidence_changed = verify_inspection_evidence(
        inspection_state(["frame://alpha", "frame://gamma"]),
        "task-inspection",
        ["alpha"],
        context=request_context(),
    )
    evidence_reordered = verify_inspection_evidence(
        inspection_state(["frame://beta", "frame://alpha"]),
        "task-inspection",
        ["alpha"],
        context=request_context(),
    )
    monkeypatch.setattr(verifier_module, "INSPECTION_EVIDENCE_RULE_VERSION", "inspection-evidence-v2")
    rule_changed = verify_inspection_evidence(state, "task-inspection", ["alpha"], context=request_context())

    identifiers = {
        baseline.verification_id,
        state_hash_changed.verification_id,
        evidence_changed.verification_id,
        evidence_reordered.verification_id,
        rule_changed.verification_id,
    }
    assert len(identifiers) == 5


def test_evidence_collection_order_is_deterministic_and_enters_identity() -> None:
    state_a = WorldState(
        run_id="run-evidence-order",
        entity_locations={"zeta": "on:table", "alpha": "on:table"},
        entity_confidence={"zeta": 0.95, "alpha": 0.95},
        entity_evidence_refs={
            "zeta": ["frame://zeta", "frame://shared"],
            "alpha": ["frame://shared", "frame://alpha"],
        },
    )
    state_b = WorldState(
        run_id="run-evidence-order",
        entity_locations={"alpha": "on:table", "zeta": "on:table"},
        entity_confidence={"alpha": 0.95, "zeta": 0.95},
        entity_evidence_refs={
            "alpha": ["frame://shared", "frame://alpha"],
            "zeta": ["frame://zeta", "frame://shared"],
        },
    )
    context = VerificationContext(
        state_hash="c" * 64,
        verified_at="2026-08-27T12:34:56Z",
        clock_id="wall",
    )

    results = [
        verify_inspection_evidence(
            state,
            "task-inspection",
            ["zeta", "alpha"],
            context=context,
        )
        for state in (state_a, state_b)
    ]

    assert [result.evidence_refs for result in results] == [
        ["frame://shared", "frame://alpha", "frame://zeta"],
        ["frame://shared", "frame://alpha", "frame://zeta"],
    ]
    assert results[0].verification_id == results[1].verification_id


@pytest.mark.parametrize(
    "state_hash",
    [
        "",
        " " * 64,
        "A" * 64,
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "0x" + "a" * 62,
        7,
        None,
    ],
)
def test_malformed_state_hash_fails_closed(state_hash: object) -> None:
    with pytest.raises(ValueError):
        VerificationContext(
            state_hash=state_hash,
            verified_at="2026-08-27T12:34:56Z",
            clock_id="wall",
        )


@pytest.mark.parametrize(
    "verified_at",
    [
        "",
        "not-a-timestamp",
        "2026-08-27T12:34:56",
        "2026-08-27 12:34:56Z",
        "2026-08-27T12:34:56+01:00",
        "2026-08-27T12:34:56-04:00",
        "2026-02-30T12:34:56Z",
        "2026-08-27T12:34Z",
        7,
        None,
    ],
)
def test_malformed_or_non_utc_wall_time_fails_closed(verified_at: object) -> None:
    with pytest.raises(ValueError):
        VerificationContext(
            state_hash="a" * 64,
            verified_at=verified_at,
            clock_id="wall",
        )


def test_monotonic_clock_cannot_label_verified_at() -> None:
    with pytest.raises(ValueError):
        VerificationContext(
            state_hash="a" * 64,
            verified_at="2026-08-27T12:34:56Z",
            clock_id="monotonic",
        )


def test_verifier_requires_an_explicit_verification_context() -> None:
    public_verifiers = (
        verify_object_in_tray,
        verify_kit_contents,
        verify_inspection_evidence,
        verify_workspace_clearance,
        verify_parcel_sorting,
        verify_parcel_policy,
    )
    for verifier in public_verifiers:
        parameter = inspect.signature(verifier).parameters.get("context")
        assert parameter is not None
        assert parameter.default is inspect.Parameter.empty

    state, _ = verification_inputs()
    with pytest.raises(TypeError):
        verify_object_in_tray(state, "task-place", "red_block", "tray")
