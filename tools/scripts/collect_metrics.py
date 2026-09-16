#!/usr/bin/env python3
"""Extract reproducible metrics from one version's JSON Lines event logs."""

import argparse
import hashlib
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from _jsonio import JsonInputError, load_json, load_jsonl

ALLOWED_ACTIONS = {"ask_confirm", "express", "grasp", "observe", "place", "stop"}
EVENT_TYPES = {
    "action_request",
    "action_result",
    "emotion",
    "fault",
    "observation",
    "policy_violation",
    "recovery_complete",
    "recovery_started",
    "task_accepted",
    "task_graph",
    "task_start",
    "task_terminal",
    "tool_call",
    "verification",
}


def _validate_run_log(log_file: Path, events: list[Any]) -> str:
    """Validate one JSONL run log before its events can influence metrics.

    A log that is internally inconsistent is rejected rather than repaired:
    trusting a partially valid file is how a run silently contributes wrong
    numerators to the release gates.
    """
    if any(not isinstance(event, dict) for event in events):
        raise RuntimeError(f"event log contains a non-object event: {log_file}")
    run_id = events[0].get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RuntimeError(f"event log has no run_id: {log_file}")
    for event in events:
        if event.get("run_id") != run_id:
            raise RuntimeError(f"run_id drift in {log_file}: expected {run_id!r}, found {event.get('run_id')!r}")
    sequences = [event.get("sequence_no") for event in events]
    if any(type(sequence) is not int for sequence in sequences):
        raise RuntimeError(f"non-integer sequence_no in {log_file}: {sequences}")
    ordered = sorted(sequences)
    if ordered != list(range(len(events))):
        raise RuntimeError(f"non-contiguous sequence_no values in {log_file}: {sequences}")
    event_ids = [event.get("event_id") for event in events]
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
        raise RuntimeError(f"missing event_id in {log_file}")
    if len(event_ids) != len(set(event_ids)):
        raise RuntimeError(f"duplicate event_id in {log_file}")
    for event in events:
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise RuntimeError(f"unknown event_type in {log_file}: {event_type!r}")
        if not isinstance(event.get("payload"), dict):
            raise RuntimeError(f"event payload is not an object in {log_file}: {event.get('event_id')!r}")
    return run_id


def _conflicting_metadata(run_id: str, previous: dict[str, Any], current: dict[str, Any]) -> str | None:
    """Report the first evaluation field whose value disagrees between files."""
    for key in sorted(set(previous) | set(current)):
        if previous.get(key) != current.get(key):
            return f"conflicting {key}: {previous.get(key)!r} and {current.get(key)!r}"
    return None


def load_runs(run_dir: Path) -> dict[str, list[dict[str, Any]]]:
    """Load every run log under ``run_dir``.

    ``run_evaluation.py`` writes logs as ``<output_dir>/<version>/*.jsonl`` while
    a single version directory is also a valid input, so both the flat and the
    documented nested layout are searched. A run ID appearing in more than one
    file fails closed instead of silently overwriting whichever file was read
    first, and the offending paths are named in the error.
    """
    if not run_dir.exists():
        raise RuntimeError(f"run directory does not exist: {run_dir}")
    if not run_dir.is_dir():
        raise RuntimeError(f"run directory is not a directory: {run_dir}")
    runs: dict[str, list[dict[str, Any]]] = {}
    sources: dict[str, Path] = {}
    metadata: dict[str, dict[str, Any]] = {}
    for log_file in sorted(run_dir.rglob("*.jsonl")):
        try:
            events = load_jsonl(log_file)
        except JsonInputError as exc:
            raise RuntimeError(f"unreadable JSONL event log: {exc}") from exc
        if not events:
            continue
        run_id = _validate_run_log(log_file, events)
        current_metadata = next(
            (event.get("evaluation") for event in events if isinstance(event.get("evaluation"), dict)),
            {},
        )
        if run_id in runs:
            conflict = _conflicting_metadata(run_id, metadata[run_id], current_metadata)
            detail = f" ({conflict})" if conflict else ""
            raise RuntimeError(
                f"duplicate run_id {run_id!r} in {sources[run_id]} and {log_file}{detail}; "
                "refusing to overwrite one run with another"
            )
        sources[run_id] = log_file
        metadata[run_id] = current_metadata
        runs[run_id] = sorted(events, key=lambda event: event["sequence_no"])
    return runs


