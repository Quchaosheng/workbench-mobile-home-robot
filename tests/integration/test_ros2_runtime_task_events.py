"""TaskEvent ingress is validated at the trust boundary before it is stored."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(ROOT),
    str(ROOT / "libs/contracts"),
    str(ROOT / "services/world_model"),
]

from workbench_contracts import ActionOutcome, DeviceState
from workbench_world_model.event_store import SQLiteEventStore

from integrations.ros2_runtime import TaskEventRejected, bind_task_event, ingest_capture, ingest_task_event


def record(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "stamp": {"sec": 1_791_790_000, "nanosec": 123_456_789},
        "task_id": "task-1",
        "target_id": "dock_a",
        "request_id": "request-1",
        "workflow_id": "single_task",
        "goal_id": "goal-1",
        "action_status": 1,
        "outcome": 0,
        "error_code": 0,
        "duration_ms": 37,
        "message": "task completed through Device Bridge",
    }
    value.update(overrides)
    return value


def test_completed_event_binds_run_and_action_identity() -> None:
    run_id, result = bind_task_event(record())

    assert run_id == "run:9:request-1|11:single_task|6:task-1"
    assert result.action_id == "goal-1"
    assert result.outcome is ActionOutcome.COMPLETED
    assert result.device_state is DeviceState.CONFIRMED
    assert result.ended_at == "2026-10-12T07:26:40.123456Z"
    # The runtime never claims where the entity ended up, so the spatial pair
    # stays null and the target is carried as evidence instead.
    assert result.entity_id is None and result.resulting_location is None
    assert result.evidence_refs == ["runtime-task-event:dock_a:goal-1:1:0:0"]


@pytest.mark.parametrize(
    ("outcome", "expected_outcome", "expected_device"),
    [
        (1, ActionOutcome.CANCELED, DeviceState.STOPPED),
        (2, ActionOutcome.SAFE_STOP, DeviceState.STOPPED),
        (3, ActionOutcome.FAILED, DeviceState.REJECTED),
    ],
)
def test_non_success_terminal_outcomes_are_execution_evidence_not_completion(
    outcome: int, expected_outcome: ActionOutcome, expected_device: DeviceState
) -> None:
    _, result = bind_task_event(record(action_status=2, outcome=outcome, error_code=112))

    assert result.outcome is expected_outcome
    assert result.device_state is expected_device
    assert result.error_code == 112
    # SAFE_STOP must never be transcribed as a confirmed action.
    assert result.device_state is not DeviceState.CONFIRMED


@pytest.mark.parametrize(
    "overrides",
    [
        {"request_id": ""},
        {"goal_id": "   "},
        {"workflow_id": None},
        {"action_status": 9},
        {"outcome": 4},
        {"action_status": 1, "outcome": 3},
        {"error_code": -1},
        {"duration_ms": 1.5},
        {"stamp": {"sec": 0, "nanosec": 0}},
        {"stamp": {"sec": 1_791_790_000, "nanosec": 2_000_000_000}},
    ],
)
def test_unusable_or_uncorrelated_events_are_rejected(overrides: dict[str, object]) -> None:
    with pytest.raises(TaskEventRejected):
        bind_task_event(record(**overrides))


def test_missing_correlation_field_is_rejected() -> None:
    value = record()
    del value["goal_id"]

    with pytest.raises(TaskEventRejected, match="missing goal_id"):
        bind_task_event(value)


def test_ingest_appends_one_action_result_per_event_and_replays_an_exact_retry(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    first = ingest_task_event(store, record())
    again = ingest_task_event(store, record())

    events = store.list_run(first.run_id)
    assert len(events) == 1
    assert again.event_id == first.event_id
    assert events[0].event_type.value == "action_result"
    assert events[0].payload["action_id"] == "goal-1"
    store.close()


def test_two_tasks_in_one_request_are_two_runs_not_one(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    first = ingest_task_event(store, record())
    second = ingest_task_event(store, record(task_id="task-2", goal_id="goal-2"))

    assert first.run_id != second.run_id
    store.close()


def test_ingest_survives_a_restart_and_replays_the_same_run(tmp_path: Path) -> None:
    path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(path)
    first = ingest_task_event(store, record())
    store.close()

    reopened = SQLiteEventStore(path)
    again = ingest_task_event(reopened, record())
    assert again.run_id == first.run_id
    assert len(reopened.list_run(first.run_id)) == 1
    reopened.close()


def test_capture_file_reports_the_failing_line(tmp_path: Path) -> None:
    capture = tmp_path / "task_events.jsonl"
    capture.write_text(
        "\n" + '{"stamp": {"sec": 1, "nanosec": 0}}\n',
        encoding="utf-8",
    )
    store = SQLiteEventStore(tmp_path / "events.sqlite")

    with pytest.raises(TaskEventRejected, match="line 2: TaskEvent is missing task_id"):
        ingest_capture(store, capture)
    store.close()
