#!/usr/bin/env python3
"""Extract reproducible metrics from one version's JSON Lines event logs.

Every published aggregate carries the run records it was computed from
(``runs``), the run ids behind each numerator (``metric_contributors``) and a
digest over both (``evidence_index_sha256``), because a total alone is not
evidence: a reviewer has to be able to name the runs, and to recompute them.
"""

import argparse
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from _jsonio import JsonInputError, load_json, load_jsonl
from _paths import enable_local_packages
from release_eligibility import (
    ELIGIBLE_RUNNER,
    SCRIPTED_RUNNERS,
    discover_manifests,
    evaluate_eligibility,
    event_log_sha256,
    file_sha256,
    load_provenance,
    manifest_search_dirs,
    resolve_recorded_path,
    sha256_bytes,
)

enable_local_packages()

from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model import create_world_state_snapshot

ALLOWED_ACTIONS = {"ask_confirm", "express", "grasp", "observe", "place", "stop"}
# The per-run index is published inside the metrics record, so it is bounded: a
# run set that does not fit is refused rather than truncated, because a truncated
# index cannot cover the same run set as the aggregate it is meant to explain.
MAX_INDEXED_RUNS = 256
MAX_RUN_EVIDENCE_REFS = 8
EVIDENCE_INDEX_FORMAT = "workbench-evaluation-evidence-index"
EVIDENCE_INDEX_FORMAT_VERSION = 1
BASELINE_TASK_FAMILY = "task-place-red-block"
# How a published aggregate is rebuilt from the per-run contributions:
# ``sum`` adds the numerators, ``ratio`` divides the summed numerators by the
# summed denominators, and ``sample`` is a percentile over the contributed
# measurements. Each run contributes at most once to each metric.
METRIC_AGGREGATION = {
    "collision_count": "sum",
    "complex_task_rate": "ratio",
    "evidence_coverage": "ratio",
    "false_completion_count": "sum",
    "goal_condition_coverage": "ratio",
    "local_planning_coverage": "ratio",
    "mean_observed_entities": "ratio",
    "observation_completeness": "ratio",
    "policy_violation_count": "sum",
    "recovery_rate": "ratio",
    "replay_success_rate": "ratio",
    "state_hash_consistency": "ratio",
    "task_family_count": "distinct",
    "task_duration_p50_s": "sample",
    "task_duration_p95_s": "sample",
    "tool_call_validity": "ratio",
    "vtcr": "ratio",
}
AGGREGATE_METRICS = tuple(METRIC_AGGREGATION)
# A sample metric is a percentile over the runs that measured it.
SAMPLE_QUANTILE = {"task_duration_p50_s": 0.5, "task_duration_p95_s": 0.95}
DURATION_METRICS = tuple(SAMPLE_QUANTILE)
# The value published when no run contributes anything. An absent measurement is
# not a zero: without an audit there is no false-completion count, and without a
# recoverable run there is no recovery rate. A ratio metric with no declared
# entry fails the self-check rather than silently publishing a invented zero.
NO_CONTRIBUTOR_VALUE = {
    "evidence_coverage": 0.0,
    "false_completion_count": None,
    "goal_condition_coverage": 0.0,
    "local_planning_coverage": 0.0,
    "mean_observed_entities": 0.0,
    "observation_completeness": 1.0,
    "recovery_rate": None,
    "task_duration_p50_s": None,
    "task_duration_p95_s": None,
    "tool_call_validity": 1.0,
    "vtcr": 0.0,
}
# Fields that are labels on the index rather than part of the digest: ``run_dir``
# is a local path, and the rest is the index the digest describes.
DIGEST_EXCLUDED_KEYS = frozenset(
    {"runs", "metric_contributors", "metric_exclusions", "evidence_index_sha256", "run_dir"}
)
OBSERVATION_FIELDS = ("observation_id", "run_id", "entity_id", "pose", "confidence")
# The evaluation log envelope field. It is log provenance owned by the runner,
# not event state: ``WorldEvent`` forbids extra fields, so replay drops it and
# the other metrics keep reading it from the untrusted record.
LOG_ENVELOPE_FIELD = "evaluation"
# The record classes the World Model reducer owns. A planning record such as
# ``task_graph`` is stored in the same log but is not state, so it stays out of
# the state projection instead of being forced into a contract it does not have.
STATE_EVENT_TYPES = {event_type.value for event_type in WorldEventType}
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


