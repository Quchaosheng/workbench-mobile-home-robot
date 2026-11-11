"""Per-run model-route evidence, bounded and derived from what actually happened.

The policy declares route tiers, timeouts, cost recording and fallbacks
(`docs/context/MODEL_POLICY.yaml`), and the playbook lists route, latency,
fallback, actual-cost and shadow-cost as P0 trace fields. Nothing bound those to
a run: `TaskGraph.model_route` was a free-form string, so a model response could
be mistaken for a plan routed under the committed policy (issue #414).

This module records the answer to five questions, and refuses to invent any of
them:

- which route was selected
- whether a fallback happened, and why
- what the call actually cost
- what the shadow cost would have been
- whether the local-resource evidence is complete

It is a record, not a control surface: it does not choose a route, retry, or
approve a provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

MODEL_ROUTE_EVIDENCE_VERSION = "model-route-evidence-v1"

# The closed vocabulary a record may use for the fallback reason. "unknown" is
# present on purpose: a record that cannot name a reason must say so rather than
# omit the field and look like a clean route.
FALLBACK_REASONS = (
    "none",
    "timeout",
    "unavailable",
    "refused",
    "budget",
    "policy",
    "unknown",
)

# What a reviewer may conclude about the local-resource evidence.
EVIDENCE_STATES = ("complete", "partial", "missing", "unknown")


class ModelRouteEvidenceError(ValueError):
    """A route evidence record that cannot be trusted."""


def _bounded_number(value: object, label: str, *, maximum: float = 1e9) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelRouteEvidenceError(f"{label} must be a number")
    number = float(value)
    if number != number or number < 0 or number > maximum:
        raise ModelRouteEvidenceError(f"{label} must be finite and between 0 and {maximum}")
    return round(number, 6)


def _bounded_text(value: object, label: str, *, limit: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelRouteEvidenceError(f"{label} must be a non-empty string")
    text = value.strip()
    if len(text) > limit:
        raise ModelRouteEvidenceError(f"{label} must be at most {limit} characters")
    return text


@dataclass(frozen=True)
class ModelRouteEvidence:
    """One run's model-route record.

    Every field is a fact the runtime observed. Nothing here is inferred from
    the policy: a record says what happened, and the policy comparison is a
    reviewer's job.
    """

    run_id: str
    policy_snapshot: str
    selected_route: str
    fallback_reason: str = "none"
    latency_ms: float = 0.0
    actual_cost_micros: float = 0.0
    shadow_cost_micros: float = 0.0
    local_evidence: str = "unknown"
    provider: str = "local"
    model: str = "unknown"
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _bounded_text(self.run_id, "run_id"))
        object.__setattr__(self, "policy_snapshot", _bounded_text(self.policy_snapshot, "policy_snapshot"))
        object.__setattr__(self, "selected_route", _bounded_text(self.selected_route, "selected_route"))
        object.__setattr__(self, "provider", _bounded_text(self.provider, "provider"))
        object.__setattr__(self, "model", _bounded_text(self.model, "model"))
        if self.fallback_reason not in FALLBACK_REASONS:
            raise ModelRouteEvidenceError(
                f"fallback_reason must be one of {list(FALLBACK_REASONS)}, got {self.fallback_reason!r}"
            )
        if self.local_evidence not in EVIDENCE_STATES:
            raise ModelRouteEvidenceError(
                f"local_evidence must be one of {list(EVIDENCE_STATES)}, got {self.local_evidence!r}"
            )
        object.__setattr__(self, "latency_ms", _bounded_number(self.latency_ms, "latency_ms"))
        object.__setattr__(self, "actual_cost_micros", _bounded_number(self.actual_cost_micros, "actual_cost_micros"))
        object.__setattr__(self, "shadow_cost_micros", _bounded_number(self.shadow_cost_micros, "shadow_cost_micros"))
        if not isinstance(self.details, Mapping):
            raise ModelRouteEvidenceError("details must be a mapping")

    def as_dict(self) -> dict[str, Any]:
        return {
            "version": MODEL_ROUTE_EVIDENCE_VERSION,
            "run_id": self.run_id,
            "policy_snapshot": self.policy_snapshot,
            "selected_route": self.selected_route,
            "fallback_reason": self.fallback_reason,
            "fell_back": self.fallback_reason != "none",
            "latency_ms": self.latency_ms,
            "actual_cost_micros": self.actual_cost_micros,
            "shadow_cost_micros": self.shadow_cost_micros,
            "local_evidence": self.local_evidence,
            "provider": self.provider,
            "model": self.model,
            "details": dict(self.details),
        }


def summarise(records: list[ModelRouteEvidence]) -> dict[str, Any]:
    """A bounded summary over one run's route records.

    Reports what the records say and nothing about what they should have said: a
    route with no record is counted as unrecorded rather than assumed to have
    followed the policy.
    """
    fell_back = [record for record in records if record.fallback_reason != "none"]
    reasons: dict[str, int] = {}
    for record in fell_back:
        reasons[record.fallback_reason] = reasons.get(record.fallback_reason, 0) + 1
    return {
        "version": MODEL_ROUTE_EVIDENCE_VERSION,
        "record_count": len(records),
        "routes": sorted({record.selected_route for record in records}),
        "fallback_count": len(fell_back),
        "fallback_reasons": {reason: reasons[reason] for reason in sorted(reasons)},
        "actual_cost_micros": round(sum(record.actual_cost_micros for record in records), 6),
        "shadow_cost_micros": round(sum(record.shadow_cost_micros for record in records), 6),
        # Any record that could not state its local evidence makes the summary
        # incomplete rather than complete-by-omission.
        "local_evidence_complete": bool(records) and all(record.local_evidence == "complete" for record in records),
        "unrecorded": not records,
    }


__all__ = [
    "EVIDENCE_STATES",
    "FALLBACK_REASONS",
    "MODEL_ROUTE_EVIDENCE_VERSION",
    "ModelRouteEvidence",
    "ModelRouteEvidenceError",
    "summarise",
]
