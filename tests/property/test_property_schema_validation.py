"""Property suite: strict payload schema validation (Issue #89).

The boundary under test is ``workbench_world_model.event_payloads``.  It promises
that a state-affecting payload is accepted only when every field has the declared
JSON type and a finite in-range value, and that the accepted value is normalized
so two spellings of the same fact cannot produce two different states.

The generator emits both halves of each rule: values just inside the boundary
(which must be accepted) and values just outside it (which must be rejected with
a typed error rather than a bare ``ValueError`` from somewhere deeper).
"""

from __future__ import annotations

import math

import pytest
from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model.event_payloads import (
    WorldEventPayloadValidationError,
    normalize_world_event,
)

from ._generator import SEEDS, Rng, assert_corpus_clean, report_for, run_corpus, write_archive_if_requested

SUITE = "schema_validation"
RUN_ID = "run-property-schema"
MIN_CASES = 3 * 140

ACCEPTED_CONFIDENCES = (0.0, 0.5, 1.0)
REJECTED_CONFIDENCES = (-0.001, 1.001, -1.0, 2.0, float("nan"), float("inf"), float("-inf"), True, "0.5", None)


def event(payload: dict) -> WorldEvent:
    return WorldEvent(
        event_id=f"evt-{SUITE}",
        run_id=RUN_ID,
        sequence_no=0,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-17T00:00:00Z",
        payload=payload,
        evidence_refs=["camera-frame-property"],
    )


def cases() -> list[dict]:
    generated: list[dict] = []
    for seed in SEEDS:
        rng = Rng(seed)
        for _ in range(140):
            confidence = rng.choice(ACCEPTED_CONFIDENCES)
            generated.append(
                {
                    "kind": "accepted",
                    "confidence": confidence,
                    "entity_id": rng.choice(("red_block", "blue-block", "tray.1")),
                    "location": rng.choice(("on:table", "in:tray")),
                }
            )
            rejected = rng.choice(REJECTED_CONFIDENCES)
            generated.append({"kind": "rejected_confidence", "confidence": rejected})
    return generated


def check(case: dict) -> None:
    if case["kind"] == "accepted":
        normalized = normalize_world_event(
            event(
                {
                    "entity_id": case["entity_id"],
                    "entity_type": "block",
                    "location": case["location"],
                    "confidence": case["confidence"],
                }
            )
        )
        payload = normalized.payload
        assert payload["confidence"] == float(case["confidence"])
        assert 0.0 <= payload["confidence"] <= 1.0
        assert payload["entity_id"] == case["entity_id"]
        assert payload["location"] == case["location"]
        # Normalization is idempotent, so re-validating an accepted event cannot
        # produce a second spelling of the same fact.
        assert normalize_world_event(normalized) == normalized
        return

    confidence = case["confidence"]
    with pytest.raises(WorldEventPayloadValidationError):
        normalize_world_event(event({"entity_id": "red_block", "confidence": confidence}))


def test_generated_payloads_are_validated_against_the_declared_range() -> None:
    report = run_corpus(SUITE, cases(), check)
    assert_corpus_clean(report)
    assert report.case_count >= MIN_CASES, f"corpus shrank to {report.case_count} cases"
    assert report.corpus_digest


def test_the_shrunk_counterexample_is_the_smallest_rejection() -> None:
    from ._generator import run_case

    def always_fails(case: object) -> None:
        raise AssertionError("synthetic failure for shrink evidence")

    outcome = run_case(
        {"entity_id": "red_block", "confidence": 0.5, "evidence_refs": ["a", "b", "c"], "extra": {"deep": [1, 2]}},
        always_fails,
    )

    assert outcome.ok is False
    assert outcome.shrunk == {}
    assert outcome.shrink_steps > 0
    assert outcome.as_dict()["case_id"]


def test_nan_and_infinity_are_rejected_rather_than_coerced() -> None:
    for confidence in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(WorldEventPayloadValidationError):
            normalize_world_event(event({"entity_id": "red_block", "confidence": confidence}))


def test_boolean_and_string_confidences_are_not_numeric() -> None:
    for confidence in (True, False, "0.5"):
        with pytest.raises(WorldEventPayloadValidationError, match="JSON number"):
            normalize_world_event(event({"entity_id": "red_block", "confidence": confidence}))


def test_blank_and_non_string_entity_ids_are_rejected() -> None:
    for entity_id in ("", "   ", 7, None):
        with pytest.raises(WorldEventPayloadValidationError, match="entity_id"):
            normalize_world_event(event({"entity_id": entity_id, "confidence": 0.5}))


def test_the_boundary_values_themselves_are_accepted() -> None:
    for confidence in (0.0, 1.0):
        normalized = normalize_world_event(event({"entity_id": "red_block", "confidence": confidence}))
        assert normalized.payload["confidence"] == confidence
    assert not math.isnan(normalize_world_event(event({"entity_id": "b", "confidence": 0})).payload["confidence"])


def test_the_corpus_is_reproducible_and_pinned() -> None:
    first = cases()
    second = cases()
    assert [case["confidence"] for case in first] == [case["confidence"] for case in second]
    assert report_for(SUITE).seeds == SEEDS


def test_a_failing_case_reports_its_shrunk_counterexample() -> None:
    from ._generator import run_case

    def rejects_zero_only(case: dict) -> None:
        assert case["confidence"] != 0.0, "zero confidence is not accepted by this synthetic rule"

    outcome = run_case({"confidence": 0.0, "noise": [1, 2, 3], "entity": "red_block"}, rejects_zero_only)

    assert outcome.ok is False
    assert outcome.shrunk == {"confidence": 0.0}
    assert "zero confidence" in (outcome.detail or "")


def test_the_corpus_digest_changes_when_a_case_changes() -> None:
    write_archive_if_requested()
    from ._generator import digest

    assert digest([{"a": 1}]) != digest([{"a": 2}])
    assert digest([{"a": 1}]) == digest([{"a": 1}])
