#!/usr/bin/env python3
"""Run reproducible scenario evaluations without confusing fixtures for evidence."""

import argparse
import json
import os
import platform
import re
import shlex
import signal
import subprocess
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from _jsonio import JsonInputError, iter_jsonl, load_json
from _paths import enable_local_packages

enable_local_packages()

from release_eligibility import build_provenance, evaluate_eligibility
from scenario_tools import TASK_PROFILES, materialize_scenario, validate_simulation_manifest
from workbench.application.redaction import (
    REDACTION_MARKER_KEY,
    REDACTION_RULES_VERSION,
    redact_mapping,
    redact_text,
)
from workbench_agent_runtime import build_policy_routed_parcel_plan

SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
EVENT_TYPES = {
    "action_request",
    "action_result",
    "emotion",
    "fault",
    "observation",
    "policy_violation",
    "task_accepted",
    "task_graph",
    "task_terminal",
    "verification",
}
VERIFICATION_STATUSES = {"confirmed", "insufficient_evidence", "refuted"}
MAX_EVENT_LOG_BYTES = 10 * 1024 * 1024
MAX_EVENTS_PER_RUN = 10_000
TIMESTAMP_WINDOW_SECONDS = 365 * 24 * 60 * 60
# Startup grace covers interpreter and adapter import time before the scenario
# itself begins; it is added to the manifest budget rather than replacing it.
EXTERNAL_STARTUP_GRACE_SECONDS = 30
EXTERNAL_TERMINATION_GRACE_SECONDS = 5.0


class EvaluationInputError(ValueError):
    """Raised before execution when an evaluation input is unsafe or ambiguous."""


class ExternalRunnerTimeout(RuntimeError):
    """Raised when an external runner exceeds its manifest-derived budget."""

    def __init__(self, message: str, *, scenario_id: str, elapsed_s: float, budget_s: int) -> None:
        super().__init__(message)
        self.scenario_id = scenario_id
        self.elapsed_s = elapsed_s
        self.budget_s = budget_s


def parse_rfc3339(value: Any, *, field: str, path: Path, line_number: int, event_id: Any) -> datetime:
    """Parse an explicit timezone-aware RFC 3339 timestamp.

    Naive or malformed values are rejected instead of being compared as if the
    writer and reader shared a clock: a naive timestamp cannot be ordered
    against an aware one, and silently assuming UTC turns an unknown offset into
    a plausible-looking duration.
    """
    location = f"{path}:{line_number} event {event_id!r}"
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"{location}: missing {field}")
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise RuntimeError(f"{location}: malformed {field}: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{location}: timezone-naive {field}: {value!r}")
    return parsed


def validate_label(value: str, field: str) -> str:
    if not isinstance(value, str) or not SAFE_LABEL.fullmatch(value):
        raise EvaluationInputError(f"{field} must be a filesystem-safe label: {value!r}")
    return value


def load_scenario_manifests(paths: list[Path]) -> list[tuple[Path, dict[str, Any]]]:
    manifests: list[tuple[Path, dict[str, Any]]] = []
    seen_ids: dict[str, Path] = {}
    for path in paths:
        try:
            payload = load_json(path)
            validate_simulation_manifest(payload)
        except (JsonInputError, OSError, UnicodeError, ValueError) as exc:
            raise EvaluationInputError(f"invalid scenario manifest {path}: {exc}") from exc
        scenario_id = validate_label(payload["scenario_id"], "scenario_id")
        if scenario_id in seen_ids:
            raise EvaluationInputError(f"duplicate scenario_id {scenario_id!r} in {seen_ids[scenario_id]} and {path}")
        seen_ids[scenario_id] = path
        manifests.append((path, payload))
    return manifests


def git_commit() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            check=False,
            text=True,
        )
    except OSError:
        return "unknown"
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def write_jsonl(path: Path, events: list[dict[str, Any]]) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text("".join(f"{json.dumps(event, sort_keys=True)}\n" for event in events), encoding="utf-8")
    temporary.replace(path)