def _declared_runner(runs: dict[str, list[dict[str, Any]]], summary: dict[str, Any]) -> str:
    """Report the runner the event logs declare, not the one a summary claims."""
    declared = sorted(
        {
            str(event["evaluation"]["runner"])
            for run in runs.values()
            for event in run
            if isinstance(event.get("evaluation"), dict) and event["evaluation"].get("runner")
        }
    )
    if not declared:
        return "unknown"
    if len(declared) == 1:
        return declared[0]
    return "mixed:" + ",".join(declared)


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


def run_durations(runs: dict[str, list[dict[str, Any]]]) -> dict[str, float]:
    """Return each run's task duration, failing closed on unusable clocks.

    Runner-measured monotonic elapsed time is preferred, because subtracting
    ``occurred_at`` values across two clock domains can invent or hide time. A
    run that cannot produce a finite, non-negative duration is invalid
    evidence: silently skipping it would bias the percentile metrics upward.
    """
    result: dict[str, float] = {}
    for run_id, events in runs.items():
        if len(events) < 2:
            continue
        measured = _measured_elapsed(run_id, events)
        duration = _wall_clock_duration(run_id, events) if measured is None else measured
        if not math.isfinite(duration) or duration < 0:
            raise RuntimeError(f"run {run_id!r} has an invalid task duration: {duration}")
        result[run_id] = duration
    return result


def durations(runs: dict[str, list[dict[str, Any]]]) -> list[float]:
    """Return per-run task durations in run order."""
    return list(run_durations(runs).values())


def duration_sources(runs: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    """Name the clock behind every task duration so reports can qualify it."""
    sources: dict[str, str] = {}
    for run_id, events in runs.items():
        if len(events) < 2:
            continue
        measured = _measured_elapsed(run_id, events)
        sources[run_id] = MONOTONIC_CLOCK if measured is not None else WALL_CLOCK
    return sources


def _world_event(run_id: str, record: dict[str, Any]) -> WorldEvent:
    """Project one log record onto the canonical WorldEvent contract.

    Only the runner's log envelope is removed. Every other field must already be
    a valid WorldEvent field, so a record with an unknown key, a non-canonical
    event type, or a malformed payload is rejected here instead of being
    repaired into something the reducer would accept.
    """
    body = {key: value for key, value in record.items() if key != LOG_ENVELOPE_FIELD}
    unknown = sorted(set(body) - set(WorldEvent.model_fields))
    if unknown:
        raise ValueError(f"log record carries fields outside the WorldEvent contract: {unknown}")
    try:
        encoded = json.dumps(body, allow_nan=False, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError, UnicodeError) as error:
        raise ValueError("log record is not representable as finite UTF-8 JSON") from error
    return WorldEvent.model_validate_json(encoded)


def canonical_replay(run_id: str, events: list[dict[str, Any]]) -> tuple[str, str]:
    """Replay one run through the World Model reducer and return its state hashes.

    The forward hash and the hash of the same stream delivered in a permuted
    order are both computed from the canonical WorldState produced by
    ``create_world_state_snapshot``, so the comparison exercises semantic state
    rather than a sorted summary of event envelopes. Any validation or reducer
    failure propagates as a RuntimeError: a run that cannot be replayed must stop
    metric collection rather than contribute a partial numerator.
    """
    records = [event for event in events if event.get("event_type") in STATE_EVENT_TYPES]
    if not records:
        raise RuntimeError(f"run {run_id!r} has no state-affecting events to replay")
    try:
        typed_events = [_world_event(run_id, record) for record in records]
        forward = create_world_state_snapshot(run_id, typed_events)
        permuted = create_world_state_snapshot(run_id, list(reversed(typed_events)))
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"canonical WorldState replay failed for run {run_id!r}: {error}") from error
    return forward.state_hash, permuted.state_hash


