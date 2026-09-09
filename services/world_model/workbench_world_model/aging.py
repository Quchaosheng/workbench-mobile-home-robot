from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import TYPE_CHECKING

from workbench_contracts import ClockId, WorldBelief

if TYPE_CHECKING:
    from .reducer import WorldState


_UTC_WALL_TIME_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")


def _finite_seconds(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError(f"{field_name} must be a finite number of seconds")
    seconds = float(value)
    if not math.isfinite(seconds):
        raise ValueError(f"{field_name} must be a finite number of seconds")
    return seconds


def _non_blank_policy_label(value: object, field_name: str) -> str:
    if type(value) is not str or not value.strip() or value.strip() == "*":
        raise ValueError(f"{field_name} must be an explicit non-wildcard label")
    return value


def parse_utc_wall_time(value: object) -> datetime:
    """Parse the only timestamp form that can be compared during replay."""
    if type(value) is not str or _UTC_WALL_TIME_PATTERN.fullmatch(value) is None:
        raise ValueError("wall time must be an RFC3339 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValueError("wall time must be an RFC3339 UTC timestamp") from error
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("wall time must use UTC")
    return parsed


@dataclass(frozen=True)
class FreshnessThresholds:
    """Finite freshness bounds for one explicit source/entity tuple."""

    stale_after_s: float
    lost_after_s: float

    def __post_init__(self) -> None:
        stale_after_s = _finite_seconds(self.stale_after_s, "stale_after_s")
        lost_after_s = _finite_seconds(self.lost_after_s, "lost_after_s")
        if stale_after_s <= 0:
            raise ValueError("stale_after_s must be greater than zero")
        if lost_after_s <= 0:
            raise ValueError("lost_after_s must be greater than zero")
        if stale_after_s > lost_after_s:
            raise ValueError("stale_after_s must be less than or equal to lost_after_s")
        object.__setattr__(self, "stale_after_s", stale_after_s)
        object.__setattr__(self, "lost_after_s", lost_after_s)


@dataclass(frozen=True)
class ObservationFreshnessPolicy:
    """Explicit policy with no wildcard or permissive fallback rule."""

    rules: Mapping[tuple[str, str], FreshnessThresholds]

    def __post_init__(self) -> None:
        if not isinstance(self.rules, Mapping) or not self.rules:
            raise ValueError("freshness policy requires at least one explicit rule")
        normalized: dict[tuple[str, str], FreshnessThresholds] = {}
        for key, thresholds in self.rules.items():
            if type(key) is not tuple or len(key) != 2:
                raise ValueError("freshness policy keys must be (source, entity_type) tuples")
            source = _non_blank_policy_label(key[0], "source")
            entity_type = _non_blank_policy_label(key[1], "entity_type")
            if not isinstance(thresholds, FreshnessThresholds):
                raise ValueError("freshness policy values must be FreshnessThresholds")
            normalized[(source, entity_type)] = thresholds
        object.__setattr__(self, "rules", MappingProxyType(normalized))

    def rule_for(self, source: object, entity_type: object) -> FreshnessThresholds | None:
        if type(source) is not str or type(entity_type) is not str:
            return None
        return self.rules.get((source, entity_type))

    def tightened(
        self,
        overrides: Mapping[tuple[str, str], FreshnessThresholds],
    ) -> ObservationFreshnessPolicy:
        """Return a deployment policy that can only make bounds shorter."""
        if not isinstance(overrides, Mapping):
            raise ValueError("freshness overrides must be a mapping")
        merged = dict(self.rules)
        for key, replacement in overrides.items():
            if key not in self.rules:
                raise ValueError(f"unknown freshness policy tuple: {key!r}")
            if not isinstance(replacement, FreshnessThresholds):
                raise ValueError("freshness policy values must be FreshnessThresholds")
            original = self.rules[key]
            if replacement.stale_after_s > original.stale_after_s or replacement.lost_after_s > original.lost_after_s:
                raise ValueError(f"deployment override cannot widen freshness thresholds for {key!r}")
            merged[key] = replacement
        return ObservationFreshnessPolicy(rules=merged)


@dataclass(frozen=True)
class ObservationAgingBoundary:
    """Immutable caller-supplied boundary; it never reads process time."""

    as_of: str
    clock_id: ClockId

    def __post_init__(self) -> None:
        if type(self.as_of) is not str or not self.as_of.strip():
            raise ValueError("aging boundary as_of must be a non-empty string")
        try:
            clock_id = self.clock_id if isinstance(self.clock_id, ClockId) else ClockId(self.clock_id)
        except (TypeError, ValueError) as error:
            raise ValueError("aging boundary requires a supported clock_id") from error
        if clock_id is ClockId.WALL:
            parse_utc_wall_time(self.as_of)
        object.__setattr__(self, "clock_id", clock_id)


def comparable_wall_observation_is_older(
    *,
    current_observed_at: object,
    current_clock_id: object,
    incoming_observed_at: object,
    incoming_clock_id: object,
) -> bool:
    """Return true only when both wall timestamps prove the incoming value older."""
    if current_clock_id not in (ClockId.WALL, ClockId.WALL.value):
        return False
    if incoming_clock_id not in (ClockId.WALL, ClockId.WALL.value):
        return False
    try:
        current = parse_utc_wall_time(current_observed_at)
        incoming = parse_utc_wall_time(incoming_observed_at)
    except ValueError:
        return False
    return incoming < current


def _belief_for_observation(
    *,
    observed_at: object,
    observation_clock_id: object,
    source: object,
    entity_type: object,
    policy: ObservationFreshnessPolicy,
    boundary: ObservationAgingBoundary,
) -> WorldBelief:
    if boundary.clock_id is not ClockId.WALL:
        return WorldBelief.LOST
    if observation_clock_id not in (ClockId.WALL, ClockId.WALL.value):
        return WorldBelief.LOST
    thresholds = policy.rule_for(source, entity_type)
    if thresholds is None:
        return WorldBelief.LOST
    try:
        observation_time = parse_utc_wall_time(observed_at)
        boundary_time = parse_utc_wall_time(boundary.as_of)
    except ValueError:
        return WorldBelief.LOST
    age_s = (boundary_time - observation_time).total_seconds()
    if age_s < 0:
        return WorldBelief.LOST
    if age_s < thresholds.stale_after_s:
        return WorldBelief.OBSERVED
    if age_s < thresholds.lost_after_s:
        return WorldBelief.STALE
    return WorldBelief.LOST


def _least_fresh(*beliefs: WorldBelief) -> WorldBelief:
    if WorldBelief.LOST in beliefs:
        return WorldBelief.LOST
    if WorldBelief.STALE in beliefs:
        return WorldBelief.STALE
    if WorldBelief.INFERRED in beliefs:
        return WorldBelief.INFERRED
    return WorldBelief.OBSERVED


def _age_attribute_metadata(
    state: WorldState,
    *,
    freshness_policy: ObservationFreshnessPolicy,
    aging_boundary: ObservationAgingBoundary,
) -> None:
    for entity_id in sorted(state.entity_attributes):
        metadata_by_key = state.entity_attribute_metadata.get(entity_id)
        if metadata_by_key is None:
            continue
        for attribute_key in sorted(metadata_by_key):
            raw_metadata = metadata_by_key[attribute_key]
            if not isinstance(raw_metadata, Mapping):
                continue
            belief = _belief_for_observation(
                observed_at=raw_metadata.get("observed_at"),
                observation_clock_id=raw_metadata.get("clock_id"),
                source=raw_metadata.get("source"),
                entity_type=state.entity_types.get(entity_id),
                policy=freshness_policy,
                boundary=aging_boundary,
            )
            if raw_metadata.get("belief") == WorldBelief.INFERRED.value and belief is WorldBelief.OBSERVED:
                belief = WorldBelief.INFERRED
            raw_metadata["belief"] = belief.value


def age_world_state(
    state: WorldState,
    *,
    freshness_policy: ObservationFreshnessPolicy,
    aging_boundary: ObservationAgingBoundary,
) -> WorldState:
    """Return a detached state aged only against explicit replay inputs."""
    if not isinstance(freshness_policy, ObservationFreshnessPolicy):
        raise TypeError("freshness_policy must be an ObservationFreshnessPolicy")
    if not isinstance(aging_boundary, ObservationAgingBoundary):
        raise TypeError("aging_boundary must be an ObservationAgingBoundary")

    aged = state.model_copy(deep=True)
    aged.freshness_evaluated = True
    aged.entity_beliefs = {}
    aged.entity_location_beliefs = {}
    _age_attribute_metadata(
        aged,
        freshness_policy=freshness_policy,
        aging_boundary=aging_boundary,
    )
    for entity_id in sorted(aged.entity_types):
        aged.entity_beliefs[entity_id] = _belief_for_observation(
            observed_at=aged.entity_last_observed_at.get(entity_id),
            observation_clock_id=aged.entity_observation_clock_ids.get(entity_id),
            source=aged.entity_observation_sources.get(entity_id),
            entity_type=aged.entity_types.get(entity_id),
            policy=freshness_policy,
            boundary=aging_boundary,
        )

    for entity_id, location in sorted(aged.entity_locations.items()):
        _, separator, endpoint_id = location.partition(":")
        endpoint_belief = (
            aged.entity_beliefs.get(endpoint_id, WorldBelief.LOST)
            if separator and endpoint_id.strip()
            else WorldBelief.LOST
        )
        location_observation_belief = _belief_for_observation(
            observed_at=aged.entity_location_last_observed_at.get(entity_id),
            observation_clock_id=aged.entity_location_clock_ids.get(entity_id),
            source=aged.entity_location_sources.get(entity_id),
            entity_type=aged.entity_types.get(entity_id),
            policy=freshness_policy,
            boundary=aging_boundary,
        )
        aged.entity_location_beliefs[entity_id] = _least_fresh(
            aged.entity_beliefs.get(entity_id, WorldBelief.LOST),
            endpoint_belief,
            location_observation_belief,
        )
    return aged


__all__ = [
    "FreshnessThresholds",
    "ObservationAgingBoundary",
    "ObservationFreshnessPolicy",
    "age_world_state",
    "comparable_wall_observation_is_older",
    "parse_utc_wall_time",
]
