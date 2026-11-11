"""Turn a verification reason into a ranked request for the next observation.

`PostActionLoop` retried observation within a fixed budget, but the retry was
generic: the same `entity_id` went back to the same observer, with no reason-
specific target and no notion of which view would settle the question. That is
repetition with a budget, not active perception — every attempt could acquire the
same uninformative view (issue #410).

This module derives a proposal from the reason code and the verifier's recorded
evidence. It is deterministic: the same reason and the same evidence always
produce the same proposal, so a reviewer can predict what the loop will try next.

It proposes; it does not observe. Nothing here changes an observer's signature or
claims a physical viewpoint exists.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

NEXT_OBSERVATION_VERSION = "next-observation-proposal-v1"

# The gap each reason names, and the view that would close it. A reason without
# an entry has no proposal: guessing one would spend an attempt on a view nobody
# asked for.
GAP_BY_REASON: dict[str, str] = {
    "verification_insufficient": "the relation was not resolvable from the sample",
    "observation_stale": "the sample predates the action it is meant to explain",
    "observation_missing": "no sample reached the loop",
    "observation_timeout": "the observer did not answer in time",
    "perception_failure": "the observer could not produce a sample",
    "observation_wrong_entity": "the sample described a different entity",
    "observation_wrong_action": "the sample answered a different action",
    "observation_wrong_run": "the sample belongs to a different run",
    "observation_clock_mismatch": "the sample is on a different clock",
    "verification_refuted": "the sample contradicted the claim",
}

# The ranked views to ask for, per gap. Earlier entries are cheaper or more
# likely to settle the question; the list is bounded so an attempt is always
# spent on one of a known few.
VIEWS_BY_REASON: dict[str, tuple[str, ...]] = {
    "verification_insufficient": ("closer-view", "alternate-angle", "repeat-view"),
    "observation_stale": ("fresh-view",),
    "observation_missing": ("repeat-view",),
    "observation_timeout": ("repeat-view", "alternate-angle"),
    "perception_failure": ("repeat-view",),
    "observation_wrong_entity": ("wider-view",),
    "observation_wrong_action": ("repeat-view",),
    "observation_wrong_run": ("repeat-view",),
    "observation_clock_mismatch": ("repeat-view",),
    "verification_refuted": ("closer-view", "alternate-angle"),
}

# A proposal is deterministic, so a view already tried for this reason is skipped
# rather than repeated: repeating it is what the issue calls out as the defect.
MAX_PROPOSALS = 3


class NextObservationError(ValueError):
    """A proposal request that cannot be answered as stated."""


@dataclass(frozen=True)
class ObservationProposal:
    """One ranked request the loop may make, and what it expects to learn."""

    version: str
    reason_code: str
    gap: str
    rank: int
    view: str
    entity_id: str
    expected_resolution: str
    previously_tried: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "reason_code": self.reason_code,
            "gap": self.gap,
            "rank": self.rank,
            "view": self.view,
            "entity_id": self.entity_id,
            "expected_resolution": self.expected_resolution,
            "previously_tried": list(self.previously_tried),
        }


def _reason_text(value: object) -> str:
    if hasattr(value, "value"):
        value = value.value
    if not isinstance(value, str) or not value:
        raise NextObservationError("a reason code must be a non-empty string")
    return value


def propose_next_observations(
    reason_code: object,
    entity_id: str,
    *,
    tried_views: Iterable[str] = (),
    limit: int = MAX_PROPOSALS,
) -> list[ObservationProposal]:
    """Rank the views that could close the gap this reason names.

    Returns an empty list when the reason names no gap — a confirmed verdict, or
    a code this module was not given a view for. An empty list means "do not
    spend an attempt", which is different from "ask again the same way".
    """
    reason = _reason_text(reason_code)
    if not isinstance(entity_id, str) or not entity_id.strip():
        raise NextObservationError("an entity_id must be a non-empty string")
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise NextObservationError("limit must be a positive integer")
    gap = GAP_BY_REASON.get(reason)
    if gap is None:
        return []
    tried = tuple(sorted({str(view) for view in tried_views}))
    views = [view for view in VIEWS_BY_REASON.get(reason, ()) if view not in tried][:limit]
    return [
        ObservationProposal(
            version=NEXT_OBSERVATION_VERSION,
            reason_code=reason,
            gap=gap,
            rank=index,
            view=view,
            entity_id=entity_id.strip(),
            # Stated per proposal so the record answers "what did this attempt
            # expect to learn" after the fact.
            expected_resolution=gap,
            previously_tried=tried,
        )
        for index, view in enumerate(views)
    ]


def verdict_changed(before: object, after: object) -> bool:
    """Whether a new sample moved the verdict.

    The issue asks the loop to record this: a retry that changes nothing is
    evidence that the proposed view was not informative, and that is worth
    knowing before the budget is spent again.
    """

    def _text(value: object) -> str:
        return str(getattr(value, "value", value))

    return _text(before) != _text(after)


__all__ = [
    "GAP_BY_REASON",
    "MAX_PROPOSALS",
    "NEXT_OBSERVATION_VERSION",
    "VIEWS_BY_REASON",
    "NextObservationError",
    "ObservationProposal",
    "propose_next_observations",
    "verdict_changed",
]