def scripted_events(version: str, manifest: dict[str, Any], commit: str, seed_base: int) -> list[dict[str, Any]]:
    """Produce deterministic multi-task fixtures for pipeline and UI tests only."""
    scenario_id = manifest["scenario_id"]
    task_id = manifest["task_id"]
    profile = TASK_PROFILES.get(task_id)
    if profile is None:
        raise EvaluationInputError(f"unsupported scripted task_id: {task_id}")
    scene = materialize_scenario(manifest)
    operations = tuple(profile["operations"])
    policy_plan = None
    policy_place_parameters: dict[str, dict[str, Any]] = {}
    policy_manifest_statuses: dict[str, str] = {}
    if task_id == "task-sort-parcels":
        policy_plan = build_policy_routed_parcel_plan(
            profile["goal"],
            {item["entity_id"]: item["attributes"] for item in scene["objects"]},
            destination_capacities={"pickup_shelf": 4, "quarantine_bin": 4},
            parcel_manifest=profile["parcel_manifest"],
            manifest_id=profile["manifest_id"],
        )
        operations = tuple(
            (step.action.target_id, step.action.parameters["destination_id"])
            for step in policy_plan.steps
            if step.action.action_type.value == "place"
        )
        policy_place_parameters = {
            step.action.target_id: dict(step.action.parameters)
            for step in policy_plan.steps
            if step.action.action_type.value == "place"
        }
        policy_manifest_statuses = {
            entity_id: parameters["manifest_guard"] for entity_id, parameters in policy_place_parameters.items()
        }
    run_id = f"{version}--{scenario_id}"
    effective_seed = seed_base + manifest["seed"]
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=effective_seed % TIMESTAMP_WINDOW_SECONDS)
    events: list[dict[str, Any]] = []
    action_index = 0

    def append(event_type: str, payload: dict[str, Any], evidence_refs: list[str] | None = None) -> None:
        sequence_no = len(events)
        events.append(
            {
                "event_id": f"{run_id}-evt-{sequence_no:03d}",
                "run_id": run_id,
                "sequence_no": sequence_no,
                "event_type": event_type,
                "occurred_at": (start + timedelta(seconds=sequence_no * 4)).isoformat().replace("+00:00", "Z"),
                "payload": payload,
                "evidence_refs": evidence_refs or [],
                "evaluation": {
                    "commit": commit,
                    "scenario_id": scenario_id,
                    "seed": effective_seed,
                    "runner": "scripted",
                    "task_id": task_id,
                    "scene_variant": scene["scene_variant"],
                },
            }
        )

    def next_action_id() -> str:
        nonlocal action_index
        action_index += 1
        return f"act-{action_index:03d}"

    def append_intermediate_failure(detail: str, evidence_refs: list[str]) -> None:
        append(
            "verification",
            {
                "verification_id": f"{run_id}-verify-recovery-{len(events):03d}",
                "task_id": task_id,
                "claim": profile["claim"],
                "status": "refuted",
                "reason_code": "attempt_failed",
                "recovery_hint": "retry_action",
                "missing_evidence": [],
                "required_conditions": list(profile["required_conditions"]),
                "evaluated_conditions": list(profile["required_conditions"]),
                "satisfied_conditions": [],
                "detail": detail,
                "evidence_refs": evidence_refs,
            },
            evidence_refs,
        )

    append(
        "task_accepted",
        {
            "task_id": task_id,
            "goal": profile["goal"],
            "version": version,
            "mode": f"scripted · {scene['scene_variant']}",
            "required_entities": list(profile["entities"]),
        },
    )
    graph_actions = (
        [
            f"{step.action.action_type.value}:{step.action.target_id}"
            + (f"->{step.action.parameters['destination_id']}" if step.action.action_type.value == "place" else "")
            for step in policy_plan.steps
        ]
        if policy_plan is not None
        else [f"observe:{entity_id}" for entity_id in profile["entities"]]
    )
    if policy_plan is None:
        graph_actions.extend(
            action
            for entity_id, destination_id in operations
            for action in (f"grasp:{entity_id}", f"place:{entity_id}->{destination_id}")
        )
    append(
        "task_graph",
        {
            "task_id": task_id,
            "planner": (
                "template-v1"
                if task_id == "task-place-red-block"
                else policy_plan.planner
                if task_id == "task-sort-parcels"
                else "template-v2"
            ),
            "model_route": "template",
            "actions": graph_actions,
            "parallel_branches": len(profile["entities"]),
            "observation_barrier": task_id == "task-sort-parcels",
            "manipulation_serial": task_id == "task-sort-parcels",
            "routing_policy": "manifest_matched_verified_intact_only" if task_id == "task-sort-parcels" else None,
            "policy_version": "parcel-routing-v3" if task_id == "task-sort-parcels" else None,
            "manifest_id": profile.get("manifest_id"),
            "manifest_statuses": policy_manifest_statuses or None,
            "routing_priorities": (
                {
                    step.action.target_id: step.action.parameters["routing_priority"]
                    for step in policy_plan.steps
                    if step.action.action_type.value == "place"
                }
                if policy_plan is not None
                else None
            ),
            "destination_capacities": ({"pickup_shelf": 4, "quarantine_bin": 4} if policy_plan is not None else None),
            "destination_occupancy": ({"pickup_shelf": 0, "quarantine_bin": 0} if policy_plan is not None else None),
        },
    )

    fault_type = manifest["fault_type"]
    low_evidence_fault = fault_type in {"occlusion", "camera_dropout", "stale_observation"}
    recoverable_fault = fault_type in {"grasp_failure", "actuator_timeout"}
    evidence: list[str] = []
    recovery_injected = False
    observation_attributes = (
        next(
            list(step.action.parameters["attributes"])
            for step in policy_plan.steps
            if step.action.action_type.value == "observe"
        )
        if policy_plan is not None
        else [
            "presence",
            "identity",
            "orientation",
        ]
    )

    for object_index, scene_object in enumerate(scene["objects"]):
        entity_id = scene_object["entity_id"]
        observe_action_id = next_action_id()
        append(
            "action_request",
            {
                "action_id": observe_action_id,
                "action_type": "observe",
                "target_id": entity_id,
                "attributes": observation_attributes,
            },
        )
        if not operations and recoverable_fault and object_index == 0:
            failure_ref = f"sensor-log://{run_id}/{entity_id}/attempt-1"
            append(
                "action_result",
                {"action_id": observe_action_id, "status": "failed", "detail": fault_type},
                [failure_ref],
            )
            append_intermediate_failure(fault_type, [failure_ref])
            observe_action_id = next_action_id()
            append(
                "action_request",
                {
                    "action_id": observe_action_id,
                    "action_type": "observe",
                    "target_id": entity_id,
                    "attributes": observation_attributes,
                    "attempt": 2,
                },
            )
            recovery_injected = True
        confidence = (
            0.42 if low_evidence_fault and object_index == len(scene["objects"]) - 1 else 0.97 - 0.02 * object_index
        )
        frame_ref = f"frame://{run_id}/{entity_id}"
        evidence.append(frame_ref)
        append(
            "observation",
            {
                "observation_id": f"{run_id}-obs-{object_index + 1:03d}",
                "run_id": run_id,
                "entity_id": entity_id,
                "entity_type": scene_object["entity_type"],
                "colour": scene_object["colour"],
                "attributes": scene_object.get("attributes", {}),
                "location": "on:table",
                "pose": {
                    "frame_id": "world",
                    "position": {"x": scene_object["x"], "y": scene_object["y"], "z": 0.02},
                    "yaw": scene_object["yaw"],
                },
                "confidence": confidence,
            },
            [frame_ref],
        )
        append(
            "action_result",
            {"action_id": observe_action_id, "status": "succeeded", "entity_id": entity_id},
            [frame_ref],
        )

    operation_failed = False
    for operation_index, (entity_id, destination_id) in enumerate(operations):
        grasp_action_id = next_action_id()
        append(
            "action_request",
            {"action_id": grasp_action_id, "action_type": "grasp", "target_id": entity_id},
        )
        should_fail_grasp = operation_index == 0 and fault_type in {"grasp_failure", "moving_target"}
        if should_fail_grasp:
            failure_ref = f"motion-log://{run_id}/{entity_id}/grasp-attempt-1"
            append(
                "action_result",
                {
                    "action_id": grasp_action_id,
                    "status": "failed",
                    "detail": "target moved" if fault_type == "moving_target" else fault_type,
                },
                [failure_ref],
            )
            evidence.append(failure_ref)
            if fault_type == "moving_target":
                operation_failed = True
                break
            append_intermediate_failure(fault_type, [failure_ref])
            grasp_action_id = next_action_id()
            append(
                "action_request",
                {
                    "action_id": grasp_action_id,
                    "action_type": "grasp",
                    "target_id": entity_id,
                    "attempt": 2,
                },
            )
            recovery_injected = True
        grasp_ref = f"motion-log://{run_id}/{entity_id}/grasp-final"
        evidence.append(grasp_ref)
        append(
            "action_result",
            {"action_id": grasp_action_id, "status": "succeeded", "entity_id": entity_id},
            [grasp_ref],
        )

        place_action_id = next_action_id()
        append(
            "action_request",
            {
                "action_id": place_action_id,
                "action_type": "place",
                "target_id": entity_id,
                "destination_id": destination_id,
                **policy_place_parameters.get(entity_id, {}),
            },
        )
        if operation_index == 0 and fault_type == "actuator_timeout":
            failure_ref = f"motion-log://{run_id}/{entity_id}/place-attempt-1"
            append(
                "action_result",
                {"action_id": place_action_id, "status": "failed", "detail": fault_type},
                [failure_ref],
            )
            evidence.append(failure_ref)
            append_intermediate_failure(fault_type, [failure_ref])
            place_action_id = next_action_id()
            append(
                "action_request",
                {
                    "action_id": place_action_id,
                    "action_type": "place",
                    "target_id": entity_id,
                    "destination_id": destination_id,
                    "attempt": 2,
                    **policy_place_parameters.get(entity_id, {}),
                },
            )
            recovery_injected = True
        place_ref = f"motion-log://{run_id}/{entity_id}/place-final"
        evidence.append(place_ref)
        append(
            "action_result",
            {
                "action_id": place_action_id,
                "status": "succeeded",
                "entity_id": entity_id,
                "resulting_location": f"in:{destination_id}",
            },
            [place_ref],
        )

    if low_evidence_fault:
        status = "insufficient_evidence"
        reason_code = "stale_observation" if fault_type == "stale_observation" else "confidence_below_threshold"
    elif fault_type == "moving_target" or operation_failed:
        status = "refuted"
        reason_code = "target_changed"
    else:
        status = "confirmed"
        reason_code = "goal_satisfied"

    evidence = list(dict.fromkeys(evidence))
    missing_evidence = (
        [f"fresh_camera_frame:{profile['entities'][-1]}", "confidence>=0.8"]
        if status == "insufficient_evidence"
        else []
    )
    required_conditions = list(profile["required_conditions"])
    satisfied_conditions = required_conditions if status == "confirmed" else required_conditions[:-1]
    if status == "refuted":
        satisfied_conditions = []
    append(
        "verification",
        {
            "verification_id": f"{run_id}-verify-final",
            "task_id": task_id,
            "claim": profile["claim"],
            "status": status,
            "reason_code": reason_code,
            "recovery_hint": (
                "re_observe" if status == "insufficient_evidence" else "none" if status == "confirmed" else "replan"
            ),
            "missing_evidence": missing_evidence,
            "required_conditions": required_conditions,
            "evaluated_conditions": required_conditions,
            "satisfied_conditions": satisfied_conditions,
            "recovery_performed": recovery_injected,
            "manifest_id": profile.get("manifest_id"),
            "manifest_statuses": policy_manifest_statuses or None,
            "evidence_refs": evidence,
        },
        evidence,
    )
    append("task_terminal", {"task_id": task_id, "status": status}, evidence)
    return events


