import ast
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


def stored_event_count(database_path: Path) -> int:
    """Count rows in the one table the current store writes.

    A raw connection is used on purpose: the point of these assertions is that a
    row which did *not* go through the store is still visible to the store, so
    counting through the store itself would beg the question.
    """

    connection = sqlite3.connect(database_path)
    try:
        return connection.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        connection.close()


def insert_event_row(database_path: Path, event_json: str) -> None:
    """Insert an event row directly, bypassing the store's own write path."""

    payload = json.loads(event_json)
    connection = sqlite3.connect(database_path)
    try:
        with connection:
            connection.execute(
                "INSERT INTO events(event_id, run_id, sequence_no, event_json) VALUES (?, ?, ?, ?)",
                (payload["event_id"], payload["run_id"], payload["sequence_no"], event_json),
            )
    finally:
        connection.close()


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
    """A table that declares a conflict policy is refused rather than trusted.

    The policy is the danger, not the write it governs: ``ON CONFLICT IGNORE`` on
    the primary key turns a conflicting ``event_id`` into a silently dropped
    event, and ``REPLACE`` turns it into a silently overwritten one. Neither may
    reach a caller as a success, so this store refuses to open a database whose
    schema declares one.
    """

    with pytest.raises(EventStoreMigrationRequiredError, match=r"backup.*rebuild"):
        open_legacy_store(tmp_path / "legacy-event-id-policy.sqlite", table_sql)


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
    """As above, for a conflict policy on the per-run sequence index."""

    with pytest.raises(EventStoreMigrationRequiredError, match=r"backup.*rebuild"):
        open_legacy_store(tmp_path / "legacy-sequence-policy.sqlite", table_sql)


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
    database_path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(database_path)
    event = make_event("evt-strict")
    payload = event.model_dump(mode="json")
    payload[field_name] = invalid_value
    insert_event_row(database_path, json.dumps(payload))

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

    assert stored_event_count(tmp_path / "events.sqlite") == 0
    assert store.list_run("run-001") == []
    store.close()


