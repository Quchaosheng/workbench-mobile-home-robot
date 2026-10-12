"""Ingest the ROS 2 runtime's terminal ``TaskEvent`` into the Workbench evidence chain.

The runtime's own README states what a TaskEvent proves: an Action terminal
status and a protocol outcome, not that an actuator moved. So this adapter
records *execution* evidence (``action_result``) for every terminal status the
runtime can report, including a device fault, and never a verification result.
A TaskEvent is therefore not a completion claim; the World Model verifier still
decides that from observations.

Transport is a captured JSONL file rather than a live DDS subscription, because
ROS 2 is not a Workbench dependency: a node or a ``ros2 topic echo`` run writes
the lines, and this module only reads them. The three correlation fields must be
present, which means the capture has to come from a runtime build that carries
``request_id``, ``workflow_id`` and ``goal_id`` on ``TaskEvent``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from workbench_contracts import ActionOutcome, ActionResult, DeviceState, DispatchState, WorldEventType
from workbench_world_model.event_store import EventStoreIntegrityError, SQLiteEventStore

# ``TaskEvent.action_status`` values published by task_executor.
SUCCEEDED = 1
ABORTED = 2
CANCELED = 3

# ``ExecuteTask::Result`` outcome -> Workbench outcome and whether the device
# answered. The runtime reports a protocol outcome only, so nothing here is
# transcribed as a confirmed physical action.
_OUTCOMES: dict[int, tuple[ActionOutcome, DeviceState]] = {
    0: (ActionOutcome.COMPLETED, DeviceState.CONFIRMED),
    1: (ActionOutcome.CANCELED, DeviceState.STOPPED),
    2: (ActionOutcome.SAFE_STOP, DeviceState.STOPPED),
    3: (ActionOutcome.FAILED, DeviceState.REJECTED),
}

_CORRELATION_KEYS = ("request_id", "workflow_id", "goal_id")
_REQUIRED_KEYS = (
    "stamp",
    "task_id",
    "target_id",
    "action_status",
    "outcome",
    "error_code",
    "duration_ms",
    "message",
    *_CORRELATION_KEYS,
)


class TaskEventRejected(ValueError):
    """The record cannot be bound to a Workbench run and must not be written."""


@dataclass(frozen=True, slots=True)
class TaskEventIngestResult:
    """What one accepted line was bound to. Not a completion claim."""

    line_number: int
    run_id: str
    action_id: str
    event_id: str


def bind_task_event(record: object, *, line_number: int = 1) -> tuple[str, ActionResult]:
    """Validate one TaskEvent record and bind it to a Workbench run and action."""

    if not isinstance(record, dict):
        raise TaskEventRejected(f"line {line_number}: TaskEvent must be a JSON object")
    for name in _REQUIRED_KEYS:
        if name not in record:
            raise TaskEventRejected(f"line {line_number}: TaskEvent is missing {name}")

    parts = {name: _text(record[name], name, line_number) for name in ("task_id", "target_id", "message")}
    parts.update({name: _text(record[name], name, line_number) for name in _CORRELATION_KEYS})
    status = _number(record["action_status"], "action_status", line_number)
    outcome = _number(record["outcome"], "outcome", line_number)
    error_code = _number(record["error_code"], "error_code", line_number)
    duration_ms = _number(record["duration_ms"], "duration_ms", line_number)
    occurred_at = _stamp(record["stamp"], line_number)

    if status not in (SUCCEEDED, ABORTED, CANCELED):
        raise TaskEventRejected(f"line {line_number}: unknown action_status {status}")
    if outcome not in _OUTCOMES:
        raise TaskEventRejected(f"line {line_number}: unknown runtime outcome {outcome}")
    if status == SUCCEEDED and outcome != 0:
        raise TaskEventRejected(f"line {line_number}: SUCCEEDED cannot carry runtime outcome {outcome}")
    if duration_ms < 0 or not 0 <= error_code <= 0xFFFF:
        raise TaskEventRejected(f"line {line_number}: duration_ms or error_code is out of range")

    action_outcome, device_state = _OUTCOMES[outcome]
    # A published TaskEvent means the ExecuteTask goal was accepted, so a command
    # was dispatched; the runtime publishes nothing for a rejected goal.
    action_id = parts["goal_id"] or parts["task_id"]
    run_id = _join("run", parts["request_id"], parts["workflow_id"], parts["task_id"])
    return run_id, ActionResult(
        result_id=_join("runtime", action_id, str(status), str(outcome), str(error_code), str(duration_ms)),
        action_id=action_id,
        run_id=run_id,
        outcome=action_outcome,
        dispatch_state=DispatchState.SENT,
        device_state=device_state,
        error_code=error_code,
        error_reason=parts["message"],
        started_at=occurred_at,
        ended_at=occurred_at,
        retry_count=0,
        # The runtime reports the device it commanded, never where the entity
        # ended up, so the target identity is an entity claim this adapter has
        # no evidence for and is not made at all. It survives in the evidence
        # reference, and a wrong entity_id would mislead every later verifier.
        evidence_refs=[f"runtime-task-event:{parts['target_id']}:{action_id}:{status}:{outcome}:{error_code}"],
    )


def ingest_task_event(store: SQLiteEventStore, record: object, *, line_number: int = 1) -> TaskEventIngestResult:
    """Bind one TaskEvent record and append it to the run's event chain."""

    run_id, result = bind_task_event(record, line_number=line_number)
    try:
        # Keyed on the runtime's own identity, so a re-delivered or re-captured
        # line replays instead of creating a second action_result.
        event = store.append_allocated(
            event_id=f"runtime-task-event:{run_id}:{result.result_id}",
            run_id=run_id,
            event_type=WorldEventType.ACTION_RESULT,
            occurred_at=result.ended_at,
            payload=result.model_dump(mode="json"),
            evidence_refs=list(result.evidence_refs),
        )
    except (EventStoreIntegrityError, TypeError, ValueError) as error:
        raise TaskEventRejected(f"line {line_number}: {error}") from error
    return TaskEventIngestResult(line_number, run_id, result.action_id, event.event_id)


