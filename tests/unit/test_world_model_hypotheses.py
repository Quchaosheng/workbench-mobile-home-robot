"""Issue #409: conflicting observations are represented, not collapsed.

The contract promised `insufficient_evidence` when an entity was observed but is
no longer certain, and described bounded multi-hypothesis tracking. The reducer
overwrote the location with the newest accepted observation, so a second
contradictory sample silently replaced the first and the verifier reported
`refuted` as if nothing had disagreed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for entry in (
    ROOT / "libs" / "kernel",
    ROOT / "libs" / "application",
    ROOT / "libs" / "contracts",
    ROOT / "services" / "world_model",
    ROOT / "libs" / "hardware",
):
    if str(entry) not in sys.path:
        sys.path.append(str(entry))

from workbench_contracts.models import ReasonCode, VerificationStatus
from workbench_world_model.reducer import MAX_LOCATION_HYPOTHESES, WorldState, _update_location
from workbench_world_model.verifier import verify_object_in_tray


def context():
    from workbench_world_model import VerificationContext

    # Verification metadata is injected at the boundary, never invented here.
    return VerificationContext(
        state_hash="a" * 64,
        verified_at="2026-08-27T12:34:56Z",
        clock_id="wall",
    )


def state_with(*locations: tuple) -> WorldState:
    state = WorldState(run_id="run-1")
    for entry in locations:
        location, refs = entry[0], entry[1]
        observed_at = entry[2] if len(entry) > 2 else None
        _update_location(state, "red_block", location, refs, observed_at)
    return state


def test_two_conflicting_observations_are_both_retained() -> None:
    state = state_with(("on:table", ["frame-0"]), ("in:tray", ["frame-1"]))
    assert state.entity_locations["red_block"] == "in:tray"
    assert state.entity_location_hypotheses["red_block"] == ["in:tray", "on:table"]


def test_the_hypothesis_set_is_bounded() -> None:
    state = state_with(("on:table", ["a"]), ("in:tray", ["b"]), ("on:shelf", ["c"]))
    hypotheses = state.entity_location_hypotheses["red_block"]
    assert len(hypotheses) == MAX_LOCATION_HYPOTHESES
    assert hypotheses[0] == "on:shelf", "the newest reading is kept first"


def test_a_conflict_yields_insufficient_evidence_not_refuted() -> None:
    """One sample says the goal holds and another says it does not."""

    state = state_with(("on:table", ["frame-0"]), ("in:tray", ["frame-1"]))
    result = verify_object_in_tray(state, "task-1", "red_block", "tray", context=context())
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code is ReasonCode.EVIDENCE_CONFLICTING
    # Both readings are named, so a reviewer can see which two samples disagree.
    assert "on:table" in result.claim and "in:tray" in result.claim


def test_an_undisputed_observation_still_confirms() -> None:
    state = state_with(("in:tray", ["frame-0"]))
    result = verify_object_in_tray(state, "task-1", "red_block", "tray", context=context())
    assert result.status is VerificationStatus.CONFIRMED
    assert result.reason_code is ReasonCode.GOAL_SATISFIED


def test_an_undisputed_absence_still_refutes() -> None:
    state = state_with(("on:table", ["frame-0"]))
    result = verify_object_in_tray(state, "task-1", "red_block", "tray", context=context())
    assert result.status is VerificationStatus.REFUTED
    assert result.reason_code is ReasonCode.GOAL_NOT_SATISFIED


def test_a_conflict_elsewhere_does_not_block_a_confirmed_tray() -> None:
    """The conflict only matters when the expected location is one of the claims."""

    state = state_with(("on:table", ["a"]), ("on:shelf", ["b"]), ("in:tray", ["c"]))
    # Hypotheses are bounded to two, and the newest two are shelf and tray.
    result = verify_object_in_tray(state, "task-1", "red_block", "tray", context=context())
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code is ReasonCode.EVIDENCE_CONFLICTING


def test_conflicting_and_missing_are_distinct_reasons() -> None:
    """Nothing arrived is not the same finding as two things disagreeing."""

    never = WorldState(run_id="run-1")
    missing = verify_object_in_tray(never, "task-1", "red_block", "tray", context=context())
    assert missing.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert missing.reason_code is ReasonCode.TARGET_NOT_OBSERVED
    assert missing.reason_code is not ReasonCode.EVIDENCE_CONFLICTING


def test_moving_an_entity_is_not_a_conflict() -> None:
    """A successful placement is a state change, not two beliefs at once.

    The first version treated every previous location as a competing claim, so a
    block that was on the table and is now in the tray came back unresolved.
    """

    state = state_with(
        ("on:table", ["frame-0"], "2026-08-31T11:00:01Z"),
        ("in:tray", ["frame-1"], "2026-08-31T11:00:04Z"),
    )
    assert state.entity_locations["red_block"] == "in:tray"
    assert state.entity_location_hypotheses["red_block"] == ["in:tray"]
    result = verify_object_in_tray(state, "task-1", "red_block", "tray", context=context())
    assert result.status is VerificationStatus.CONFIRMED


def test_two_readings_at_the_same_instant_do_conflict() -> None:
    state = state_with(
        ("in:tray", ["cam-a"], "2026-08-31T11:00:04Z"),
        ("on:table", ["cam-b"], "2026-08-31T11:00:04Z"),
    )
    assert state.entity_location_hypotheses["red_block"] == ["on:table", "in:tray"]
    result = verify_object_in_tray(state, "task-1", "red_block", "tray", context=context())
    assert result.status is VerificationStatus.INSUFFICIENT_EVIDENCE
    assert result.reason_code is ReasonCode.EVIDENCE_CONFLICTING


def test_readings_without_timestamps_cannot_be_ordered_and_conflict() -> None:
    """Absent a timestamp neither reading can be preferred, so neither is."""

    state = state_with(("in:tray", ["a"]), ("on:table", ["b"]))
    assert len(state.entity_location_hypotheses["red_block"]) == 2