def validate_event_log(
    path: Path,
    run_id: str,
    *,
    scenario_id: str | None = None,
    seed: int | None = None,
    commit: str | None = None,
) -> list[dict[str, Any]]:
    try:
        if path.stat().st_size > MAX_EVENT_LOG_BYTES:
            raise RuntimeError(f"event log exceeds {MAX_EVENT_LOG_BYTES} bytes: {path}")
        events = []
        line_numbers = []
        for line_number, event in iter_jsonl(path):
            events.append(event)
            line_numbers.append(line_number)
    except RuntimeError:
        raise
    except JsonInputError as exc:
        # JsonInputError already carries the file, line and duplicate field name.
        raise RuntimeError(f"runner produced an unreadable JSONL event log: {exc}") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"runner produced an unreadable JSONL event log: {path}: {exc}") from exc
    if not events:
        raise RuntimeError(f"runner produced an empty event log: {path}")
    if len(events) > MAX_EVENTS_PER_RUN:
        raise RuntimeError(f"runner produced more than {MAX_EVENTS_PER_RUN} events: {path}")
    if any(not isinstance(event, dict) for event in events):
        raise RuntimeError(f"event log contains a non-object event: {path}")
    sequences = [event.get("sequence_no") for event in events]
    if any(type(sequence) is not int for sequence in sequences) or sequences != list(range(len(events))):
        raise RuntimeError(f"non-contiguous sequence_no values in {path}: {sequences}")
    if any(event.get("run_id") != run_id for event in events):
        raise RuntimeError(f"run_id drift in {path}")
    event_ids = [event.get("event_id") for event in events]
    if any(not isinstance(event_id, str) or not event_id for event_id in event_ids):
        raise RuntimeError(f"missing event_id in {path}")
    if len(event_ids) != len(set(event_ids)):
        raise RuntimeError(f"duplicate event_id in {path}")
    for index, event in enumerate(events):
        event_type = event.get("event_type")
        if not isinstance(event_type, str) or event_type not in EVENT_TYPES:
            raise RuntimeError(f"unknown event_type in {path}: {event_type!r}")
        parse_rfc3339(
            event.get("occurred_at"),
            field="occurred_at",
            path=path,
            line_number=line_numbers[index],
            event_id=event.get("event_id"),
        )
        if not isinstance(event.get("payload"), dict):
            raise RuntimeError(f"event payload is not an object in {path}")
        evidence_refs = event.get("evidence_refs", [])
        if not isinstance(evidence_refs, list) or any(not isinstance(ref, str) for ref in evidence_refs):
            raise RuntimeError(f"event evidence_refs is not a string list in {path}")
        evaluation = event.get("evaluation")
        if not isinstance(evaluation, dict):
            raise RuntimeError(f"event is missing evaluation metadata in {path}")
        if not isinstance(evaluation.get("commit"), str) or not evaluation["commit"]:
            raise RuntimeError(f"event is missing evaluation commit in {path}")
        if not isinstance(evaluation.get("scenario_id"), str) or not evaluation["scenario_id"]:
            raise RuntimeError(f"event is missing evaluation scenario_id in {path}")
        if type(evaluation.get("seed")) is not int:
            raise RuntimeError(f"event is missing integer evaluation seed in {path}")
        if scenario_id is not None and evaluation["scenario_id"] != scenario_id:
            raise RuntimeError(f"evaluation scenario_id drift in {path}")
        if seed is not None and evaluation["seed"] != seed:
            raise RuntimeError(f"evaluation seed drift in {path}")
        if commit is not None and evaluation["commit"] != commit:
            raise RuntimeError(f"evaluation commit drift in {path}")
    verifications = [event for event in events if event.get("event_type") == "verification"]
    if not verifications:
        raise RuntimeError(f"event log contains no verification result: {path}")
    if any(
        not isinstance(event["payload"].get("status"), str)
        or event["payload"].get("status") not in VERIFICATION_STATUSES
        for event in verifications
    ):
        raise RuntimeError(f"verification contains an unknown status in {path}")
    for event in verifications:
        event_evidence = event.get("evidence_refs")
        payload_evidence = event["payload"].get("evidence_refs")
        if (
            not isinstance(event_evidence, list)
            or not event_evidence
            or any(not isinstance(reference, str) for reference in event_evidence)
            or not isinstance(payload_evidence, list)
            or not payload_evidence
            or any(not isinstance(reference, str) for reference in payload_evidence)
        ):
            raise RuntimeError(f"verification without evidence_refs in {path}")
        condition_sets: dict[str, set[str]] = {}
        for field in ("required_conditions", "evaluated_conditions", "satisfied_conditions"):
            values = event["payload"].get(field)
            if (
                not isinstance(values, list)
                or any(not isinstance(value, str) or not value for value in values)
                or len(values) != len(set(values))
            ):
                raise RuntimeError(f"verification {field} must be a unique string list in {path}")
            condition_sets[field] = set(values)
        required = condition_sets["required_conditions"]
        evaluated = condition_sets["evaluated_conditions"]
        satisfied = condition_sets["satisfied_conditions"]
        if not required or not satisfied.issubset(evaluated) or not evaluated.issubset(required):
            raise RuntimeError(f"verification condition accounting is inconsistent in {path}")
        if event["payload"]["status"] == "confirmed" and satisfied != required:
            raise RuntimeError(f"confirmed verification has unsatisfied conditions in {path}")
    terminals = [event for event in events if event.get("event_type") == "task_terminal"]
    if terminals and terminals[-1]["payload"].get("status") != verifications[-1]["payload"].get("status"):
        raise RuntimeError(f"terminal status disagrees with final verification in {path}")
    return events


