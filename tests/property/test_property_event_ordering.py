"""Property suite: event ordering, identity and idempotence (Issue #89).

The boundary under test is ``workbench_world_model.reducer``.  It promises that a
validated stream folds by ``sequence_no`` regardless of the order the caller
supplied, that an exact duplicate is folded rather than applied twice, and that a
stream which breaks either promise is rejected instead of silently reordered.
"""

from __future__ import annotations

import pytest
from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model.reducer import WorldState, apply_event, reduce_events

from ._generator import SEEDS, Rng, assert_corpus_clean, digest, report_for, run_corpus, write_archive_if_requested

SUITE = "event_ordering"
RUN_ID = "run-property-ordering"
ENTITY_IDS = ("red_block", "blue_block", "tray", "shelf_a")
LOCATIONS = ("on:table", "in:tray", "on:shelf_a", "held_by:arm")

MIN_CASES = 3 * 120


def typed_observation(sequence_no: int, *, entity_id: str, entity_type: str, suffix: str) -> WorldEvent:
    return WorldEvent(
        event_id=f"evt-{SUITE}-{suffix}",
        run_id=RUN_ID,
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at=f"2026-08-17T00:01:{sequence_no:02d}Z",
        payload={
            "entity_id": entity_id,
            "entity_type": entity_type,
            "location": "on:table",
            "confidence": 0.5,
        },
        evidence_refs=[f"camera-frame-{suffix}"],
    )


def observation(sequence_no: int, *, entity_id: str, location: str, confidence: float, suffix: str) -> WorldEvent:
    return WorldEvent(
        event_id=f"evt-{SUITE}-{suffix}",
        run_id=RUN_ID,
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at=f"2026-08-17T00:00:{sequence_no:02d}Z",
        payload={
            "entity_id": entity_id,
            "entity_type": "block",
            "location": location,
            "confidence": confidence,
        },
        evidence_refs=[f"camera-frame-{suffix}"],
    )


def ordered_stream(rng: Rng, length: int) -> list[WorldEvent]:
    events: list[WorldEvent] = []
    for index in range(length):
        events.append(
            observation(
                index,
                entity_id=rng.choice(ENTITY_IDS),
                location=rng.choice(LOCATIONS),
                confidence=rng.integer(0, 1000) / 1000.0,
                suffix=f"{rng.next_u64():016x}",
            )
        )
    return events


def shuffled(rng: Rng, events: list[WorldEvent]) -> list[WorldEvent]:
    pool = list(events)
    rng.shuffle(pool)
    return pool


def cases() -> list[dict]:
    generated: list[dict] = []
    for seed in SEEDS:
        rng = Rng(seed)
        for _ in range(120):
            length = rng.integer(1, 6)
            generated.append({"seed": seed, "stream": ordered_stream(rng, length), "length": length})

            # The same entity_id must not change its entity_type mid-run; that is
            # a rejection the reducer owns and a generator must exercise it.
            entity_id = rng.choice(ENTITY_IDS)
            conflicting = [
                typed_observation(0, entity_id=entity_id, entity_type="block", suffix=f"type-a-{rng.next_u64():x}"),
                typed_observation(1, entity_id=entity_id, entity_type="tray", suffix=f"type-b-{rng.next_u64():x}"),
            ]
            generated.append({"seed": seed, "stream": conflicting, "length": 2, "conflict": True})
    return generated


def check(case: dict) -> None:
    events = case["stream"]
    rng = Rng(case["seed"] ^ case["length"])

    if case.get("conflict"):
        with pytest.raises(ValueError, match="conflicting entity_type"):
            reduce_events(RUN_ID, events)
        # The conflict must be detected from the stream itself, so it survives a
        # reordering of the same events.
        with pytest.raises(ValueError, match="conflicting entity_type"):
            reduce_events(RUN_ID, list(reversed(events)))
        return

    baseline = reduce_events(RUN_ID, events)

    # 1. Any permutation of one valid stream reduces to the same state.
    for _ in range(3):
        assert reduce_events(RUN_ID, shuffled(rng, events)) == baseline

    # 2. Re-appending the whole stream is a no-op: exact duplicates fold.
    assert reduce_events(RUN_ID, [*events, *events]) == baseline

    # 3. A stream reversed is still ordered by sequence_no, not by position.
    assert reduce_events(RUN_ID, list(reversed(events))) == baseline

    # 4. The reduced state records exactly the applied event ids, in order.
    assert baseline.applied_event_ids == [event.event_id for event in events]


