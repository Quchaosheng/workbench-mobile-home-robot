"""Ask one bounded "what if" about a recorded run without editing it.

The repository can replay a run and verify its provenance, but a reviewer could
not ask what a different input would have produced: the event stream is fixed,
and the only way to change it was to edit a run bundle (issue #411).

This tool applies a named, in-memory transformation to a copy of the events,
recomputes the canonical state, and reports what changed. It is deliberately
narrow:

- one transformation per invocation, named on the command line
- the input is never written to; the output says what it did and to which event
- the result is labelled a counterfactual, never a replay of the original run

It has no release or physical-validation authority, and it does not claim the
hypothetical run is in any way observed.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT / "libs" / "kernel"),
    str(ROOT / "libs" / "application"),
    str(ROOT / "libs" / "contracts"),
    str(ROOT / "services" / "world_model"),
]

from workbench_contracts.models import WorldEvent, WorldEventType
from workbench_world_model.reducer import reduce_events

COUNTERFACTUAL_VERSION = "counterfactual-replay-v1"

# The closed set of transformations. Each is a pure edit of one event's fields;
# anything that would need a new event, a reordering, or a change to the run's
# identity is out of scope on purpose.
TRANSFORMS = ("drop-event", "replace-field")


class CounterfactualError(ValueError):
    """A counterfactual that cannot be applied as stated."""


def _events(payload: object) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise CounterfactualError("the run bundle must be a JSON object")
    events = payload.get("events")
    if not isinstance(events, list) or not events:
        raise CounterfactualError("the run bundle must carry a non-empty events list")
    return [copy.deepcopy(event) for event in events if isinstance(event, dict)]


def _delete_path(event: dict[str, Any], dotted: str) -> None:
    parts = [part for part in dotted.split(".") if part]
    if not parts:
        raise CounterfactualError("a field path must name at least one segment")
    target: Any = event
    for part in parts[:-1]:
        if not isinstance(target, dict) or part not in target:
            raise CounterfactualError(f"the event has no field {dotted!r}")
        target = target[part]
    if not isinstance(target, dict) or parts[-1] not in target:
        raise CounterfactualError(f"the event has no field {dotted!r}")
    del target[parts[-1]]


def apply_transform(
    events: list[dict[str, Any]],
    *,
    transform: str,
    event_id: str,
    field: str | None = None,
    value: object = None,
) -> list[dict[str, Any]]:
    """Return a transformed copy; the input list is not modified."""
    if transform not in TRANSFORMS:
        raise CounterfactualError(f"unknown transform {transform!r}; expected one of {list(TRANSFORMS)}")
    if not any(event.get("event_id") == event_id for event in events):
        raise CounterfactualError(f"no event in the run has id {event_id!r}")
    if transform == "drop-event":
        return [event for event in events if event.get("event_id") != event_id]
    if not field:
        raise CounterfactualError("replace-field needs a field path")
    result = copy.deepcopy(events)
    for event in result:
        if event.get("event_id") == event_id:
            parts = [part for part in field.split(".") if part]
            target: Any = event
            for part in parts[:-1]:
                if not isinstance(target, dict) or part not in target:
                    raise CounterfactualError(f"the event has no field {field!r}")
                target = target[part]
            if not isinstance(target, dict) or parts[-1] not in target:
                # Replacing a field the event does not have would invent one, and
                # the report would describe a change to a document that never
                # carried it. The counterfactual edits; it does not extend.
                raise CounterfactualError(f"the event has no field {field!r}")
            target[parts[-1]] = value
            break
    return result


def _canonical(events: list[dict[str, Any]], run_id: str) -> dict[str, Any]:
    # The model is strict, so the event type arrives as its enum: a raw string
    # would be silently accepted by a laxer loader and then compared wrongly.
    prepared: list[dict[str, Any]] = []
    for event in events:
        item = dict(event)
        raw_type = item.get("event_type")
        if isinstance(raw_type, str):
            item["event_type"] = WorldEventType(raw_type)
        prepared.append(item)
    parsed = [WorldEvent.model_validate(event) for event in prepared]
    state = reduce_events(run_id, parsed)
    payload = state.as_dict() if hasattr(state, "as_dict") else dict(state)
    return {
        "entity_locations": payload.get("entity_locations", {}),
        "entity_beliefs": payload.get("entity_beliefs", {}),
    }


def replay_counterfactual(
    bundle: dict[str, Any],
    *,
    transform: str,
    event_id: str,
    field: str | None = None,
    value: object = None,
) -> dict[str, Any]:
    """Apply one transformation in memory and report the difference.

    The bundle is read, never written. The report names the transformation, the
    event it touched and what changed in the canonical state, so a reviewer can
    reproduce it from the same inputs.
    """
    events = _events(bundle)
    run_id = str(bundle.get("run_id") or events[0].get("run_id") or "")
    if not run_id:
        raise CounterfactualError("the run bundle carries no run_id")
    before = _canonical(events, run_id)
    after_events = apply_transform(events, transform=transform, event_id=event_id, field=field, value=value)
    try:
        after = _canonical(after_events, run_id)
    except Exception as error:  # noqa: BLE001 - the reducer's refusal is the result
        # A transformation that makes the stream invalid is a finding, not a
        # crash: the reviewer asked what would happen, and this is the answer.
        return {
            "version": COUNTERFACTUAL_VERSION,
            "label": "counterfactual",
            "run_id": run_id,
            "transform": transform,
            "event_id": event_id,
            "field": field,
            "applied": False,
            "refused_by": "reducer",
            "refusal": str(error),
            "observed_run": False,
        }
    changed = sorted(
        entity
        for entity in set(before["entity_locations"]) | set(after["entity_locations"])
        if before["entity_locations"].get(entity) != after["entity_locations"].get(entity)
    )
    return {
        "version": COUNTERFACTUAL_VERSION,
        "label": "counterfactual",
        "run_id": run_id,
        "transform": transform,
        "event_id": event_id,
        "field": field,
        "applied": True,
        "events_before": len(events),
        "events_after": len(after_events),
        "changed_entities": changed,
        "beliefs_before": before["entity_beliefs"],
        "beliefs_after": after["entity_beliefs"],
        # Stated in the payload so the report cannot be mistaken for a replay.
        "observed_run": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Apply one counterfactual to a run bundle")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--transform", required=True, choices=TRANSFORMS)
    parser.add_argument("--event-id", required=True)
    parser.add_argument("--field")
    parser.add_argument("--value")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        bundle = json.loads(args.bundle.read_text(encoding="utf-8"))
        report = replay_counterfactual(
            bundle,
            transform=args.transform,
            event_id=args.event_id,
            field=args.field,
            value=args.value,
        )
    except (OSError, json.JSONDecodeError, CounterfactualError) as error:
        print(f"counterfactual refused: {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