def ingest_capture(store: SQLiteEventStore, path: str | Path) -> tuple[TaskEventIngestResult, ...]:
    """Ingest a JSONL TaskEvent capture. Blank lines are skipped."""

    results = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as error:
            raise TaskEventRejected(f"line {line_number}: invalid JSON: {error}") from error
        results.append(ingest_task_event(store, record, line_number=line_number))
    return tuple(results)


def _join(prefix: str, *parts: str) -> str:
    # Length-prefixed, the same way the runtime fingerprints a request: two
    # different field tuples must never render to one identity.
    return f"{prefix}:" + "|".join(f"{len(part)}:{part}" for part in parts)


def _text(value: object, name: str, line_number: int) -> str:
    if type(value) is not str or not value.strip():
        raise TaskEventRejected(f"line {line_number}: {name} must be a non-empty string")
    return value


def _number(value: object, name: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TaskEventRejected(f"line {line_number}: {name} must be an integer")
    return value


def _stamp(value: object, line_number: int) -> str:
    """Render ``builtin_interfaces/Time`` as the run's wall-clock instant.

    An unset or unreadable stamp is rejected rather than written: a wrong time
    on an evidence row is worse than a refused line.
    """

    if not isinstance(value, dict):
        raise TaskEventRejected(f"line {line_number}: stamp must be an object")
    sec = _number(value.get("sec"), "stamp.sec", line_number)
    nanosec = _number(value.get("nanosec"), "stamp.nanosec", line_number)
    if sec <= 0 or not 0 <= nanosec < 1_000_000_000:
        raise TaskEventRejected(f"line {line_number}: stamp is not a usable time")
    try:
        moment = datetime.fromtimestamp(sec, tz=UTC) + timedelta(microseconds=nanosec // 1000)
    except (OverflowError, OSError, ValueError) as error:
        raise TaskEventRejected(f"line {line_number}: stamp.sec is out of range") from error
    return moment.isoformat().replace("+00:00", "Z")