def load_human_audit(audit_path: Path | None) -> dict[str, Any] | None:
    """Read the human audit without interpreting it.

    Interpretation belongs to ``release_eligibility.evaluate_eligibility``, so
    metrics and the release report cannot drift into two different readings of
    the same file.
    """
    if audit_path is None:
        return None
    try:
        audit = load_json(audit_path)
    except JsonInputError as exc:
        raise RuntimeError(f"unreadable human audit: {exc}") from exc
    if not isinstance(audit, dict):
        raise RuntimeError(f"human audit is not an object: {audit_path}")
    return audit


def audit_false_completions(
    runs: dict[str, list[dict[str, Any]]], audit_path: Path | None
) -> tuple[int | None, bool, str | None]:
    """Count false completions using the shared eligibility predicate."""
    audit = load_human_audit(audit_path)
    if audit is None:
        return None, False, None
    verdict = evaluate_eligibility(runs=runs, provenance=None, manifests=None, audit=audit)
    decisions = audit.get("runs")
    if not isinstance(decisions, dict) or set(runs) - set(decisions):
        raise RuntimeError(f"human audit is missing {len(set(runs) - set(decisions or {}))} run(s)")
    false_completions = 0
    for run_id, events in runs.items():
        statuses = verification_statuses(events)
        claimed_complete = bool(statuses and statuses[-1] == "confirmed")
        if claimed_complete and decisions[run_id].get("oracle_status") != "confirmed":
            false_completions += 1
    reviewed_by = audit.get("reviewed_by")
    # Reviewer and timestamp are part of eligibility, not of this count, so an
    # audit that cannot be attributed is reported as not reviewed.
    complete = not any(
        reason in verdict["reasons"]
        for reason in (
            "human audit is missing reviewer, timestamp or per-run oracle status",
            "human audit does not cover every run",
        )
    )
    attributed = reviewed_by.strip() if isinstance(reviewed_by, str) else ""
    return false_completions, complete, attributed or None


def _evaluation_of(events: list[dict[str, Any]]) -> dict[str, Any]:
    return next((event.get("evaluation") for event in events if isinstance(event.get("evaluation"), dict)), {})


def source_class(runner: str | None) -> str:
    """Name the evidence class a run belongs to, in a closed vocabulary."""
    if not runner:
        return "unknown"
    if runner.casefold() in SCRIPTED_RUNNERS:
        return "scripted"
    return ELIGIBLE_RUNNER if runner == ELIGIBLE_RUNNER else "unknown"