def test_append_rejects_invalid_observation_before_insert(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")
    invalid = make_event("evt-invalid", payload={"entity_id": 123})

    with pytest.raises(WorldEventPayloadValidationError, match="entity_id"):
        store.append(invalid)

    assert stored_event_count(tmp_path / "events.sqlite") == 0
    store.close()


def test_append_allocated_rejects_before_committing_sequence(tmp_path: Path) -> None:
    store = SQLiteEventStore(tmp_path / "events.sqlite")

    with pytest.raises(WorldEventPayloadValidationError, match="result_id"):
        append_allocated_event(store, "evt-invalid", payload={"result_id": ""})

    assert stored_event_count(tmp_path / "events.sqlite") == 0
    stored = append_allocated_event(store, "evt-valid")
    assert stored.sequence_no == 1
    store.close()


def test_list_run_rejects_externally_inserted_malformed_payload(tmp_path: Path) -> None:
    database_path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(database_path)
    malformed = make_event("evt-legacy-invalid", payload={"entity_id": 123})
    event_json = json.dumps(
        malformed.model_dump(mode="python"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    insert_event_row(tmp_path / "events.sqlite", event_json)

    with pytest.raises(WorldEventPayloadValidationError, match="entity_id"):
        store.list_run("run-001")

    store.close()


def test_one_database_holds_two_runs_numbered_per_run(tmp_path: Path) -> None:
    """Issue #161: one file holds several runs, each numbered from its own start.

    The World Model event contract numbers its first event ``1``. Under the former
    World Model table that was per-run too, but it was a second implementation;
    the assertion here is that the adapter keeps that guarantee on the one shared
    schema rather than on a World Model table of its own.
    """

    database_path = tmp_path / "events.sqlite"
    store = SQLiteEventStore(database_path)
    store.append(make_event("evt-001", run_id="run-001", sequence_no=1))
    store.append(make_event("evt-002", run_id="run-001", sequence_no=2))
    store.append(make_event("evt-003", run_id="run-002", sequence_no=1))
    store.close()

    reopened = SQLiteEventStore(database_path)
    assert [(event.run_id, event.sequence_no) for event in reopened.list_run("run-001")] == [
        ("run-001", 1),
        ("run-001", 2),
    ]
    assert [(event.run_id, event.sequence_no) for event in reopened.list_run("run-002")] == [("run-002", 1)]
    assert reopened.run_ids() == ["run-001", "run-002"]
    reopened.close()


@pytest.mark.parametrize(
    "table_sql",
    [
        # The pre-#161 Kernel layout: sequence_no globally unique, so a second run
        # could not be written at all.
        """
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL UNIQUE,
            event_json TEXT NOT NULL
        )
        """,
        # The same global uniqueness expressed as a named index.
        """
        CREATE TABLE events (
            event_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            sequence_no INTEGER NOT NULL,
            event_json TEXT NOT NULL
        );
        CREATE UNIQUE INDEX unique_sequence_everywhere ON events(sequence_no)
        """,
    ],
    ids=["inline-unique", "named-index"],
)
def test_globally_unique_sequence_layout_is_refused(tmp_path: Path, table_sql: str) -> None:
    """A database that cannot hold two runs is refused, not read as empty.

    Opening it and showing no events would be the silent-empty-database failure
    Issue #161 exists to prevent: the file holds a run, and the store would report
    that it holds nothing.
    """

    database_path = tmp_path / "v1-layout.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        for statement in (statement for statement in table_sql.split(";") if statement.strip()):
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(EventStoreMigrationRequiredError, match=r"backup.*rebuild"):
        SQLiteEventStore(database_path)

    # The refused database is left exactly as it was, so the operator can back it
    # up and rebuild it rather than finding it half-migrated.
    connection = sqlite3.connect(database_path)
    try:
        columns = [row[1] for row in connection.execute("PRAGMA table_info(events)")]
    finally:
        connection.close()
    assert columns == ["event_id", "run_id", "sequence_no", "event_json"]


def test_readable_but_unsupported_schema_version_is_refused(tmp_path: Path) -> None:
    """A database that is not empty and not current fails closed.

    A version 1 database whose ``events`` table has the current shape still cannot
    be read, because the recorded version is what the schema *means* rather than
    what its columns look like.
    """

    database_path = tmp_path / "v1-version.sqlite"
    connection = sqlite3.connect(database_path)
    try:
        connection.execute("CREATE TABLE event_store_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO event_store_meta(key, value) VALUES ('schema_version', '1')")
        connection.execute(
            """
            CREATE TABLE events (
                event_id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                sequence_no INTEGER NOT NULL,
                event_json TEXT NOT NULL,
                UNIQUE(run_id, sequence_no)
            )
            """
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(EventStoreMigrationRequiredError, match=r"schema_version.*backup.*rebuild"):
        SQLiteEventStore(database_path)


def test_world_model_module_owns_no_sql_or_connection() -> None:
    """The duplicated schema and connection lifecycle are gone, not merely unused.

    A behavioural test can pass while the second implementation is still present
    and reachable, so this reads the module's syntax tree: no SQL statement may
    appear anywhere in it, and it may not import ``sqlite3`` at all. The check is
    structural rather than textual on purpose, because the module's docstring
    legitimately *names* the table it used to own when explaining why it does not
    any more, and a text search would confuse that prose with an implementation.
    """

    tree = ast.parse((ROOT / "services/world_model/workbench_world_model/event_store.py").read_text(encoding="utf-8"))
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names} | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "sqlite3" not in imported

    literals = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)]
    for keyword in ("CREATE TABLE", "INSERT INTO", "SELECT ", "PRAGMA"):
        assert not any(keyword in literal.upper() for literal in literals), (
            f"World Model store still holds SQL: {keyword}"
        )
