import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "libs/contracts"), str(ROOT / "services/world_model")]

from workbench_contracts import WorldEvent, WorldEventType
from workbench_world_model import event_store as event_store_module
from workbench_world_model.event_payloads import WorldEventPayloadValidationError

SQLiteEventStore = event_store_module.SQLiteEventStore
EventStoreIntegrityError = event_store_module.EventStoreIntegrityError
EventStoreMigrationRequiredError = event_store_module.EventStoreMigrationRequiredError


def make_event(
    event_id: str,
    *,
    run_id: str = "run-001",
    sequence_no: int = 1,
    payload: dict[str, object] | None = None,
) -> WorldEvent:
    event_payload: dict[str, object] = {
        "entity_id": "red_block",
        "entity_type": "block",
        "location": "on:table",
        "confidence": 0.9,
    }
    if payload is not None:
        event_payload.update(payload)
    return WorldEvent(
        event_id=event_id,
        run_id=run_id,
        sequence_no=sequence_no,
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-04T10:00:00Z",
        payload=event_payload,
        evidence_refs=["camera-frame-001"],
    )


def open_legacy_store(database_path: Path, table_sql: str) -> SQLiteEventStore:
    connection = sqlite3.connect(database_path)
    try:
        connection.execute(table_sql)
        connection.commit()
    finally:
        connection.close()
    return SQLiteEventStore(database_path)


def test_exact_reappend_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    event = make_event("evt-001")

    store.append(event)
    store.append(event)

    assert store.list_run("run-001") == [event]
    store.close()


