"""Where do two traces of the same scenario disagree, step by step.

The repository is careful not to merge environment classes: provenance keeps
scripted, Gazebo and physical runs apart, and replay_compatible() refuses a
mismatch. That protects the evidence but leaves a reviewer's question open — when
the same scenario identity runs in two environments, where do the traces
diverge? Nothing produced that report, so a disagreement had to be found by
reading two event logs side by side (issue #413).

This is a read-only comparison. It keeps each trace's environment class in its
own section, never merges them, and distinguishes "these two steps disagree" from
"this run never executed" — the two findings a reviewer must not conflate.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

DIVERGENCE_VERSION = "semantic-divergence-v1"

# Fields that identify a step rather than describe what happened in it. Comparing
# these would report every pair as divergent for the wrong reason.
IDENTITY_FIELDS = ("event_id", "sequence_no", "occurred_at", "evidence_refs")

# The largest number of differing steps reported per pair. A report is a pointer
# to where to look, not a dump of both logs.
MAX_REPORTED_STEPS = 50


class DivergenceError(ValueError):
    """A comparison that cannot be made as stated."""


def _trace_events(payload: object) -> tuple[str, list[dict[str, Any]]]:
    if not isinstance(payload, dict):
        raise DivergenceError("a trace must be a JSON object")
    events = payload.get("events")
    if not isinstance(events, list):
        raise DivergenceError("a trace must carry an events list")
    environment = payload.get("environment_class")
    if not isinstance(environment, str) or not environment:
        # Never guessed: an unlabelled trace would silently join a family it may
        # not belong to.
        raise DivergenceError("a trace must state its environment_class")
    return environment, [event for event in events if isinstance(event, dict)]


def _comparable(event: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in event.items() if key not in IDENTITY_FIELDS}


def _step_key(event: dict[str, Any]) -> str:
    event_id = event.get("event_id")
    if isinstance(event_id, str) and event_id:
        return event_id
    return f"seq:{event.get('sequence_no')}"


def compare_traces(left: object, right: object) -> dict[str, Any]:
    """Compare two traces by step, preserving each one's environment class."""
    left_env, left_events = _trace_events(left)
    right_env, right_events = _trace_events(right)

    left_by_step = {_step_key(event): event for event in left_events}
    right_by_step = {_step_key(event): event for event in right_events}

    only_left = sorted(set(left_by_step) - set(right_by_step))
    only_right = sorted(set(right_by_step) - set(left_by_step))

    divergent: list[dict[str, Any]] = []
    for step in sorted(set(left_by_step) & set(right_by_step)):
        left_event = _comparable(left_by_step[step])
        right_event = _comparable(right_by_step[step])
        if left_event == right_event:
            continue
        fields = sorted(
            key for key in set(left_event) | set(right_event) if left_event.get(key) != right_event.get(key)
        )
        divergent.append(
            {
                "step": step,
                "fields": fields,
                # The values are included so the report is actionable without
                # reopening both logs.
                "left": {key: left_event.get(key) for key in fields},
                "right": {key: right_event.get(key) for key in fields},
            }
        )

    truncated = len(divergent) > MAX_REPORTED_STEPS
    compared = len(set(left_by_step) & set(right_by_step))
    return {
        "version": DIVERGENCE_VERSION,
        "families": [
            {"environment_class": left_env, "event_count": len(left_events)},
            {"environment_class": right_env, "event_count": len(right_events)},
        ],
        # Kept apart on purpose: merging them would be the honesty problem the
        # provenance rules exist to prevent.
        "comparable": left_env == right_env,
        "steps_compared": compared,
        "divergent_step_count": len(divergent),
        "divergent_steps": divergent[:MAX_REPORTED_STEPS],
        "truncated": truncated,
        "only_in_left": only_left[:MAX_REPORTED_STEPS],
        "only_in_right": only_right[:MAX_REPORTED_STEPS],
        # The distinction the issue asks for: a trace with no events did not
        # execute, which is not the same as a trace that agreed.
        "left_executed": bool(left_events),
        "right_executed": bool(right_events),
        "never_executed": [
            environment for environment, events in ((left_env, left_events), (right_env, right_events)) if not events
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Compare two traces step by step")
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        report = compare_traces(
            json.loads(args.left.read_text(encoding="utf-8")),
            json.loads(args.right.read_text(encoding="utf-8")),
        )
    except (OSError, json.JSONDecodeError, DivergenceError) as error:
        print(f"divergence refused: {error}", file=sys.stderr)
        return 2
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