def test_generated_streams_reduce_independently_of_input_order() -> None:
    report = run_corpus(SUITE, cases(), check)
    assert_corpus_clean(report)
    assert report.case_count >= MIN_CASES, f"corpus shrank to {report.case_count} cases"
    assert report.corpus_digest


# The rejection boundary is exercised with hand-written cases on purpose: a
# generated stream cannot express "this is the same event_id with different
# content" without pinning both halves of the conflict.


def test_conflicting_event_id_content_is_rejected() -> None:
    first = observation(0, entity_id="red_block", location="on:table", confidence=0.9, suffix="conflict")
    second = observation(
        0, entity_id="red_block", location="in:tray", confidence=0.9, suffix="conflict-conflict"
    ).model_copy(update={"event_id": first.event_id})

    with pytest.raises(ValueError, match="duplicated with different complete content"):
        reduce_events(RUN_ID, [first, second])


def test_shared_sequence_between_two_event_ids_is_rejected() -> None:
    first = observation(0, entity_id="red_block", location="on:table", confidence=0.9, suffix="seq-a")
    second = observation(0, entity_id="blue_block", location="in:tray", confidence=0.9, suffix="seq-b")

    with pytest.raises(ValueError, match="is shared by event_id"):
        reduce_events(RUN_ID, [first, second])


def test_apply_event_refuses_an_entity_type_change() -> None:
    """The fold itself, not only stream preflight, must refuse the conflict.

    ``reduce_events`` validates the whole stream up front, so a mutation of the
    per-event guard in ``apply_event`` would stay invisible if the suite only
    drove ``reduce_events``.  ``apply_event`` is public API and is exercised on
    its own here.
    """

    state = WorldState(run_id=RUN_ID)
    first = typed_observation(0, entity_id="red_block", entity_type="block", suffix="apply-type-a")
    state = apply_event(state, first)
    assert state.entity_types["red_block"] == "block"

    second = typed_observation(1, entity_id="red_block", entity_type="tray", suffix="apply-type-b")
    with pytest.raises(ValueError, match="conflicting entity_type"):
        apply_event(state, second)

    # Re-applying an already-applied event is idempotent and does not re-check.
    assert apply_event(state, first) == state


def test_apply_event_is_idempotent_for_one_event() -> None:
    event = typed_observation(0, entity_id="tray", entity_type="tray", suffix="apply-idempotent")
    state = WorldState(run_id=RUN_ID)
    once = apply_event(state, event)
    twice = apply_event(once, event)

    assert once == twice
    assert twice.applied_event_ids == [event.event_id]
    assert once.evidence_refs == event.evidence_refs == twice.evidence_refs


def test_a_foreign_run_id_is_rejected() -> None:
    event = observation(0, entity_id="red_block", location="on:table", confidence=0.9, suffix="foreign")

    with pytest.raises(ValueError, match="does not match requested run_id"):
        reduce_events("run-somewhere-else", [event])


def test_a_non_event_member_is_rejected() -> None:
    event = observation(0, entity_id="red_block", location="on:table", confidence=0.9, suffix="not-an-event")

    with pytest.raises(TypeError, match="WorldEvent values"):
        reduce_events(RUN_ID, [event, {"event_id": "evt-forged"}])


def test_the_corpus_is_reproducible_for_a_fixed_seed() -> None:
    write_archive_if_requested()
    first = cases()
    second = cases()

    assert len(first) == len(second)
    assert [case["length"] for case in first] == [case["length"] for case in second]
    assert [case["stream"] for case in first] == [case["stream"] for case in second]
    assert report_for(SUITE).seeds == SEEDS
    # The recorded corpus digest must be the digest of the corpus this run
    # actually generated, so a silently smaller corpus changes the artifact.
    assert report_for(SUITE).corpus_digest == digest(first)

    # The stream itself is pinned, so a change to the generator is a visible
    # corpus change in review rather than a silent reshuffle of every case.
    rng = Rng(SEEDS[0])
    numbers = [rng.next_u64() for _ in range(4)]
    assert numbers == [0x37973F7FDA16255C, 0xC01441EB386FBB37, 0x0C6DA505D4170E24, 0xEFA4F61DF50D0D38]


def test_shrink_prefers_the_smallest_failing_case() -> None:
    from ._generator import shrink

    failing = {"entity_id": "red_block", "location": "in:tray", "confidence": 0.5, "extra": [1, 2, 3]}

    def still_fails(candidate: object) -> bool:
        return isinstance(candidate, dict) and candidate.get("location") == "in:tray"

    minimal, steps = shrink(failing, still_fails)

    assert minimal == {"location": "in:tray"}
    assert steps > 0