def external_timeout_budget(manifest: dict[str, Any]) -> int:
    """Derive the runner wall-clock budget from the manifest plus startup grace."""
    timeout_s = manifest.get("timeout_s")
    if not isinstance(timeout_s, int) or isinstance(timeout_s, bool) or timeout_s <= 0:
        raise EvaluationInputError(f"scenario manifest timeout_s must be a positive integer: {timeout_s!r}")
    return timeout_s + EXTERNAL_STARTUP_GRACE_SECONDS


def _terminate_process_group(process: subprocess.Popen[Any]) -> None:
    """Stop a runner and its descendants deterministically on Linux and Windows."""
    if process.poll() is None:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.terminate()
                except OSError:
                    pass
    try:
        process.wait(timeout=EXTERNAL_TERMINATION_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                text=True,
            )
        else:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            process.wait(timeout=EXTERNAL_TERMINATION_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def run_external(
    command_template: str,
    manifest_path: Path,
    output_path: Path,
    seed: int,
    version: str,
    *,
    timeout_s: int,
    scenario_id: str,
) -> None:
    substitutions = {
        "manifest": str(manifest_path.resolve()),
        "output": str(output_path.resolve()),
        "seed": str(seed),
        "version": version,
    }
    try:
        command = command_template.format(**substitutions)
    except (KeyError, ValueError) as exc:
        raise EvaluationInputError(f"invalid runner command template: {exc}") from exc
    argv: Sequence[str] = shlex.split(command, posix=os.name != "nt")
    started = time.monotonic()
    try:
        creation_flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
        process = subprocess.Popen(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=os.name != "nt",
            creationflags=creation_flags,
        )
    except OSError as exc:
        raise RuntimeError(f"external runner could not start: {exc}") from exc
    try:
        _stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _terminate_process_group(process)
        elapsed = time.monotonic() - started
        raise ExternalRunnerTimeout(
            redact_text(
                f"external runner for {scenario_id} timed out after {elapsed:.1f}s "
                f"(budget {timeout_s}s including {EXTERNAL_STARTUP_GRACE_SECONDS}s startup grace); "
                f"command: {command}"
            ),
            scenario_id=scenario_id,
            elapsed_s=elapsed,
            budget_s=timeout_s,
        ) from None
    if process.returncode != 0:
        # A runner that fails inside a credentialed command echoes the credential
        # into stderr; this message ends up in a traceback or a report, so it is
        # scrubbed at the point where it is built.
        raise RuntimeError(redact_text(f"external runner failed ({process.returncode}): {(stderr or '').strip()}"))


def redacted_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """Return the publishable form of the run summary.

    The summary is the artifact a release reviewer reads, and it embeds
    ``blocked_detail`` text from a failing runner.  It is scrubbed as a whole
    rather than field by field so a new diagnostic field cannot be added later
    without also being covered.
    """
    sanitized, findings = redact_mapping(summary)
    if findings:
        sanitized[REDACTION_MARKER_KEY] = {"rules": REDACTION_RULES_VERSION, "redacted_values": findings}
    return sanitized


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run reproducible Workbench-1 evaluations")
    parser.add_argument("--versions", required=True, help="Comma-separated version labels")
    parser.add_argument("--scenarios", nargs="+", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--seed-base", type=int, default=1000)
    parser.add_argument("--runner", choices=("scripted", "external"), default="scripted")
    parser.add_argument(
        "--runner-command", help="External command template with {manifest}, {output}, {seed}, {version}"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.runner == "external" and not args.runner_command:
        raise EvaluationInputError("--runner-command is required for the external runner")
    versions = [version.strip() for version in args.versions.split(",") if version.strip()]
    scenarios = sorted(args.scenarios)
    if not versions or not scenarios:
        raise EvaluationInputError("at least one version and scenario are required")
    for version in versions:
        validate_label(version, "version")
    if len(versions) != len(set(versions)):
        raise EvaluationInputError("version labels must be unique")
    manifests = load_scenario_manifests(scenarios)

    commit = git_commit()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    summaries: list[dict[str, Any]] = []
    # Provenance is built from the validated logs themselves, so it can be
    # recomputed and checked later instead of being read back from a summary.
    accepted_logs: dict[str, list[dict[str, Any]]] = {}
    manifest_paths: dict[str, Path] = {
        str(manifest["scenario_id"]): scenario_path for scenario_path, manifest in manifests
    }
    print(f"Running {len(versions)} version(s) x {len(scenarios)} scenario(s) with {args.runner} runner")

    for version in versions:
        version_dir = args.output_dir / version
        version_dir.mkdir(exist_ok=True)
        for scenario_path, manifest in manifests:
            run_id = f"{version}--{manifest['scenario_id']}"
            output_path = version_dir / f"{manifest['scenario_id']}.jsonl"
            effective_seed = args.seed_base + manifest["seed"]
            if args.runner == "scripted":
                write_jsonl(output_path, scripted_events(version, manifest, commit, args.seed_base))
                events = validate_event_log(
                    output_path,
                    run_id,
                    scenario_id=manifest["scenario_id"],
                    seed=effective_seed,
                    commit=commit,
                )
                accepted_logs[run_id] = events
            else:
                partial_path = output_path.with_name(f".{output_path.name}.partial")
                partial_path.unlink(missing_ok=True)
                try:
                    run_external(
                        args.runner_command,
                        scenario_path,
                        partial_path,
                        effective_seed,
                        version,
                        timeout_s=external_timeout_budget(manifest),
                        scenario_id=manifest["scenario_id"],
                    )
                    events = validate_event_log(
                        partial_path,
                        run_id,
                        scenario_id=manifest["scenario_id"],
                        seed=effective_seed,
                        commit=commit,
                    )
                    accepted_logs[run_id] = events
                    partial_path.replace(output_path)
                except ExternalRunnerTimeout as exc:
                    summaries.append(
                        {
                            "run_id": run_id,
                            "version": version,
                            "scenario_id": manifest["scenario_id"],
                            "seed": effective_seed,
                            "commit": commit,
                            "runner": args.runner,
                            "event_log": None,
                            "verification_status": "blocked",
                            "release_eligible": False,
                            "eligibility_reasons": ["the runner was blocked; no evidence was produced"],
                            "blocked_reason": "runner_timeout",
                            "blocked_detail": str(exc),
                            "elapsed_s": round(exc.elapsed_s, 3),
                            "timeout_s": exc.budget_s,
                        }
                    )
                    print(f"  {run_id}: blocked (runner_timeout after {exc.elapsed_s:.1f}s)")
                    continue
                finally:
                    # A timed out or failed runner must never leave promotable output.
                    partial_path.unlink(missing_ok=True)
            final_verification = [event for event in events if event["event_type"] == "verification"][-1]
            summaries.append(
                {
                    "run_id": run_id,
                    "version": version,
                    "scenario_id": manifest["scenario_id"],
                    "seed": effective_seed,
                    "commit": commit,
                    "runner": args.runner,
                    "event_log": str(output_path),
                    "verification_status": final_verification["payload"]["status"],
                }
            )
            print(f"  {run_id}: {summaries[-1]['verification_status']}")

    blocked = [entry for entry in summaries if entry["verification_status"] == "blocked"]
    provenance = build_provenance(
        runner=args.runner,
        commit=commit,
        environment={
            "platform": platform.platform(),
            "python": platform.python_version(),
            "machine": platform.machine(),
        },
        manifests=manifest_paths,
        logs=accepted_logs,
    )
    # The summary repeats the verdict; it never establishes it. A reader can
    # recompute the same answer with check_evaluation_eligibility.py.
    verdict = evaluate_eligibility(runs=accepted_logs, provenance=provenance, manifests=manifest_paths)
    for entry in summaries:
        run_verdict = verdict["per_run"].get(entry["run_id"], {})
        entry["release_eligible"] = bool(run_verdict.get("eligible", False))
        entry["eligibility_reasons"] = list(run_verdict.get("reasons", []))
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "commit": commit,
        "runner": args.runner,
        "blocked_count": len(blocked),
        "release_eligible": verdict["eligible"] and not blocked,
        "eligibility_reasons": verdict["reasons"],
        "run_count": len(summaries),
        "runs": summaries,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(redacted_summary(summary), indent=2), encoding="utf-8")
    (args.output_dir / "provenance.json").write_text(
        json.dumps(redacted_summary(provenance), indent=2, sort_keys=True), encoding="utf-8"
    )
    if blocked:
        print(f"{len(blocked)} run(s) blocked; no release evidence was produced for them.")
    if args.runner == "scripted":
        print("Scripted fixtures completed. These runs test the pipeline and are not release evidence.")
    validated_count = len(summaries) - len(blocked)
    print(f"Wrote {validated_count} validated event log(s) to {args.output_dir}")
    return 1 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