def _final_verification(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((event for event in reversed(events) if event.get("event_type") == "verification"), None)


def _payload_status(event: dict[str, Any] | None) -> str | None:
    payload = (event or {}).get("payload", {})
    status = payload.get("status") if isinstance(payload, dict) else None
    return status if isinstance(status, str) else None


def _evidence_refs(event: dict[str, Any] | None) -> list[str]:
    """Return a bounded list of the evidence references behind a run's outcome.

    Only reference strings leave the run directory. Frames, payloads and camera
    content stay where they are: the index names evidence, it does not copy it.
    """
    payload = (event or {}).get("payload", {})
    refs = payload.get("evidence_refs") if isinstance(payload, dict) else None
    if not isinstance(refs, list):
        return []
    return sorted({ref for ref in refs if isinstance(ref, str) and ref})[:MAX_RUN_EVIDENCE_REFS]


def _manifest_sha256(
    run_id: str,
    events: list[dict[str, Any]],
    provenance: dict[str, Any] | None,
    manifests: dict[str, Path],
) -> str | None:
    """Bind a run to the manifest hash recorded for it, or report no binding.

    The provenance record is the binding this index exists to preserve once the
    metrics file is separated from the run directory, so its hash is preferred.
    A run with no record falls back to the manifest its own log declares, and a
    run that resolves neither reports no binding instead of borrowing another
    run's manifest.
    """
    entries = (provenance or {}).get("runs")
    entry = entries.get(run_id) if isinstance(entries, dict) else None
    entry = entry if isinstance(entry, dict) else {}
    if isinstance(entry.get("manifest_sha256"), str):
        return entry["manifest_sha256"]
    recorded = resolve_recorded_path(entry.get("manifest_path"))
    if recorded is not None and recorded.is_file():
        return file_sha256(recorded)
    manifest_path = manifests.get(str(_evaluation_of(events).get("scenario_id")))
    return file_sha256(manifest_path) if manifest_path is not None else None


def _ratio(numerator: int, denominator: int) -> dict[str, int]:
    return {"numerator": numerator, "denominator": denominator}


def _collision_count(events: list[dict[str, Any]]) -> int:
    return sum(
        event.get("event_type") == "fault" and event.get("payload", {}).get("fault_type") == "collision"
        for event in events
    )


def run_contributions(
    events: list[dict[str, Any]],
    *,
    run_family: str,
    duration: float | None,
    replay: tuple[str, str],
    audit_supplied: bool,
    audit_decision: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, str]]:
    """Return one run's contribution to every aggregate, and why one is absent.

    A metric this run cannot participate in is named with a reason, so a run
    present in the directory is reported as excluded rather than silently
    missing. The aggregates are rebuilt from these contributions, which is what
    makes "which run ids produced this number" answerable at all.
    """
    statuses = verification_statuses(events)
    final = _final_verification(events)
    payload = (final or {}).get("payload", {})
    payload = payload if isinstance(payload, dict) else {}
    contributions: dict[str, Any] = {}
    excluded: dict[str, str] = {}

    if statuses:
        contributions["vtcr"] = _ratio(int(statuses[-1] == "confirmed"), 1)
    else:
        excluded["vtcr"] = "no verification record"
    if audit_supplied:
        decision = audit_decision if isinstance(audit_decision, dict) else {}
        claimed = bool(statuses and statuses[-1] == "confirmed")
        contributions["false_completion_count"] = {
            "count": int(claimed and decision.get("oracle_status") != "confirmed")
        }
    else:
        excluded["false_completion_count"] = "no human false-completion audit was supplied"
    if len(statuses) > 1 and "refuted" in statuses[:-1]:
        contributions["recovery_rate"] = _ratio(int(statuses[-1] == "confirmed"), 1)
    else:
        excluded["recovery_rate"] = "no intermediate refuted verification"
    for metric in DURATION_METRICS:
        if duration is None:
            excluded[metric] = "fewer than two events, so no duration was measured"
        else:
            contributions[metric] = {"value": duration}

    requests = [event for event in events if event.get("event_type") == "action_request"]
    if requests:
        contributions["tool_call_validity"] = _ratio(
            sum(event.get("payload", {}).get("action_type") in ALLOWED_ACTIONS for event in requests), len(requests)
        )
    else:
        excluded["tool_call_validity"] = "no action_request"
    plans = [event for event in events if event.get("event_type") == "task_graph"]
    if plans:
        contributions["local_planning_coverage"] = _ratio(
            sum(event.get("payload", {}).get("model_route") in {"local", "template"} for event in plans), len(plans)
        )
    else:
        excluded["local_planning_coverage"] = "no task_graph"
    observations = [event for event in events if event.get("event_type") == "observation"]
    if observations:
        contributions["observation_completeness"] = _ratio(
            sum(all(field in event.get("payload", {}) for field in OBSERVATION_FIELDS) for event in observations),
            len(observations),
        )
    else:
        excluded["observation_completeness"] = "no observation"
    verifications = [event for event in events if event.get("event_type") == "verification"]
    if verifications:
        contributions["evidence_coverage"] = _ratio(
            sum(bool(event.get("payload", {}).get("evidence_refs")) for event in verifications), len(verifications)
        )
    else:
        excluded["evidence_coverage"] = "no verification"
    required = payload.get("required_conditions", [])
    evaluated = payload.get("evaluated_conditions", [])
    required_set = set(required) if isinstance(required, list) else set()
    evaluated_set = set(evaluated) if isinstance(evaluated, list) else set()
    if required_set:
        contributions["goal_condition_coverage"] = _ratio(len(required_set & evaluated_set), len(required_set))
    else:
        excluded["goal_condition_coverage"] = "the final verification required no condition"

    contributions["collision_count"] = {"count": _collision_count(events)}
    contributions["policy_violation_count"] = {
        "count": sum(event.get("event_type") == "policy_violation" for event in events)
    }
    contributions["state_hash_consistency"] = _ratio(int(replay[0] == replay[1]), 1)
    contributions["replay_success_rate"] = _ratio(1, 1)
    contributions["complex_task_rate"] = _ratio(int(run_family != BASELINE_TASK_FAMILY), 1)
    contributions["mean_observed_entities"] = _ratio(
        len(
            {
                event.get("payload", {}).get("entity_id")
                for event in observations
                if event.get("payload", {}).get("entity_id")
            }
        ),
        1,
    )
    contributions["task_family_count"] = {"value": run_family}
    return contributions, excluded