def run_metadata(runs: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    """Preserve the version, runner, commit and scenario each run declares."""
    preserved: dict[str, dict[str, Any]] = {}
    for run_id, events in runs.items():
        evaluation = next(
            (event.get("evaluation") for event in events if isinstance(event.get("evaluation"), dict)),
            {},
        )
        preserved[run_id] = {
            key: evaluation.get(key) for key in ("commit", "scenario_id", "seed", "runner", "task_id", "scene_variant")
        }
    return preserved


def _load_summary(summary_path: Path) -> dict[str, Any]:
    """Read the optional run summary, rejecting ambiguous evidence."""
    if not summary_path.is_file():
        return {}
    try:
        payload = load_json(summary_path)
    except JsonInputError as exc:
        raise RuntimeError(f"unreadable run summary: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"run summary is not a JSON object: {summary_path}")
    return payload


def verification_statuses(events: list[dict[str, Any]]) -> list[str]:
    return [
        str(event.get("payload", {}).get("status")) for event in events if event.get("event_type") == "verification"
    ]


def task_id(events: list[dict[str, Any]]) -> str:
    accepted = next((event for event in events if event.get("event_type") == "task_accepted"), {})
    return str(accepted.get("payload", {}).get("task_id", "unknown"))


def percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[index]


MONOTONIC_CLOCK = "runner_monotonic_elapsed"
WALL_CLOCK = "occurred_at_wall_clock"


def _wall_clock_duration(run_id: str, events: list[dict[str, Any]]) -> float:
    """Derive a duration by subtracting two wall-clock timestamps.

    ``occurred_at`` is written by whichever node produced the event, so this is
    cross-node wall-clock arithmetic and is only used when the runner recorded
    no monotonic elapsed evidence of its own.
    """
    try:
        start = datetime.fromisoformat(str(events[0]["occurred_at"]).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(events[-1]["occurred_at"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"run {run_id!r} has unusable occurred_at timestamps") from exc
    if start.tzinfo is None or end.tzinfo is None:
        raise RuntimeError(f"run {run_id!r} has timezone-naive occurred_at timestamps")
    return (end - start).total_seconds()


def _measured_elapsed(run_id: str, events: list[dict[str, Any]]) -> float | None:
    """Return the runner's own monotonic elapsed measurement, if it recorded one.

    Only the runner-owned ``evaluation`` metadata is consulted, so an unrelated
    ``elapsed_s`` inside an event payload cannot silently become a duration.
    """
    for event in reversed(events):
        elapsed = event.get("evaluation", {}).get("elapsed_s")
        if elapsed is None:
            continue
        if isinstance(elapsed, bool) or not isinstance(elapsed, int | float):
            raise RuntimeError(f"run {run_id!r} has a non-numeric elapsed_s measurement: {elapsed!r}")
        return float(elapsed)
    return None


def durations(runs: dict[str, list[dict[str, Any]]]) -> list[float]:
    """Return per-run task durations, failing closed on unusable clocks.

    Runner-measured monotonic elapsed time is preferred, because subtracting
    ``occurred_at`` values across two clock domains can invent or hide time. A
    run that cannot produce a finite, non-negative duration is invalid
    evidence: silently skipping it would bias the percentile metrics upward.
    """
    result: list[float] = []
    for run_id, events in runs.items():
        if len(events) < 2:
            continue
        measured = _measured_elapsed(run_id, events)
        duration = _wall_clock_duration(run_id, events) if measured is None else measured
        if not math.isfinite(duration) or duration < 0:
            raise RuntimeError(f"run {run_id!r} has an invalid task duration: {duration}")
        result.append(duration)
    return result


def duration_sources(runs: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Name the clock behind every task duration so reports can qualify it."""
    sources: dict[str, str] = {}
    for run_id, events in runs.items():
        if len(events) < 2:
            continue
        measured = _measured_elapsed(run_id, events)
        sources[run_id] = MONOTONIC_CLOCK if measured is not None else WALL_CLOCK
    return sources


def replay_digest(events: list[dict[str, Any]]) -> str:
    state = {
        "run_id": events[0].get("run_id") if events else None,
        "event_ids": [],
        "last_action": None,
        "verification_status": None,
        "evidence_refs": [],
    }
    for event in sorted(events, key=lambda item: item.get("sequence_no", -1)):
        state["event_ids"].append(event.get("event_id"))
        if event.get("event_type") == "action_result":
            state["last_action"] = event.get("payload")
        if event.get("event_type") == "verification":
            state["verification_status"] = event.get("payload", {}).get("status")
        state["evidence_refs"].extend(event.get("evidence_refs", []))
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def audit_false_completions(
    runs: dict[str, list[dict[str, Any]]], audit_path: Path | None
) -> tuple[int | None, bool, str | None]:
    if audit_path is None:
        return None, False, None
    try:
        audit = load_json(audit_path)
    except JsonInputError as exc:
        raise RuntimeError(f"unreadable human audit: {exc}") from exc
    decisions = audit.get("runs", {})
    missing = sorted(set(runs) - set(decisions))
    if missing:
        raise RuntimeError(f"human audit is missing {len(missing)} run(s)")
    false_completions = 0
    for run_id, events in runs.items():
        statuses = verification_statuses(events)
        claimed_complete = bool(statuses and statuses[-1] == "confirmed")
        oracle_status = decisions[run_id].get("oracle_status")
        if claimed_complete and oracle_status != "confirmed":
            false_completions += 1
    return false_completions, True, audit.get("reviewed_by")


def collect(run_dir: Path, audit_path: Path | None = None) -> dict[str, Any]:
    runs = load_runs(run_dir)
    if not runs:
        raise RuntimeError(f"no JSON Lines event logs found in {run_dir}")
    events = [event for run in runs.values() for event in run]
    final_statuses = [verification_statuses(run)[-1] for run in runs.values() if verification_statuses(run)]
    verified = sum(status == "confirmed" for status in final_statuses)
    task_durations = durations(runs)
    action_requests = [event for event in events if event.get("event_type") == "action_request"]
    observations = [event for event in events if event.get("event_type") == "observation"]
    verifications = [event for event in events if event.get("event_type") == "verification"]
    plans = [event for event in events if event.get("event_type") == "task_graph"]

    recoverable = [run for run in runs.values() if "refuted" in verification_statuses(run)[:-1]]
    recovered = sum(verification_statuses(run)[-1] == "confirmed" for run in recoverable)
    valid_replays = sum(
        [event.get("sequence_no") for event in run] == list(range(len(run)))
        and all(event.get("run_id") == run_id for event in run)
        for run_id, run in runs.items()
    )
    stable_hashes = sum(replay_digest(run) == replay_digest(list(reversed(run))) for run in runs.values())
    false_completions, audit_complete, reviewed_by = audit_false_completions(runs, audit_path)
    run_task_ids = {run_id: task_id(run) for run_id, run in runs.items()}
    task_family_distribution = Counter(run_task_ids.values())
    task_family_vtcr = {}
    for family, family_run_count in sorted(task_family_distribution.items()):
        family_runs = [run for run_id, run in runs.items() if run_task_ids[run_id] == family]
        family_verified = sum(verification_statuses(run)[-1] == "confirmed" for run in family_runs)
        task_family_vtcr[family] = family_verified / family_run_count
    observed_entities = [
        len(
            {
                event.get("payload", {}).get("entity_id")
                for event in run
                if event.get("event_type") == "observation" and event.get("payload", {}).get("entity_id")
            }
        )
        for run in runs.values()
    ]
    final_verifications = [
        [event for event in run if event.get("event_type") == "verification"][-1]
        for run in runs.values()
        if any(event.get("event_type") == "verification" for event in run)
    ]
    required_condition_count = 0
    evaluated_condition_count = 0
    for event in final_verifications:
        payload = event.get("payload", {})
        required = payload.get("required_conditions", [])
        evaluated = payload.get("evaluated_conditions", [])
        required_set = set(required) if isinstance(required, list) else set()
        evaluated_set = set(evaluated) if isinstance(evaluated, list) else set()
        required_condition_count += len(required_set)
        evaluated_condition_count += len(required_set & evaluated_set)

    summary_path = run_dir.parent / "summary.json"
    summary = _load_summary(summary_path)
    return {
        "false_completion_count": false_completions,
        "false_completion_reviewed": audit_complete,
        "false_completion_reviewed_by": reviewed_by,
        "collision_count": sum(
            event.get("event_type") == "fault" and event.get("payload", {}).get("fault_type") == "collision"
            for event in events
        ),
        "policy_violation_count": sum(event.get("event_type") == "policy_violation" for event in events),
        "vtcr": verified / len(final_statuses) if final_statuses else 0.0,
        "task_duration_p50_s": percentile(task_durations, 0.5),
        "task_duration_p95_s": percentile(task_durations, 0.95),
        "task_duration_sources": duration_sources(runs),
        "recovery_rate": recovered / len(recoverable) if recoverable else None,
        "tool_call_validity": (
            sum(event.get("payload", {}).get("action_type") in ALLOWED_ACTIONS for event in action_requests)
            / len(action_requests)
            if action_requests
            else 1.0
        ),
        "local_planning_coverage": (
            sum(event.get("payload", {}).get("model_route") in {"local", "template"} for event in plans) / len(plans)
            if plans
            else 0.0
        ),
        "observation_completeness": (
            sum(
                all(
                    field in event.get("payload", {})
                    for field in ("observation_id", "run_id", "entity_id", "pose", "confidence")
                )
                for event in observations
            )
            / len(observations)
            if observations
            else 1.0
        ),
        "evidence_coverage": (
            sum(bool(event.get("payload", {}).get("evidence_refs")) for event in verifications) / len(verifications)
            if verifications
            else 0.0
        ),
        "state_hash_consistency": stable_hashes / len(runs),
        "replay_success_rate": valid_replays / len(runs),
        "task_family_count": len(task_family_distribution),
        "task_family_distribution": dict(sorted(task_family_distribution.items())),
        "task_family_vtcr": task_family_vtcr,
        "complex_task_rate": sum(family != "task-place-red-block" for family in run_task_ids.values()) / len(runs),
        "mean_observed_entities": sum(observed_entities) / len(observed_entities) if observed_entities else 0.0,
        "goal_condition_coverage": (
            evaluated_condition_count / required_condition_count if required_condition_count else 0.0
        ),
        "run_count": len(runs),
        "total_events": len(events),
        "run_dir": str(run_dir),
        "runner": summary.get("runner", "unknown"),
        "release_eligible": bool(summary.get("release_eligible", False)) and audit_complete,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract metrics from event logs")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("metrics.json"))
    parser.add_argument("--human-audit", type=Path)
    args = parser.parse_args()
    metrics = collect(args.run_dir, args.human_audit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(json.dumps(metrics, indent=2))
    print(f"metrics written to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
