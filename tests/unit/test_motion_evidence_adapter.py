from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

from workbench_contracts import ActionResult, WorldEvent, WorldEventType
from workbench_world_model.event_store import EventStoreIntegrityError, SQLiteEventStore
from workbench_world_model.motion_evidence_adapter import (
    MotionEvidenceAdapter,
    MotionEvidenceValidationError,
)


def action_result_payload(**updates: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "result_id": "res-001",
        "action_id": "act-001",
        "run_id": "run-001",
        "outcome": "completed",
        "dispatch_state": "sent",
        "device_state": "confirmed",
        "error_code": None,
        "error_reason": None,
        "started_at": "2026-08-04T00:00:12.400Z",
        "ended_at": "2026-08-04T00:00:15.900Z",
        "clock_id": "monotonic",
        "retry_count": 0,
        "entity_id": "red_block",
        "resulting_location": "in:tray",
        "evidence_refs": ["mcu-frame-0142", "mcu-frame-0143"],
    }
    payload.update(updates)
    return payload


class SerializableEvent:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = data
        self.calls = 0

    def as_serializable(self) -> dict[str, object]:
        self.calls += 1
        return deepcopy(self.data)


def execution_event_data(**updates: object) -> dict[str, object]:
    data: dict[str, object] = {
        "event_type": "action_result",
        "run_id": "run-001",
        "action_id": "act-001",
        "payload": action_result_payload(),
        "clock_id": "monotonic",
    }
    data.update(updates)
    return data