def test_canonical_payload_key_order_is_idempotent(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    first = make_event("evt-001", payload={"entity_id": "red_block", "confidence": 0.98})
    reordered = make_event("evt-001", payload={"confidence": 0.98, "entity_id": "red_block"})

    store.append(first)
    store.append(reordered)

    assert store.list_run("run-001") == [first]
    store.close()


def test_conflicting_event_id_raises_integrity_error_and_preserves_original(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    original = make_event("evt-001")
    conflict = make_event("evt-001", payload={"entity_id": "blue_block", "location": "on:table"})
    store.append(original)

    with pytest.raises(EventStoreIntegrityError, match="event_id"):
        store.append(conflict)

    assert store.list_run("run-001") == [original]
    store.close()


def test_conflicting_run_sequence_raises_integrity_error_and_preserves_original(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    original = make_event("evt-001")
    conflict = make_event("evt-002")
    store.append(original)

    with pytest.raises(EventStoreIntegrityError, match=r"run_id.*sequence_no"):
        store.append(conflict)

    assert store.list_run("run-001") == [original]
    store.close()


def test_same_sequence_number_is_allowed_for_different_runs(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    first = make_event("evt-001", run_id="run-001")
    second = make_event("evt-002", run_id="run-002")

    store.append(first)
    store.append(second)

    assert store.list_run("run-001") == [first]
    assert store.list_run("run-002") == [second]
    store.close()


def test_failed_append_rolls_back_and_next_append_succeeds(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    original = make_event("evt-001", sequence_no=1)
    conflict = make_event("evt-conflict", sequence_no=1)
    following = make_event("evt-002", sequence_no=2)
    store.append(original)

    with pytest.raises(EventStoreIntegrityError):
        store.append(conflict)
    store.append(following)

    assert store.list_run("run-001") == [original, following]
    store.close()


def test_list_run_orders_by_sequence_number(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    events = [
        make_event("evt-003", sequence_no=3),
        make_event("evt-001", sequence_no=1),
        make_event("evt-002", sequence_no=2),
    ]
    for event in events:
        store.append(event)

    assert [event.sequence_no for event in store.list_run("run-001")] == [1, 2, 3]
    store.close()


def test_reopen_preserves_data_and_integrity_constraints(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    original = make_event("evt-001")
    store = SQLiteEventStore(database_path)
    store.append(original)
    store.close()

    reopened = SQLiteEventStore(database_path)
    reopened.append(original)
    with pytest.raises(EventStoreIntegrityError, match="event_id"):
        reopened.append(
            make_event(
                "evt-001",
                payload={"entity_id": "blue_block", "location": "on:table"},
            )
        )
    with pytest.raises(EventStoreIntegrityError):
        reopened.append(make_event("evt-conflict"))

    assert reopened.list_run("run-001") == [original]
    reopened.close()


def test_legacy_table_fails_closed_with_recovery_instruction(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL
        )
        """
    )
    connection.commit()
    connection.close()

    try:
        store = SQLiteEventStore(database_path)
    except EventStoreMigrationRequiredError as exc:
        message = str(exc).lower()
        assert "backup" in message
        assert "rebuild" in message
    else:
        store.close()
        pytest.fail("legacy database must fail closed")

    connection = sqlite3.connect(database_path)
    columns = connection.execute("PRAGMA table_info(world_events)").fetchall()
    connection.close()
    assert [column[1] for column in columns] == ["event_id", "run_id", "sequence_no", "event_json"]


def test_legacy_partial_sequence_index_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-partial-index.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX unique_run_sequence_partial
        ON world_events(run_id, sequence_no)
        WHERE sequence_no > 100
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(EventStoreMigrationRequiredError, match=r"backup.*rebuild"):
        SQLiteEventStore(database_path)


def test_legacy_composite_event_primary_key_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-composite-primary-key.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE world_events (
            event_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            PRIMARY KEY(event_id, run_id),
            UNIQUE(run_id, sequence_no)
        )
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(EventStoreMigrationRequiredError, match=r"backup.*rebuild"):
        SQLiteEventStore(database_path)


def test_legacy_table_with_trigger_fails_closed(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy-trigger.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, sequence_no)
        )
        """
    )
    connection.execute(
        """
        CREATE TRIGGER ignore_world_event
        BEFORE INSERT ON world_events
        BEGIN
            SELECT RAISE(IGNORE);
        END
        """
    )
    connection.commit()
    connection.close()

    with pytest.raises(EventStoreMigrationRequiredError, match=r"backup.*rebuild"):
        SQLiteEventStore(database_path)


@pytest.mark.parametrize(
    "table_sql",
    [
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY ON CONFLICT IGNORE,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, sequence_no)
        )
        """,
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY ON CONFLICT REPLACE,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, sequence_no)
        )
        """,
    ],
    ids=["ignore", "replace"],
)
def test_event_id_conflict_policy_cannot_silence_typed_error(tmp_path: Path, table_sql: str) -> None:
    store = open_legacy_store(tmp_path / "legacy-event-id-policy.sqlite", table_sql)
    original = make_event("evt-001")
    conflict = make_event(
        "evt-001",
        sequence_no=2,
        payload={"entity_id": "blue_block", "location": "on:table"},
    )

    try:
        store.append(original)
        with pytest.raises(EventStoreIntegrityError, match="event_id"):
            store.append(conflict)
        assert store.list_run("run-001") == [original]
    finally:
        store.close()


@pytest.mark.parametrize(
    "table_sql",
    [
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, sequence_no) ON CONFLICT IGNORE
        )
        """,
        """
        CREATE TABLE world_events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL,
            UNIQUE(run_id, sequence_no) ON CONFLICT REPLACE
        )
        """,
    ],
    ids=["ignore", "replace"],
)
def test_sequence_conflict_policy_cannot_silence_typed_error(tmp_path: Path, table_sql: str) -> None:
    store = open_legacy_store(tmp_path / "legacy-sequence-policy.sqlite", table_sql)
    original = make_event("evt-001")
    conflict = make_event(
        "evt-002",
        payload={"entity_id": "blue_block", "location": "on:table"},
    )

    try:
        store.append(original)
        with pytest.raises(EventStoreIntegrityError, match=r"run_id.*sequence_no"):
            store.append(conflict)
        assert store.list_run("run-001") == [original]
    finally:
        store.close()


def test_concurrent_sequence_conflict_has_one_winner(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    initial = SQLiteEventStore(database_path)
    initial.close()
    barrier = Barrier(2)

    def append(event: WorldEvent) -> str:
        store = SQLiteEventStore(database_path)
        barrier.wait()
        try:
            store.append(event)
        except EventStoreIntegrityError:
            return "conflict"
        finally:
            store.close()
        return "stored"

    events = [make_event("evt-001"), make_event("evt-002")]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, events))

    store = SQLiteEventStore(database_path)
    stored = store.list_run("run-001")
    store.close()

    assert sorted(results) == ["conflict", "stored"]
    assert len(stored) == 1
    assert stored[0] in events


def append_allocated_event(
    store: SQLiteEventStore,
    event_id: str,
    *,
    run_id: str = "run-allocated",
    payload: dict[str, object] | None = None,
) -> WorldEvent:
    result_payload: dict[str, object] = {
        "result_id": event_id,
        "action_id": "act-001",
        "run_id": run_id,
        "outcome": "completed",
        "dispatch_state": "sent",
        "device_state": "confirmed",
        "error_code": None,
        "error_reason": None,
        "started_at": "2026-08-04T10:00:00Z",
        "ended_at": "2026-08-04T10:00:01Z",
        "clock_id": "monotonic",
        "retry_count": 0,
        "entity_id": None,
        "resulting_location": None,
        "evidence_refs": ["mcu-frame-001"],
    }
    if payload is not None:
        result_payload.update(payload)
    return store.append_allocated(
        event_id=event_id,
        run_id=run_id,
        event_type=WorldEventType.ACTION_RESULT,
        occurred_at="2026-08-04T10:00:01Z",
        payload=result_payload,
        evidence_refs=["mcu-frame-001"],
    )


def test_allocated_sequence_follows_run_maximum(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")

    first = append_allocated_event(store, "evt-allocated-001")
    store.append(make_event("evt-existing", run_id="run-allocated", sequence_no=4))
    following = append_allocated_event(store, "evt-allocated-002")

    assert first.sequence_no == 1
    assert following.sequence_no == 5
    assert [event.sequence_no for event in store.list_run("run-allocated")] == [1, 4, 5]
    store.close()


def test_allocated_sequence_is_per_run(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")

    first_run = append_allocated_event(store, "evt-run-a", run_id="run-a")
    second_run = append_allocated_event(store, "evt-run-b", run_id="run-b")

    assert first_run.sequence_no == second_run.sequence_no == 1
    store.close()


def test_allocated_exact_retry_is_idempotent_and_conflict_preserves_original(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    original_payload = {"result_id": "res-001", "action_id": "act-001", "outcome": "completed"}

    original = append_allocated_event(store, "evt-allocated-001", payload=original_payload)
    retry = append_allocated_event(
        store,
        "evt-allocated-001",
        payload={"outcome": "completed", "action_id": "act-001", "result_id": "res-001"},
    )

    assert retry == original
    assert store.list_run("run-allocated") == [original]

    with pytest.raises(EventStoreIntegrityError, match="event_id"):
        append_allocated_event(
            store,
            "evt-allocated-001",
            payload={"result_id": "res-001", "action_id": "act-001", "outcome": "failed"},
        )

    assert store.list_run("run-allocated") == [original]
    store.close()


def test_get_event_returns_exact_event_or_none(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    stored = append_allocated_event(store, "evt-lookup")

    assert store.get_event("evt-lookup") == stored
    assert store.get_event("missing") is None
    store.close()


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [("sequence_no", True), ("sequence_no", "1"), ("unexpected", "value")],
    ids=["bool-as-integer", "numeric-string", "unknown-field"],
)
def test_get_event_rejects_coercive_or_unknown_stored_values(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    event = make_event("evt-strict")
    payload = event.model_dump(mode="json")
    payload[field_name] = invalid_value
    with store.connection:
        store.connection.execute(
            """
            INSERT INTO world_events(event_id, run_id, sequence_no, event_json)
            VALUES (?, ?, ?, ?)
            """,
            (event.event_id, event.run_id, event.sequence_no, json.dumps(payload)),
        )

    with pytest.raises(ValidationError):
        store.get_event(event.event_id)

    store.close()


def test_concurrent_allocated_appends_are_race_safe(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    initial = SQLiteEventStore(database_path)
    initial.close()
    barrier = Barrier(2)

    def append(event_id: str) -> WorldEvent:
        store = SQLiteEventStore(database_path)
        barrier.wait()
        try:
            return append_allocated_event(store, event_id, run_id="run-concurrent")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(append, ["evt-concurrent-a", "evt-concurrent-b"]))

    store = SQLiteEventStore(database_path)
    replayed = store.list_run("run-concurrent")
    store.close()

    assert sorted(event.sequence_no for event in results) == [1, 2]
    assert [event.sequence_no for event in replayed] == [1, 2]
    assert {event.event_id for event in replayed} == {"evt-concurrent-a", "evt-concurrent-b"}


def test_store_rejects_non_finite_json_before_insert(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    invalid = make_event("evt-nan", payload={"confidence": float("nan")})

    with pytest.raises(WorldEventPayloadValidationError, match="confidence"):
        store.append(invalid)

    assert store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 0
    assert store.list_run("run-001") == []
    store.close()


@pytest.mark.parametrize(
    ("case", "entity_type"),
    [
        ("missing", None),
        ("empty", ""),
        ("blank", "   "),
        ("integer", 7),
        ("boolean", True),
        ("array", []),
    ],
)
def test_append_rejects_invalid_entity_type_before_insert(
    tmp_path: Path,
    case: str,
    entity_type: object,
) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    invalid = make_event(f"evt-{case}")
    payload = dict(invalid.payload)
    if case == "missing":
        payload.pop("entity_type")
    else:
        payload["entity_type"] = entity_type
    invalid = invalid.model_copy(update={"payload": payload})
    before = store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]

    with pytest.raises(WorldEventPayloadValidationError, match="entity_type"):
        store.append(invalid)

    after = store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0]
    assert before == after == 0
    store.close()


def test_append_allocated_rejects_missing_entity_type_before_sequence_allocation(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")

    with pytest.raises(WorldEventPayloadValidationError, match="entity_type"):
        store.append_allocated(
            event_id="evt-invalid-observation",
            run_id="run-allocated-observation",
            event_type=WorldEventType.OBSERVATION,
            occurred_at="2026-08-04T10:00:00Z",
            payload={"entity_id": "red_block", "location": "on:table", "confidence": 0.9},
            evidence_refs=["camera-frame-001"],
        )

    assert store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 0
    stored = store.append_allocated(
        event_id="evt-valid-observation",
        run_id="run-allocated-observation",
        event_type=WorldEventType.OBSERVATION,
        occurred_at="2026-08-04T10:00:00Z",
        payload={
            "entity_id": "red_block",
            "entity_type": "block",
            "location": "on:table",
            "confidence": 0.9,
        },
        evidence_refs=["camera-frame-001"],
    )
    assert stored.sequence_no == 1
    store.close()


def test_append_rejects_invalid_observation_before_insert(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    invalid = make_event("evt-invalid", payload={"entity_id": 123})

    with pytest.raises(WorldEventPayloadValidationError, match="entity_id"):
        store.append(invalid)

    assert store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 0
    store.close()


def test_append_allocated_rejects_before_committing_sequence(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")

    with pytest.raises(WorldEventPayloadValidationError, match="result_id"):
        append_allocated_event(store, "evt-invalid", payload={"result_id": ""})

    assert store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 0
    stored = append_allocated_event(store, "evt-valid")
    assert stored.sequence_no == 1
    store.close()


def test_list_run_rejects_externally_inserted_malformed_payload(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    malformed = make_event("evt-legacy-invalid", payload={"entity_id": 123})
    event_json = json.dumps(
        malformed.model_dump(mode="python"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with store.connection:
        store.connection.execute(
            """
            INSERT INTO world_events(event_id, run_id, sequence_no, event_json)
            VALUES (?, ?, ?, ?)
            """,
            (
                malformed.event_id,
                malformed.run_id,
                malformed.sequence_no,
                event_json,
            ),
        )

    with pytest.raises(WorldEventPayloadValidationError, match="entity_id"):
        store.list_run("run-001")

    store.close()


def test_legacy_observation_without_entity_type_fails_closed_without_migration(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    legacy = make_event("evt-legacy-missing-type")
    payload = legacy.model_dump(mode="python")
    payload["payload"].pop("entity_type")
    event_json = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    with store.connection:
        store.connection.execute(
            """
            INSERT INTO world_events(event_id, run_id, sequence_no, event_json)
            VALUES (?, ?, ?, ?)
            """,
            (legacy.event_id, legacy.run_id, legacy.sequence_no, event_json),
        )

    with pytest.raises(WorldEventPayloadValidationError, match="entity_type"):
        store.get_event(legacy.event_id)
    with pytest.raises(WorldEventPayloadValidationError, match="entity_type"):
        store.list_run(legacy.run_id)

    assert store.connection.execute("SELECT COUNT(*) FROM world_events").fetchone()[0] == 1
    store.close()