def build_evidence_index(
    runs: dict[str, list[dict[str, Any]]],
    *,
    provenance: dict[str, Any] | None,
    manifests: dict[str, Path],
    audit: dict[str, Any] | None,
    replayed: dict[str, tuple[str, str]],
) -> tuple[list[dict[str, Any]], dict[str, list[str]], dict[str, dict[str, list[str]]]]:
    """Build the bounded per-run index, its contributor sets and its exclusions."""
    if len(runs) > MAX_INDEXED_RUNS:
        raise RuntimeError(
            f"{len(runs)} runs exceed the bounded evidence index of {MAX_INDEXED_RUNS}; "
            "refusing to publish an index that cannot cover the run set it explains"
        )
    measured = run_durations(runs)
    decisions = (audit or {}).get("runs")
    decisions = decisions if isinstance(decisions, dict) else {}
    records: list[dict[str, Any]] = []
    contributors: dict[str, list[str]] = {metric: [] for metric in AGGREGATE_METRICS}
    exclusions: dict[str, dict[str, list[str]]] = {metric: {} for metric in AGGREGATE_METRICS}
    for run_id in sorted(runs):
        events = runs[run_id]
        final = _final_verification(events)
        contributions, excluded = run_contributions(
            events,
            run_family=task_id(events),
            duration=measured.get(run_id),
            replay=replayed[run_id],
            audit_supplied=audit is not None,
            audit_decision=decisions.get(run_id),
        )
        for metric, contribution in contributions.items():
            if contributes_to_numerator(metric, contribution):
                contributors[metric].append(run_id)
        for metric in AGGREGATE_METRICS:
            if metric not in contributions:
                exclusions[metric].setdefault(excluded[metric], []).append(run_id)
        records.append(
            {
                "run_id": run_id,
                "source_class": source_class(_evaluation_of(events).get("runner")),
                "task_family": task_id(events),
                "event_log_sha256": event_log_sha256(events),
                "manifest_sha256": _manifest_sha256(run_id, events, provenance, manifests),
                "final_outcome": _payload_status(final),
                "evidence_refs": _evidence_refs(final),
                "contributions": contributions,
                "excluded": dict(sorted(excluded.items())),
            }
        )
    return records, contributors, {metric: dict(sorted(reasons.items())) for metric, reasons in exclusions.items()}