def test_valid_action_result_maps_to_world_event(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    adapter = MotionEvidenceAdapter(store)
    source = SerializableEvent(execution_event_data())
    original = deepcopy(source.data)

    reference = adapter.append(source)
    stored = adapter.resolve(reference)

    assert source.calls == 1
    assert source.data == original
    assert reference == "world-event:motion-result:run-001:res-001"
    assert isinstance(stored, WorldEvent)
    assert stored.event_id == "motion-result:run-001:res-001"
    assert stored.run_id == "run-001"
    assert stored.sequence_no == 1
    assert stored.event_type is WorldEventType.ACTION_RESULT
    assert stored.occurred_at == "2026-08-04T00:00:15.900Z"
    assert stored.evidence_refs == ["mcu-frame-0142", "mcu-frame-0143"]
    assert ActionResult.model_validate_json(json.dumps(stored.payload)).model_dump(mode="json") == stored.payload
    store.close()


def test_stable_reference_resolves_exact_event(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    adapter = MotionEvidenceAdapter(store)

    reference = adapter.append(SerializableEvent(execution_event_data()))

    assert adapter.resolve(reference) == store.list_run("run-001")[0]
    assert adapter.resolve("world-event:missing") is None
    assert adapter.resolve("unsupported:motion-result:run-001:res-001") is None
    store.close()


@pytest.mark.parametrize(
    "data",
    [
        execution_event_data(event_type="node_started"),
        execution_event_data(payload={}),
        execution_event_data(run_id="run-other"),
        execution_event_data(action_id="act-other"),
        execution_event_data(payload=action_result_payload(result_id="")),
        execution_event_data(
            payload=action_result_payload(
                outcome="failed",
                device_state="rejected",
            )
        ),
    ],
    ids=[
        "node-started",
        "invalid-payload",
        "run-mismatch",
        "action-mismatch",
        "empty-result-id",
        "failed-resulting-location",
    ],
)
def test_invalid_execution_event_fails_before_write(tmp_path: Path, data: dict[str, object]) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    adapter = MotionEvidenceAdapter(store)

    with pytest.raises(MotionEvidenceValidationError):
        adapter.append(SerializableEvent(data))

    assert store.list_run("run-001") == []
    assert store.list_run("run-other") == []
    store.close()


def test_identical_retry_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    adapter = MotionEvidenceAdapter(store)
    first = SerializableEvent(execution_event_data())
    reordered_payload = dict(reversed(list(action_result_payload().items())))
    retry = SerializableEvent(execution_event_data(payload=reordered_payload))

    first_reference = adapter.append(first)
    retry_reference = adapter.append(retry)

    assert retry_reference == first_reference
    assert len(store.list_run("run-001")) == 1
    store.close()


def test_conflicting_result_retry_preserves_original(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    adapter = MotionEvidenceAdapter(store)
    reference = adapter.append(SerializableEvent(execution_event_data()))
    original = adapter.resolve(reference)

    with pytest.raises(EventStoreIntegrityError, match="event_id"):
        adapter.append(SerializableEvent(execution_event_data(payload=action_result_payload(retry_count=1))))

    assert store.list_run("run-001") == [original]
    store.close()


def test_append_uses_detached_serializable_snapshot(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    adapter = MotionEvidenceAdapter(store)
    source_data = execution_event_data()
    source = SerializableEvent(source_data)

    reference = adapter.append(source)
    payload = source_data["payload"]
    assert isinstance(payload, dict)
    payload["resulting_location"] = "on:table"
    evidence_refs = payload["evidence_refs"]
    assert isinstance(evidence_refs, list)
    evidence_refs.append("mutated-after-append")

    stored = adapter.resolve(reference)
    assert stored is not None
    assert stored.payload["resulting_location"] == "in:tray"
    assert stored.evidence_refs == ["mcu-frame-0142", "mcu-frame-0143"]
    store.close()


def test_persistence_failures_propagate_without_reference(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(database_path)
    adapter = MotionEvidenceAdapter(store)
    invalid_json = execution_event_data()
    invalid_json["unsupported"] = object()

    with pytest.raises(TypeError):
        adapter.append(SerializableEvent(invalid_json))
    assert store.list_run("run-001") == []

    class CommitFailingConnection:
        """A connection whose commit fails after the row is written.

        The failure is injected at the commit rather than at the insert on
        purpose: it is the case where the row is present inside the transaction
        and must not survive it, so a rollback that did nothing would be visible
        as a stored event rather than as an error.
        """

        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection
            self.insert_was_visible = False

        def execute(self, statement: str, parameters: tuple[object, ...] = ()) -> sqlite3.Cursor:
            return self._connection.execute(statement, parameters)

        def executemany(self, statement: str, parameters: object) -> sqlite3.Cursor:
            return self._connection.executemany(statement, parameters)

        def commit(self) -> None:
            self.insert_was_visible = self._connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1
            raise sqlite3.OperationalError("injected commit failure")

        def rollback(self) -> None:
            self._connection.rollback()

        def backup(self, target: sqlite3.Connection) -> None:
            self._connection.backup(target)

        @property
        def in_transaction(self) -> bool:
            return self._connection.in_transaction

        def close(self) -> None:
            self._connection.close()

    failing_connection = CommitFailingConnection(store._store._connection)
    store._store._connection = failing_connection
    reference: str | None = None
    with pytest.raises(EventStoreIntegrityError, match="could not be appended") as failure:
        reference = adapter.append(SerializableEvent(execution_event_data()))

    # The injected failure is still reachable through the cause chain, so an
    # operator sees the real reason and not only the typed wrapper the adapter
    # publishes over the Kernel's typed error.
    causes = []
    current: BaseException | None = failure.value
    while current is not None:
        causes.append(current)
        current = current.__cause__
    assert any(
        isinstance(cause, sqlite3.OperationalError) and "injected commit failure" in str(cause) for cause in causes
    ), [type(cause).__name__ for cause in causes]
    assert reference is None
    assert failing_connection.insert_was_visible
    assert not failing_connection.in_transaction
    assert store.list_run("run-001") == []
    store.close()

    reopened = SQLiteEventStore(database_path)
    recovered_adapter = MotionEvidenceAdapter(reopened)
    assert reopened.list_run("run-001") == []
    recovered_reference = recovered_adapter.append(SerializableEvent(execution_event_data()))
    recovered = recovered_adapter.resolve(recovered_reference)
    assert recovered is not None
    assert recovered.sequence_no == 1
    reopened.close()

    class FailingStore:
        def raw_event_row(self, _: str) -> None:
            return None

        def append_allocated(self, **_: object) -> WorldEvent:
            raise RuntimeError("store unavailable")

        def get_event(self, _: str) -> WorldEvent | None:
            return None

    with pytest.raises(RuntimeError, match="store unavailable"):
        MotionEvidenceAdapter(FailingStore()).append(SerializableEvent(execution_event_data()))

    # A closed store is refused with the adapter's own typed error rather than
    # leaking the Kernel's error type or a bare sqlite3 ProgrammingError.
    with pytest.raises(EventStoreIntegrityError, match="no open SQLite connection"):
        adapter.append(SerializableEvent(execution_event_data()))


def test_adapter_import_does_not_require_motion_package() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)

    result = subprocess.run(
        [sys.executable, "-c", "import workbench_world_model.motion_evidence_adapter"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
