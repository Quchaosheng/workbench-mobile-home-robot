"""Issue #410: a bounded, deterministic proposal for the next observation.

The retry used to be generic: the same entity went back to the same observer, so
every attempt could acquire the same uninformative view. These tests pin the
ranking, the refusals, and the record of whether the new sample changed anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "services" / "agent_runtime") not in sys.path:
    sys.path.append(str(ROOT / "services" / "agent_runtime"))

from workbench_agent_runtime.next_observation import (
    GAP_BY_REASON,
    MAX_PROPOSALS,
    NextObservationError,
    propose_next_observations,
    verdict_changed,
)


def test_insufficient_evidence_proposes_ranked_views() -> None:
    proposals = propose_next_observations("verification_insufficient", "red_block")
    assert [proposal.rank for proposal in proposals] == list(range(len(proposals)))
    assert proposals[0].view == "closer-view"
    assert all(proposal.entity_id == "red_block" for proposal in proposals)


def test_the_proposal_states_the_gap_it_closes() -> None:
    proposal = propose_next_observations("observation_stale", "red_block")[0]
    assert proposal.gap == GAP_BY_REASON["observation_stale"]
    assert "predates" in proposal.expected_resolution


def test_a_view_already_tried_is_not_proposed_again() -> None:
    """Repeating the same request is the defect the issue names."""

    first = propose_next_observations("verification_insufficient", "red_block")
    tried = [first[0].view]
    second = propose_next_observations("verification_insufficient", "red_block", tried_views=tried)
    assert first[0].view not in [proposal.view for proposal in second]
    assert second[0].previously_tried == tuple(sorted(tried))


def test_a_reason_with_no_gap_proposes_nothing() -> None:
    """An empty list means do not spend an attempt, not ask again the same way."""

    assert propose_next_observations("post_action_confirmed", "red_block") == []
    assert propose_next_observations("dependency_blocked", "red_block") == []


def test_every_proposed_reason_names_a_gap() -> None:
    for reason in GAP_BY_REASON:
        proposals = propose_next_observations(reason, "red_block")
        assert proposals, f"{reason} names a gap but proposed nothing"
        assert len(proposals) <= MAX_PROPOSALS


def test_the_proposal_set_is_bounded() -> None:
    proposals = propose_next_observations("verification_insufficient", "red_block", limit=1)
    assert len(proposals) == 1


def test_an_enum_reason_is_accepted() -> None:
    class _Reason:
        value = "observation_stale"

    proposals = propose_next_observations(_Reason(), "red_block")
    assert proposals[0].reason_code == "observation_stale"


def test_an_empty_reason_is_refused() -> None:
    with pytest.raises(NextObservationError, match="reason code"):
        propose_next_observations("", "red_block")


def test_an_empty_entity_is_refused() -> None:
    with pytest.raises(NextObservationError, match="entity_id"):
        propose_next_observations("observation_stale", "   ")


def test_an_invalid_limit_is_refused() -> None:
    with pytest.raises(NextObservationError, match="limit"):
        propose_next_observations("observation_stale", "red_block", limit=0)


def test_verdict_changed_reports_whether_the_sample_mattered() -> None:
    assert verdict_changed("refuted", "confirmed") is True
    assert verdict_changed("refuted", "refuted") is False


def test_verdict_changed_accepts_a_status_enum() -> None:
    class _Status:
        def __init__(self, value: str) -> None:
            self.value = value

    assert verdict_changed(_Status("insufficient_evidence"), _Status("confirmed")) is True
    assert verdict_changed(_Status("refuted"), _Status("refuted")) is False