def contributes_to_numerator(metric: str, contribution: dict[str, Any]) -> bool:
    """Report whether a run moved the numerator a reviewer would ask about.

    A count only names the runs that produced the event, and a ratio only names
    the runs that supplied a success: the runs that merely supplied a
    denominator are still in the rate, but they did not add to its top line.
    """
    mode = METRIC_AGGREGATION[metric]
    if mode == "sum":
        return bool(contribution.get("count"))
    if mode == "ratio":
        return bool(contribution.get("numerator"))
    return True


def aggregate_from_index(metric: str, records: list[dict[str, Any]]) -> Any:
    """Rebuild one published aggregate from the per-run contributions alone.

    This is the recomputation a reviewer performs: reading only ``runs[]``, the
    aggregate must come back identical, or the index does not explain it.
    """
    contributions = [record["contributions"][metric] for record in records if metric in record["contributions"]]
    if not contributions:
        # An explicit zero-contributor state: no run could evaluate this metric,
        # which is a missing measurement rather than a measured zero.
        if metric not in NO_CONTRIBUTOR_VALUE:
            raise RuntimeError(f"metric {metric!r} has no contributing run and no declared empty value")
        return NO_CONTRIBUTOR_VALUE[metric]
    mode = METRIC_AGGREGATION[metric]
    if mode == "sum":
        return sum(item["count"] for item in contributions)
    if mode == "ratio":
        denominator = sum(item["denominator"] for item in contributions)
        if not denominator:
            raise RuntimeError(f"metric {metric!r} rebuilds to an empty denominator")
        return sum(item["numerator"] for item in contributions) / denominator
    if mode == "sample":
        return percentile([item["value"] for item in contributions], SAMPLE_QUANTILE[metric])
    if mode == "distinct":
        return len({item["value"] for item in contributions})
    raise RuntimeError(f"metric {metric!r} declares an unknown aggregation {mode!r}")


def _same_aggregate(published: Any, rebuilt: Any) -> bool:
    if isinstance(published, bool) or isinstance(rebuilt, bool):
        return published is rebuilt
    if published is None or rebuilt is None:
        return published is None and rebuilt is None
    return published == rebuilt


def evidence_index_sha256(metrics: dict[str, Any]) -> str:
    """Digest the aggregates together with the canonically sorted run records.

    Reordering the input run files cannot change the digest, because the records
    are sorted by ``run_id`` and every key is sorted; editing a run id, a log
    hash, a manifest hash or a contribution does change it.
    """
    payload = {
        "format": EVIDENCE_INDEX_FORMAT,
        "format_version": EVIDENCE_INDEX_FORMAT_VERSION,
        "aggregates": {key: value for key, value in metrics.items() if key not in DIGEST_EXCLUDED_KEYS},
        "metric_contributors": metrics.get("metric_contributors", {}),
        "metric_exclusions": metrics.get("metric_exclusions", {}),
        "runs": sorted(metrics.get("runs", []), key=lambda record: record["run_id"]),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return sha256_bytes(canonical.encode("utf-8"))


def verify_evidence_index(metrics: dict[str, Any]) -> None:
    """Refuse an aggregate the per-run index cannot reproduce.

    A total without a run index is not evidence, so a consumer runs this before
    trusting a metrics file: an index that disagrees with its aggregate, names a
    contributor it does not hold, or carries a stale digest raises instead of
    being reported as a number.
    """
    records = metrics.get("runs")
    if not isinstance(records, list) or not records:
        raise RuntimeError("refusing to publish aggregate metrics without a per-run evidence index")
    run_ids = [record.get("run_id") for record in records]
    if run_ids != sorted(set(run_ids)):
        raise RuntimeError("evidence index run records are not uniquely and canonically ordered")
    for metric in AGGREGATE_METRICS:
        rebuilt = aggregate_from_index(metric, records)
        if not _same_aggregate(rebuilt, metrics.get(metric)):
            raise RuntimeError(
                f"metric {metric!r} is {metrics.get(metric)!r} but its per-run index rebuilds to {rebuilt!r}"
            )
        expected = [
            record["run_id"]
            for record in records
            if metric in record["contributions"] and contributes_to_numerator(metric, record["contributions"][metric])
        ]
        if metrics.get("metric_contributors", {}).get(metric) != expected:
            raise RuntimeError(f"metric {metric!r} names contributors its run records do not support")
    if evidence_index_sha256(metrics) != metrics.get("evidence_index_sha256"):
        raise RuntimeError("evidence index digest does not match its own content")


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
    # Every run must replay through the canonical reducer before any metric is
    # published, so a malformed, ambiguous or unreplayable run aborts the
    # collection instead of being scored as a partial success.
    replayed = {run_id: canonical_replay(run_id, run) for run_id, run in runs.items()}
    valid_replays = len(replayed)
    stable_hashes = sum(forward == permuted for forward, permuted in replayed.values())
    false_completions, audit_complete, reviewed_by = audit_false_completions(runs, audit_path)
    # Eligibility is recomputed from the logs and the provenance record. The
    # summary is consulted only to detect a disagreement, never to grant a pass.
    provenance_path = run_dir.parent / "provenance.json"
    provenance = None
    if provenance_path.is_file():
        try:
            provenance = load_provenance(provenance_path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            provenance = None
    run_task_ids = {run_id: task_id(run) for run_id, run in runs.items()}
    task_family_distribution = Counter(run_task_ids.values())
    task_family_vtcr = {}
    for family, family_run_count in sorted(task_family_distribution.items()):
        family_runs = [run for run_id, run in runs.items() if run_task_ids[run_id] == family]
        # A run without a verification record has not been verified, so it must
        # not crash the family aggregate or silently count as confirmed.
        family_verified = sum(
            bool(verification_statuses(run)) and verification_statuses(run)[-1] == "confirmed" for run in family_runs
        )
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
    audit = load_human_audit(audit_path)
    manifests = discover_manifests(manifest_search_dirs(run_dir))
    # Eligibility is recomputed from the logs and the provenance record. The
    # summary is consulted only to detect a disagreement, never to grant a pass.
    verdict = evaluate_eligibility(runs=runs, provenance=provenance, manifests=manifests, audit=audit)
    summary_claims = bool(summary.get("release_eligible", False))
    if summary_claims and not verdict["eligible"]:
        verdict = {
            **verdict,
            "reasons": sorted([*verdict["reasons"], "summary claims eligibility that the evidence does not support"]),
        }
    if verdict["eligible"] and summary and not summary_claims:
        verdict = {
            **verdict,
            "reasons": sorted([*verdict["reasons"], "summary does not claim the eligibility the evidence supports"]),
            "eligible": False,
        }
    # The aggregates above are computed from the events directly; the index
    # below recomputes each of them from its own per-run contributions. The two
    # are then compared by ``verify_evidence_index``, so a contribution table
    # that does not reproduce the published number is refused rather than
    # published as an explanation of it.
    records, contributors, exclusions = build_evidence_index(
        runs,
        provenance=provenance,
        manifests=manifests,
        audit=audit,
        replayed=replayed,
    )
    metrics = {
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
        "runner": _declared_runner(runs, summary),
        "provenance_present": provenance is not None,
        "release_eligible": bool(verdict["eligible"]),
        "eligibility_reasons": verdict["reasons"],
        "run_index_format": EVIDENCE_INDEX_FORMAT,
        "run_index_format_version": EVIDENCE_INDEX_FORMAT_VERSION,
        "runs": records,
        "metric_contributors": contributors,
        "metric_exclusions": exclusions,
    }
    metrics["evidence_index_sha256"] = evidence_index_sha256(metrics)
    verify_evidence_index(metrics)
    return metrics


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
